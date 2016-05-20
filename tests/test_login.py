import unittest
import mock
import koji
import sys

class LoginTestCase(unittest.TestCase):
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
        self.assertTrue(handler.login(session=None, options=options))

    def test_ssl_handler(self):
        handler = koji.SSLAuthHandler('ssl')
        options = mock.Mock()
        self.assertTrue(handler.test(options))
        with mock.patch('koji.ClientSession') as MockClientSession:
            session = MockClientSession.return_value
            session.ssl_login.return_value = True
            self.assertTrue(handler.login(session=session, options=options))
            session.ssl_login.return_value = False
            self.assertFalse(handler.login(session=session, options=options))
            session.ssl_login.side_effect = koji.AuthError
            with self.assertRaises(koji.AuthError):
                handler.login(session=session, options=options)
        options.cert = 'cert'
        options.serverca = 'serverca'
        options.runas = 'proxyuser'
        self.assertEqual(handler.build_debug_info(options=options, error=koji.AuthError('errormsg')),
                         {'proxyuser': 'proxyuser', 'serverca': 'serverca', 'cert': 'cert',
                          'error': 'AuthError: errormsg'})

    def test_password_handler(self):
        handler = koji.PasswordAuthHandler('password')
        options = mock.Mock()
        options.user = None
        self.assertFalse(bool(handler.test(options)))
        options.user = 'user'
        self.assertTrue(bool(handler.test(options)))
        with mock.patch('koji.ClientSession') as MockClientSession:
            session = MockClientSession.return_value
            session.login.return_value = True
            self.assertTrue(handler.login(session=session, options=options))
            session.login.return_value = False
            self.assertFalse(handler.login(session=session, options=options))
            session.login.side_effect = koji.AuthError
            with self.assertRaises(koji.AuthError):
                handler.login(session=session, options=options)
        self.assertEqual(handler.build_debug_info(options=options, error=koji.AuthError('errormsg')),
                         {'user': 'user', 'error': 'AuthError: errormsg'})


if __name__ == '__main__':
    unittest.main()
