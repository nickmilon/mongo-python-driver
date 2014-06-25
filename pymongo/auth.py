# Copyright 2013-2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Authentication helpers."""

from __future__ import unicode_literals

import hmac

HAVE_KERBEROS = True
try:
    import kerberos
except ImportError:
    HAVE_KERBEROS = False

from collections import namedtuple
from hashlib import md5

from bson.binary import Binary
from bson.py3compat import b, string_type, _unicode
from bson.son import SON
from pymongo.errors import ConfigurationError, OperationFailure


MECHANISMS = frozenset(['GSSAPI', 'MONGODB-CR', 'MONGODB-X509', 'PLAIN'])
"""The authentication mechanisms supported by PyMongo."""


MongoCredential = namedtuple('MongoCredential',
    ['mechanism', 'source', 'username', 'password', 'mechanism_properties'])


def _build_credentials_tuple(mech, source, user, passwd, extra):
    """Build and return a mechanism specific credentials tuple.
    """
    if mech == 'GSSAPI':
        props = {'service_name': extra.get('gssapiservicename', 'mongodb')}
        # No password, source is always $external.
        return MongoCredential(mech, '$external', user, None, props)
    elif mech == 'MONGODB-X509':
        return MongoCredential(mech, '$external', user, None, None)
    return MongoCredential(mech, source, user, passwd, None)


def _password_digest(username, password):
    """Get a password digest to use for authentication.
    """
    if not isinstance(password, string_type):
        raise TypeError("password must be an "
                        "instance of %s" % (string_type.__name__,))
    if len(password) == 0:
        raise ValueError("password can't be empty")
    if not isinstance(username, string_type):
        raise TypeError("password must be an "
                        "instance of  %s" % (string_type.__name__,))

    md5hash = md5()
    data = "%s:mongo:%s" % (username, password)
    md5hash.update(data.encode('utf-8'))
    return _unicode(md5hash.hexdigest())


def _auth_key(nonce, username, password):
    """Get an auth key to use for authentication.
    """
    digest = _password_digest(username, password)
    md5hash = md5()
    data = "%s%s%s" % (nonce, _unicode(username), digest)
    md5hash.update(data.encode('utf-8'))
    return _unicode(md5hash.hexdigest())


def _authenticate_gssapi(credentials, sock_info, cmd_func):
    """Authenticate using GSSAPI.
    """
    try:
        username = credentials.username
        gsn = credentials.mechanism_properties['service_name']
        # Starting here and continuing through the while loop below - establish
        # the security context. See RFC 4752, Section 3.1, first paragraph.
        result, ctx = kerberos.authGSSClientInit(
            gsn + '@' + sock_info.host, gssflags=kerberos.GSS_C_MUTUAL_FLAG)

        if result != kerberos.AUTH_GSS_COMPLETE:
            raise OperationFailure('Kerberos context failed to initialize.')

        try:
            # pykerberos uses a weird mix of exceptions and return values
            # to indicate errors.
            # 0 == continue, 1 == complete, -1 == error
            # Only authGSSClientStep can return 0.
            if kerberos.authGSSClientStep(ctx, '') != 0:
                raise OperationFailure('Unknown kerberos '
                                       'failure in step function.')

            # Start a SASL conversation with mongod/s
            # Note: pykerberos deals with base64 encoded byte strings.
            # Since mongo accepts base64 strings as the payload we don't
            # have to use bson.binary.Binary.
            payload = kerberos.authGSSClientResponse(ctx)
            cmd = SON([('saslStart', 1),
                       ('mechanism', 'GSSAPI'),
                       ('payload', payload),
                       ('autoAuthorize', 1)])
            response, _ = cmd_func(sock_info, '$external', cmd)

            # Limit how many times we loop to catch protocol / library issues
            for _ in range(10):
                result = kerberos.authGSSClientStep(ctx,
                                                    str(response['payload']))
                if result == -1:
                    raise OperationFailure('Unknown kerberos '
                                           'failure in step function.')

                payload = kerberos.authGSSClientResponse(ctx) or ''

                cmd = SON([('saslContinue', 1),
                           ('conversationId', response['conversationId']),
                           ('payload', payload)])
                response, _ = cmd_func(sock_info, '$external', cmd)

                if result == kerberos.AUTH_GSS_COMPLETE:
                    break
            else:
                raise OperationFailure('Kerberos '
                                       'authentication failed to complete.')

            # Once the security context is established actually authenticate.
            # See RFC 4752, Section 3.1, last two paragraphs.
            if kerberos.authGSSClientUnwrap(ctx,
                                            str(response['payload'])) != 1:
                raise OperationFailure('Unknown kerberos '
                                       'failure during GSS_Unwrap step.')

            if kerberos.authGSSClientWrap(ctx,
                                          kerberos.authGSSClientResponse(ctx),
                                          username) != 1:
                raise OperationFailure('Unknown kerberos '
                                       'failure during GSS_Wrap step.')

            payload = kerberos.authGSSClientResponse(ctx)
            cmd = SON([('saslContinue', 1),
                       ('conversationId', response['conversationId']),
                       ('payload', payload)])
            response, _ = cmd_func(sock_info, '$external', cmd)

        finally:
            kerberos.authGSSClientClean(ctx)

    except kerberos.KrbError as exc:
        raise OperationFailure(str(exc))


