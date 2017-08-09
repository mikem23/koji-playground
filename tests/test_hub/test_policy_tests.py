import mock
import unittest

import koji
import kojihub


class TestBasicTests(unittest.TestCase):

    def test_operation_test(self):
        obj = kojihub.OperationTest('operation foo*')
        self.assertFalse(obj.run({'operation': 'FOOBAR'}))
        self.assertTrue(obj.run({'operation': 'foobar'}))

    @mock.patch('kojihub.policy_get_pkg')
    def test_package_test(self, policy_get_pkg):
        obj = kojihub.PackageTest('package foo*')
        policy_get_pkg.return_value = {'name': 'mypackage'}
        self.assertFalse(obj.run({}))
        policy_get_pkg.return_value = {'name': 'foobar'}
        self.assertTrue(obj.run({}))

    @mock.patch('kojihub.policy_get_pkg')
    def test_new_package_test(self, policy_get_pkg):
        obj = kojihub.NewPackageTest('is_new_package')
        policy_get_pkg.return_value = {'name': 'mypackage', 'id': 42}
        self.assertFalse(obj.run({}))
        policy_get_pkg.return_value = {'name': 'foobar', 'id': None}
        self.assertTrue(obj.run({}))


class TestPolicyGetUser(unittest.TestCase):

    def setUp(self):
        self.get_user = mock.patch('kojihub.get_user').start()
        self.context = mock.patch('kojihub.context').start()

    def tearDown(self):
        mock.patch.stopall()

    def test_get_user_specified(self):
        self.get_user.return_value = 'USER'
        result = kojihub.policy_get_user({'user_id': 42})
        self.assertEqual(result, 'USER')
        self.get_user.assert_called_once_with(42)

    def test_get_no_user(self):
        self.context.session.logged_in = False
        result = kojihub.policy_get_user({})
        self.assertEqual(result, None)
        self.get_user.assert_not_called()

    def test_get_logged_in_user(self):
        self.context.session.logged_in = True
        self.context.session.user_id = 99
        self.get_user.return_value = 'USER'
        result = kojihub.policy_get_user({})
        self.assertEqual(result, 'USER')
        self.get_user.assert_called_once_with(99)

    def test_get_user_specified_with_login(self):
        self.get_user.return_value = 'USER'
        self.context.session.logged_in = True
        self.context.session.user_id = 99
        result = kojihub.policy_get_user({'user_id': 42})
        self.assertEqual(result, 'USER')
        self.get_user.assert_called_once_with(42)


class TestPolicyGetCGs(unittest.TestCase):

    def setUp(self):
        self.get_build = mock.patch('kojihub.get_build').start()
        self.list_rpms = mock.patch('kojihub.list_rpms').start()
        self.list_archives = mock.patch('kojihub.list_archives').start()
        self.get_buildroot = mock.patch('kojihub.get_buildroot').start()

    def tearDown(self):
        mock.patch.stopall()

    def _fakebr(self, br_id, strict):
        self.assertEqual(strict, True)
        return {'cg_name': self._cgname(br_id)}

    def _cgname(self, br_id):
        if br_id is None:
            return None
        return 'cg for br %s'% br_id

    def test_policy_get_cg_basic(self):
        self.get_build.return_value = {'id': 42}
        br1 = [1,1,1,2,3,4,5,5]
        br2 = [2,2,7,7,8,8,9,9,None]
        self.list_rpms.return_value = [{'buildroot_id': n} for n in br1]
        self.list_archives.return_value = [{'buildroot_id': n} for n in br2]
        self.get_buildroot.side_effect = self._fakebr
        # let's see...
        result = kojihub.policy_get_cgs({'build': 'NVR'})
        expect = set([self._cgname(n) for n in br1 + br2])
        self.assertEqual(result, expect)
        self.list_rpms.called_once_with(buildID=42)
        self.list_archives.called_once_with(buildID=42)
        self.get_build.called_once_with('NVR', strict=True)

    def test_policy_get_cg_nobuild(self):
        with self.assertRaises(koji.GenericError):
            kojihub.policy_get_cgs({'package': 'foobar'})
        self.get_build.assert_not_called()
