import unittest

import mock

import koji
import kojihub


class TestCreateUser(unittest.TestCase):

    def setUp(self):
        self.verify_name_user = mock.patch('kojihub.verify_name_user').start()
        self.get_user = mock.patch('kojihub.get_user').start()
        self.get_user_by_krb_principal = mock.patch('kojihub.get_user_by_krb_principal').start()
        self.exports = kojihub.RootExports()
        self.context = mock.patch('kojihub.context').start()
        # It seems MagicMock will not automatically handle attributes that
        # start with "assert"
        self.context.session.assertPerm = mock.MagicMock()
        self.context.session.assertLogin = mock.MagicMock()
        self.user_name = 'test_user'
        self.user_info = {'id': 1, 'krb_principals': [], 'name': self.user_name,
                          'status': 0, 'usertype': 0}
        self.user_info_krb = {'id': 1, 'krb_principals': ['test_user@fedora.org'],
                              'name': self.user_name, 'status': 0, 'usertype': 0}

    def test_create_user_wrong_format(self):
        user_name = 'test-user+'

        # name is longer as expected
        self.verify_name_user.side_effect = koji.GenericError
        with self.assertRaises(koji.GenericError):
            self.exports.createUser(user_name)

        # not except regex rules
        self.verify_name_user.side_effect = koji.GenericError
        with self.assertRaises(koji.GenericError):
            self.exports.createUser(user_name)

    def test_create_user_exists(self):
        expected = 'user already exists: %s' % self.user_name
        self.verify_name_user.return_value = None
        self.get_user.return_value = self.user_info
        with self.assertRaises(koji.GenericError) as cm:
            self.exports.createUser(self.user_name)
        self.assertEqual(expected, str(cm.exception))

    def test_create_user_exists_krb(self):
        krb_principal = 'test_user@fedora.org'
        expected = 'user with this Kerberos principal already exists: %s' % krb_principal
        self.verify_name_user.return_value = None
        self.get_user.return_value = None
        self.get_user_by_krb_principal.return_value = self.user_info_krb
        with self.assertRaises(koji.GenericError) as cm:
            self.exports.createUser(self.user_name, krb_principal=krb_principal)
        self.assertEqual(expected, str(cm.exception))