def _authenticate_plain(credentials, sock_info, cmd_func):
    """Authenticate using SASL PLAIN (RFC 4616)
    """
    source = credentials.source
    username = credentials.username
    password = credentials.password
    payload = ('\x00%s\x00%s' % (username, password)).encode('utf-8')
    cmd = SON([('saslStart', 1),
               ('mechanism', 'PLAIN'),
               ('payload', Binary(payload)),
               ('autoAuthorize', 1)])
    cmd_func(sock_info, source, cmd)


def _authenticate_cram_md5(credentials, sock_info, cmd_func):
    """Authenticate using CRAM-MD5 (RFC 2195)
    """
    source = credentials.source
    username = credentials.username
    password = credentials.password
    # The password used as the mac key is the
    # same as what we use for MONGODB-CR
    passwd = _password_digest(username, password)
    cmd = SON([('saslStart', 1),
               ('mechanism', 'CRAM-MD5'),
               ('payload', Binary(b'')),
               ('autoAuthorize', 1)])
    response, _ = cmd_func(sock_info, source, cmd)
    # MD5 as implicit default digest for digestmod is deprecated
    # in python 3.4
    mac = hmac.HMAC(key=passwd.encode('utf-8'), digestmod=md5)
    mac.update(response['payload'])
    challenge = username.encode('utf-8') + b' ' + b(mac.hexdigest())
    cmd = SON([('saslContinue', 1),
               ('conversationId', response['conversationId']),
               ('payload', Binary(challenge))])
    cmd_func(sock_info, source, cmd)


def _authenticate_x509(credentials, sock_info, cmd_func):
    """Authenticate using MONGODB-X509.
    """
    query = SON([('authenticate', 1),
                 ('mechanism', 'MONGODB-X509'),
                 ('user', credentials.username)])
    cmd_func(sock_info, '$external', query)


def _authenticate_mongo_cr(credentials, sock_info, cmd_func):
    """Authenticate using MONGODB-CR.
    """
    source = credentials.source
    username = credentials.username
    password = credentials.password
    # Get a nonce
    response, _ = cmd_func(sock_info, source, {'getnonce': 1})
    nonce = response['nonce']
    key = _auth_key(nonce, username, password)

    # Actually authenticate
    query = SON([('authenticate', 1),
                 ('user', username),
                 ('nonce', nonce),
                 ('key', key)])
    cmd_func(sock_info, source, query)


_AUTH_MAP = {
    'CRAM-MD5': _authenticate_cram_md5,
    'GSSAPI': _authenticate_gssapi,
    'MONGODB-CR': _authenticate_mongo_cr,
    'MONGODB-X509': _authenticate_x509,
    'PLAIN': _authenticate_plain,
}


def authenticate(credentials, sock_info, cmd_func):
    """Authenticate sock_info.
    """
    mechanism = credentials.mechanism
    if mechanism == 'GSSAPI':
        if not HAVE_KERBEROS:
            raise ConfigurationError('The "kerberos" module must be '
                                     'installed to use GSSAPI authentication.')
    auth_func = _AUTH_MAP.get(mechanism)
    auth_func(credentials, sock_info, cmd_func)

