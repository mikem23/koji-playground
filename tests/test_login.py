import unittest
import mock
from mock import call
import koji
import krbV
import socket


class LoginTestCase(unittest.TestCase):
    maxDiff = None

    def test_create_auth_handler(self):
        options = mock.Mock()
        for authtype in ['noauth', 'ssl', 'password', 'kerberos']:
            options.authtype = authtype
            handler = koji.create_auth_handler(options)
            self.assertEqual(handler, koji._handler_mapping.get(authtype))
        options.authtype = 'notavailble'
        with self.assertRaises(koji.AuthError):
            koji.create_auth_handler(options)
        options.authtype = None
        handler = koji.create_auth_handler(options)
        self.assertIsInstance(handler, koji.DefaultAuthHandler)

    def test_noauth_handler(self):
        handler = koji.NoAuthHandler('noauth')
        options = mock.Mock()
        options.noauth = True
        self.assertTrue(handler.test(options))
        self.assertTrue(handler.login(None, options))

    def test_ssl_handler(self):
        handler = koji.SSLAuthHandler('ssl')
        options = mock.MagicMock()
        self.assertTrue(handler.test(options))
        session = mock.MagicMock()
        session.ssl_login.return_value = True
        self.assertTrue(handler.login(session, options))
        session.ssl_login.return_value = False
        self.assertFalse(handler.login(session, options))
        session.ssl_login.side_effect = koji.AuthError
        with self.assertRaises(koji.AuthError):
            handler.login(session, options)
        options.cert = 'cert'
        options.serverca = 'serverca'
        options.runas = 'proxyuser'
        self.assertEqual(handler.build_debug_info(options=options, error=koji.AuthError('errormsg')),
                         {'proxyuser': 'proxyuser', 'serverca': 'serverca', 'cert': 'cert',
                          'error': 'AuthError: errormsg'})

    def test_password_handler(self):
        handler = koji.PasswordAuthHandler('password')
        options = mock.MagicMock()
        options.user = None
        self.assertFalse(bool(handler.test(options)))
        options.user = 'user'
        self.assertTrue(bool(handler.test(options)))
        session = mock.MagicMock()
        session.login.return_value = True
        self.assertTrue(handler.login(session, options))
        session.login.return_value = False
        self.assertFalse(handler.login(session, options))
        session.login.side_effect = koji.AuthError
        with self.assertRaises(koji.AuthError):
            handler.login(session, options)
        self.assertEqual(handler.build_debug_info(options=options, error=koji.AuthError('errormsg')),
                         {'user': 'user', 'error': 'AuthError: errormsg'})

    def test_kerberos_handler(self):
        handler = koji.KerberosAuthHandler('kerberos')
        options = mock.MagicMock()
        with mock.patch('koji.krbV', new=None):
            self.assertFalse(bool(handler.test(options)))
        with mock.patch('koji.krbV') as krbV_mock:
            self.assertTrue(bool(handler.test(options)))
            krbV_mock.Krb5Error = krbV.Krb5Error
            krbV_mock.default_context.side_effect = krbV_mock.Krb5Error
            self.assertFalse(bool(handler.test(options)))
        session = mock.MagicMock()
        session.krb_login.return_value = True
        self.assertTrue(handler.login(session, options))
        session.krb_login.assert_called_with(principal=options.principal, keytab=options.keytab,
                                             proxyuser=options.runas)
        session.krb_login.return_value = False
        options.keytab = None
        self.assertFalse(handler.login(session, options))
        session.krb_login.assert_called_with(proxyuser=options.runas)
        with self.assertRaises(koji.AuthError) as cm:
            session.krb_login.side_effect = krbV.Krb5Error(10000, 'errormsg')
            handler.login(session, options)
        self.assertEqual(cm.exception.message, 'Kerberos authentication failed: errormsg (10000)')
        with self.assertRaises(koji.AuthError) as cm:
            session.krb_login.side_effect = socket.error(mock.ANY, 'errormsg')
            handler.login(session, options)

        self.assertEqual(handler.build_debug_info(options=options, error=koji.AuthError('errormsg')),
                         {'keytab': options.keytab,
                          'principal': options.principal,
                          'proxyuser': options.runas,
                          'error': 'AuthError: errormsg'})
        self.assertEqual(cm.exception.message,
                         'Could not connect to Kerberos authentication service: errormsg')

    def test_default_handler(self):
        with mock.patch('koji._build_auth_handler_seq', return_value=[]):
            with self.assertRaises(koji.AuthError) as cm:
                options = mock.MagicMock()
                handler = koji.DefaultAuthHandler(options)
            self.assertEqual(cm.exception.message, 'Unable to log in, no authentication methods available.')

        mock_handler1 = mock.MagicMock()
        mock_handler1.authtype = 'authtype1'
        mock_handler2 = mock.MagicMock()
        mock_handler2.authtype = 'authtype2'
        mock_handler3 = mock.MagicMock()
        mock_handler3.authtype = 'authtype3'
        mock_handler4 = mock.MagicMock()
        mock_handler4.authtype = 'authtype4'
        handler_seq = [('authtype1', mock_handler1),
                       ('authtype2', mock_handler2),
                       ('authtype3', mock_handler3),
                       ('authtype4', mock_handler4)]
        mock_manager = mock.MagicMock()
        mock_handler1.login.return_value = False
        inner_exc = Exception()
        mock_handler2.login.side_effect = inner_exc
        mock_handler3.login.side_effect = [True, False]
        mock_handler4.login.return_value = False

        options = mock.MagicMock()
        session = mock.MagicMock()
        for (authtype, handler) in handler_seq:
            mock_manager.attach_mock(handler, authtype + '_handler')
        with mock.patch('koji._build_auth_handler_seq', return_value=handler_seq):
            with mock.patch('koji.DefaultAuthHandler._build_debug_msg') as build_debug_msg_mock:
                mock_manager.attach_mock(build_debug_msg_mock, 'build_debug_msg')
                handler = koji.DefaultAuthHandler(options)
                self.assertTrue(handler.login(session, options))
                self.assertEqual(handler.authtype, 'authtype3')
                self.assertEqual(handler.infos, [('authtype1', mock_handler1.build_debug_info.return_value),
                                                 ('authtype2', mock_handler2.build_debug_info.return_value)])
                with self.assertRaises(koji.AuthError):
                    handler = koji.DefaultAuthHandler(options)
                    handler.login(session, options)
                self.assertEqual(handler.infos, [('authtype1', mock_handler1.build_debug_info.return_value),
                                                 ('authtype2', mock_handler2.build_debug_info.return_value),
                                                 ('authtype3', mock_handler3.build_debug_info.return_value),
                                                 ('authtype4', mock_handler4.build_debug_info.return_value)])
            self.assertListEqual(mock_manager.mock_calls,
                                 [call.authtype1_handler.login(session, options),
                                  call.authtype1_handler.build_debug_info(options),
                                  call.authtype2_handler.login(session, options),
                                  call.authtype2_handler.build_debug_info(options, inner_exc),
                                  call.authtype3_handler.login(session, options),
                                  call.authtype1_handler.login(session, options),
                                  call.authtype1_handler.build_debug_info(options),
                                  call.authtype2_handler.login(session, options),
                                  call.authtype2_handler.build_debug_info(options, inner_exc),
                                  call.authtype3_handler.login(session, options),
                                  call.authtype3_handler.build_debug_info(options),
                                  call.authtype4_handler.login(session, options),
                                  call.authtype4_handler.build_debug_info(options),
                                  call.build_debug_msg(session, options)])

            session.baseurl = 'baseurl'
            options.profile = 'profile'
            handler.infos = [('authtype1', {'item1': 1, 'item2': 'str'}),
                             ('authtype2', {'item1': 'sth', 'item3': None, 'item0': 'nth'})]
            self.assertMultiLineEqual(handler._build_debug_msg(session, options),
                                      """You are using the hub at baseurl
but unable to log in, no available authentication method succeeds.

Please check this trace that shows what auth methods you attempted with the errors and parameters.
    [TRACE]:
    - [0] Bad Authentication via authtype1:
        item1           1
        item2           str
    - [1] Bad Authentication via authtype2:
        item0           nth
        item1           sth
        item3           None


(Your authentication parameters can be specified in command line or in config files.
Type "koji --help" for help about global options,
or check the "[profile]" section in your config files;
or use "koji --authtype=noauth <command> ..." to skip login phase.
    --authtype=AUTHTYPE   force use of a type of authentication, options:
                          kerberos, noauth, password, ssl)
""")


if __name__ == '__main__':
    unittest.main()
