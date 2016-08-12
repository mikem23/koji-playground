import unittest

# This is python-mock, not the rpm mock tool we know and love
import mock

import koji


class KrbVTestCase(unittest.TestCase):

    @mock.patch('koji.krbV', new=None)
    @mock.patch('koji.gssapi', new=None)
    @mock.patch('koji.ClientSession._setup_connection')
    def test_krbv_disabled(self, _setup_connection):
        """ Test that when krb libs are absent, we behave rationally. """
        self.assertEquals(koji.krbV, None)
        self.assertEquals(koji.gssapi, None)
        session = koji.ClientSession('whatever')
        with self.assertRaises(ImportError):
            session.krb_login()

    @mock.patch('koji.krbV', new=None)
    @mock.patch('koji.gssapi', new=True)
    @mock.patch('koji.ClientSession.krb_gssapi_login')
    @mock.patch('koji.ClientSession.krb_krbV_login')
    @mock.patch('koji.ClientSession._setup_connection')
    @mock.patch('koji.ClientSession._callMethod')
    def test_krbv_disabled(self, _callMethod, _setup_connection, krb_krbV_login, krb_gssapi_login):
        """ Test that gssapi codepath gets used """
        #import pdb; pdb.set_trace()
        self.assertEquals(koji.krbV, None)
        self.assertEquals(koji.gssapi, True)
        krb_gssapi_login.return_value = '23 fnord'  #session id and key
        session = koji.ClientSession('whatever')
        login_args = ('principal', 'keytab', 'ccache', 'proxyuser')
        session.krb_login(*login_args)
        krb_krbV_login.assert_not_called()
        krb_gssapi_login.assert_called_with(*login_args)
