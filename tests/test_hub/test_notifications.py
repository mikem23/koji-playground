from __future__ import absolute_import
import mock
try:
    import unittest2 as unittest
except ImportError:
    import unittest

import koji
import kojihub

QP = kojihub.QueryProcessor
IP = kojihub.InsertProcessor
UP = kojihub.UpdateProcessor

class TestNotifications(unittest.TestCase):
    def getInsert(self, *args, **kwargs):
        insert = IP(*args, **kwargs)
        insert.execute = mock.MagicMock()
        self.inserts.append(insert)
        return insert

    def getQuery(self, *args, **kwargs):
        query = QP(*args, **kwargs)
        query.execute = mock.MagicMock()
        self.queries.append(query)
        return query

    def getUpdate(self, *args, **kwargs):
        update = UP(*args, **kwargs)
        update.execute = mock.MagicMock()
        self.updates.append(update)
        return update

    def setUp(self):
        self.context = mock.patch('kojihub.context').start()
        self.context.opts = {
            'EmailDomain': 'test.domain.com',
            'NotifyOnSuccess': True,
        }

        self.QueryProcessor = mock.patch('kojihub.QueryProcessor',
                side_effect=self.getQuery).start()
        self.queries = []
        self.InsertProcessor = mock.patch('kojihub.InsertProcessor',
                side_effect=self.getInsert).start()
        self.inserts = []
        self.UpdateProcessor = mock.patch('kojihub.UpdateProcessor',
                side_effect=self.getUpdate).start()
        self.updates = []

        self.exports = kojihub.RootExports()
        self.exports.getLoggedInUser = mock.MagicMock()
        self.exports.getUser = mock.MagicMock()
        self.exports.hasPerm = mock.MagicMock()
        self.exports.getBuildNotification = mock.MagicMock()

    def tearDown(self):
        mock.patch.stopall()


    @mock.patch('kojihub.get_user')
    @mock.patch('kojihub.readPackageList')
    def test_get_notification_recipients(self, readPackageList, get_user):
        # without build / tag_id
        build = None
        tag_id = None
        state = koji.BUILD_STATES['CANCELED']

        emails = kojihub.get_notification_recipients(build, tag_id, state)
        self.assertEqual(emails, [])

        # only query to watchers
        self.assertEqual(len(self.queries), 1)
        q = self.queries[0]
        self.assertEqual(q.columns, ['email'])
        self.assertEqual(q.tables, ['build_notifications'])
        self.assertEqual(q.clauses, ['package_id IS NULL',
                                     'status = %(users_status)i',
                                     'success_only = FALSE',
                                     'tag_id IS NULL',
                                     'usertype IN %(users_usertypes)s'])
        self.assertEqual(q.joins, ['JOIN users ON build_notifications.user_id = users.id'])
        self.assertEqual(q.values['state'], state)
        self.assertEqual(q.values['build'], build)
        self.assertEqual(q.values['tag_id'], tag_id)
        readPackageList.assert_not_called()


        ### with build without tag
        build = {'package_id': 12345, 'owner_name': 'owner_name'}
        self.queries = []

        emails = kojihub.get_notification_recipients(build, tag_id, state)
        self.assertEqual(emails, ['owner_name@test.domain.com'])

        # there should be only query to watchers
        self.assertEqual(len(self.queries), 1)
        q = self.queries[0]
        self.assertEqual(q.columns, ['email'])
        self.assertEqual(q.tables, ['build_notifications'])
        self.assertEqual(q.clauses, ['package_id = %(package_id)i OR package_id IS NULL',
                                     'status = %(users_status)i',
                                     'success_only = FALSE',
                                     'tag_id IS NULL',
                                     'usertype IN %(users_usertypes)s'])
        self.assertEqual(q.joins, ['JOIN users ON build_notifications.user_id = users.id'])
        self.assertEqual(q.values['package_id'], build['package_id'])
        self.assertEqual(q.values['state'], state)
        self.assertEqual(q.values['build'], build)
        self.assertEqual(q.values['tag_id'], tag_id)
        readPackageList.assert_not_called()

        ### with tag without build makes no sense
        build = None
        tag_id = 123
        self.queries = []

        with self.assertRaises(koji.GenericError):
            kojihub.get_notification_recipients(build, tag_id, state)
        self.assertEqual(self.queries, [])
        readPackageList.assert_not_called()


        ### with tag and build
        build = {'package_id': 12345, 'owner_name': 'owner_name'}
        tag_id = 123
        self.queries = []
        readPackageList.return_value = {12345: {'blocked': False, 'owner_id': 'owner_id'}}
        get_user.return_value = {
            'id': 'owner_id',
            'name': 'pkg_owner_name',
            'status': koji.USER_STATUS['NORMAL'],
            'usertype': koji.USERTYPES['NORMAL']
        }

        emails = kojihub.get_notification_recipients(build, tag_id, state)
        self.assertEqual(sorted(emails), ['owner_name@test.domain.com', 'pkg_owner_name@test.domain.com'])


        # there should be only query to watchers
        self.assertEqual(len(self.queries), 1)
        q = self.queries[0]
        self.assertEqual(q.columns, ['email'])
        self.assertEqual(q.tables, ['build_notifications'])
        self.assertEqual(q.clauses, ['package_id = %(package_id)i OR package_id IS NULL',
                                     'status = %(users_status)i',
                                     'success_only = FALSE',
                                     'tag_id = %(tag_id)i OR tag_id IS NULL',
                                     'usertype IN %(users_usertypes)s'])
        self.assertEqual(q.joins, ['JOIN users ON build_notifications.user_id = users.id'])
        self.assertEqual(q.values['package_id'], build['package_id'])
        self.assertEqual(q.values['state'], state)
        self.assertEqual(q.values['build'], build)
        self.assertEqual(q.values['tag_id'], tag_id)
        readPackageList.assert_called_once_with(pkgID=build['package_id'], tagID=tag_id, inherit=True)
        get_user.asssert_called_once_with('owner_id', strict=True)

        # blocked package owner
        get_user.return_value = {
            'id': 'owner_id',
            'name': 'pkg_owner_name',
            'status': koji.USER_STATUS['BLOCKED'],
            'usertype': koji.USERTYPES['NORMAL']
        }
        emails = kojihub.get_notification_recipients(build, tag_id, state)
        self.assertEqual(emails, ['owner_name@test.domain.com'])

        # package owner is machine
        get_user.return_value = {
            'id': 'owner_id',
            'name': 'pkg_owner_name',
            'status': koji.USER_STATUS['NORMAL'],
            'usertype': koji.USERTYPES['HOST']
        }
        emails = kojihub.get_notification_recipients(build, tag_id, state)
        self.assertEqual(emails, ['owner_name@test.domain.com'])

    #####################
    # Create notification

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 1}
        self.exports.getUser.return_value = {'id': 2, 'name': 'username'}
        self.exports.hasPerm.return_value = True
        get_package_id.return_value = package_id
        get_tag_id.return_value = tag_id
        get_build_notifications.return_value = []

        r = self.exports.createNotification(user_id, package_id, tag_id, success_only)
        self.assertEqual(r, None)

        self.exports.getLoggedInUser.assert_called_once()
        self.exports.getUser.asssert_called_once_with(user_id)
        self.exports.hasPerm.asssert_called_once_with('admin')
        get_package_id.assert_called_once_with(package_id, strict=True)
        get_tag_id.assert_called_once_with(tag_id, strict=True)
        get_build_notifications.assert_called_once_with(2)
        self.assertEqual(len(self.inserts), 1)
        insert = self.inserts[0]
        self.assertEqual(insert.table, 'build_notifications')
        self.assertEqual(insert.data, {
            'package_id': package_id,
            'user_id': 2,
            'tag_id': tag_id,
            'success_only': success_only,
            'email': 'username@test.domain.com',
        })
        self.assertEqual(insert.rawdata, {})

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification_unauthentized(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.createNotification(user_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification_invalid_user(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 2
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 1}
        self.exports.getUser.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.createNotification(user_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification_no_perm(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 2
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 1, 'name': 'a'}
        self.exports.getUser.return_value = {'id': 2, 'name': 'b'}
        self.exports.hasPerm.return_value = False

        with self.assertRaises(koji.GenericError):
            self.exports.createNotification(user_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification_invalid_pkg(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 2
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 2, 'name': 'a'}
        self.exports.getUser.return_value = {'id': 2, 'name': 'a'}
        get_package_id.side_effect = ValueError

        with self.assertRaises(ValueError):
            self.exports.createNotification(user_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification_invalid_tag(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 2
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 2, 'name': 'a'}
        self.exports.getUser.return_value = {'id': 2, 'name': 'a'}
        get_package_id.return_value = package_id
        get_tag_id.side_effect = ValueError

        with self.assertRaises(ValueError):
            self.exports.createNotification(user_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_createNotification_exists(self, get_package_id, get_tag_id,
            get_build_notifications):
        user_id = 2
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 2, 'name': 'a'}
        self.exports.getUser.return_value = {'id': 2, 'name': 'a'}
        get_package_id.return_value = package_id
        get_tag_id.return_value = tag_id
        get_build_notifications.return_value = [{
            'package_id': package_id,
            'tag_id': tag_id,
            'success_only': success_only,
        }]

        with self.assertRaises(koji.GenericError):
            self.exports.createNotification(user_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)

    #####################
    # Delete notification
    @mock.patch('kojihub._dml')
    def test_deleteNotification(self, _dml):
        user_id = 752
        n_id = 543
        self.exports.getBuildNotification.return_value = {'user_id': user_id}

        self.exports.deleteNotification(n_id)

        self.exports.getBuildNotification.assert_called_once_with(n_id)
        self.exports.getLoggedInUser.assert_called_once_with()
        _dml.assert_called_once()

    @mock.patch('kojihub._dml')
    def test_deleteNotification_missing(self, _dml):
        user_id = 752
        n_id = 543
        self.exports.getBuildNotification.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.deleteNotification(n_id)

        self.exports.getBuildNotification.assert_called_once_with(n_id)
        _dml.assert_not_called()

    @mock.patch('kojihub._dml')
    def test_deleteNotification_not_logged(self, _dml):
        user_id = 752
        n_id = 543
        self.exports.getBuildNotification.return_value = {'user_id': user_id}
        self.exports.getLoggedInUser.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.deleteNotification(n_id)

        self.exports.getBuildNotification.assert_called_once_with(n_id)
        _dml.assert_not_called()

    @mock.patch('kojihub._dml')
    def test_deleteNotification_no_perm(self, _dml):
        user_id = 752
        n_id = 543
        self.exports.getBuildNotification.return_value = {'user_id': user_id}
        self.exports.getLoggedInUser.return_value = {'id': 1}
        self.exports.hasPerm.return_value = False

        with self.assertRaises(koji.GenericError):
            self.exports.deleteNotification(n_id)

        self.exports.getBuildNotification.assert_called_once_with(n_id)
        _dml.assert_not_called()


    #####################
    # Update notification
    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_updateNotification(self, get_package_id, get_tag_id,
            get_build_notifications):
        n_id = 5432
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 1}
        self.exports.hasPerm.return_value = True
        get_package_id.return_value = package_id
        get_tag_id.return_value = tag_id
        get_build_notifications.return_value = [{
            'tag_id': tag_id,
            'user_id': user_id,
            'package_id': package_id,
            'success_only': not success_only,
        }]
        self.exports.getBuildNotification.return_value = {'user_id': user_id}

        r = self.exports.updateNotification(n_id, package_id, tag_id, success_only)
        self.assertEqual(r, None)

        self.exports.getLoggedInUser.assert_called_once()
        self.exports.hasPerm.asssert_called_once_with('admin')
        get_package_id.assert_called_once_with(package_id, strict=True)
        get_tag_id.assert_called_once_with(tag_id, strict=True)
        get_build_notifications.assert_called_once_with(user_id)
        self.assertEqual(len(self.inserts), 0)
        self.assertEqual(len(self.updates), 1)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_updateNotification_not_logged(self, get_package_id, get_tag_id,
            get_build_notifications):
        n_id = 5432
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.updateNotification(n_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)
        self.assertEqual(len(self.updates), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_updateNotification_missing(self, get_package_id, get_tag_id,
            get_build_notifications):
        n_id = 5432
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 1}
        self.exports.getBuildNotification.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.updateNotification(n_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)
        self.assertEqual(len(self.updates), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_updateNotification_no_perm(self, get_package_id, get_tag_id,
            get_build_notifications):
        n_id = 5432
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 132}
        self.exports.getBuildNotification.return_value = {'user_id': user_id}
        self.exports.hasPerm.return_value = False

        with self.assertRaises(koji.GenericError):
            self.exports.updateNotification(n_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)
        self.assertEqual(len(self.updates), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_updateNotification_exists(self, get_package_id, get_tag_id,
            get_build_notifications):
        n_id = 5432
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = {'id': 1}
        self.exports.hasPerm.return_value = True
        get_package_id.return_value = package_id
        get_tag_id.return_value = tag_id
        get_build_notifications.return_value = [{
            'tag_id': tag_id,
            'user_id': user_id,
            'package_id': package_id,
            'success_only': success_only,
        }]
        self.exports.getBuildNotification.return_value = {'user_id': user_id}

        with self.assertRaises(koji.GenericError):
            self.exports.updateNotification(n_id, package_id, tag_id, success_only)

        self.exports.getLoggedInUser.assert_called_once()
        self.exports.hasPerm.asssert_called_once_with('admin')
        get_package_id.assert_called_once_with(package_id, strict=True)
        get_tag_id.assert_called_once_with(tag_id, strict=True)
        get_build_notifications.assert_called_once_with(user_id)
        self.assertEqual(len(self.inserts), 0)
        self.assertEqual(len(self.updates), 0)

    @mock.patch('kojihub.get_build_notifications')
    @mock.patch('kojihub.get_tag_id')
    @mock.patch('kojihub.get_package_id')
    def test_updateNotification_not_logged(self, get_package_id, get_tag_id,
            get_build_notifications):
        n_id = 5432
        user_id = 1
        package_id = 234
        tag_id = 345
        success_only = True
        self.exports.getLoggedInUser.return_value = None

        with self.assertRaises(koji.GenericError):
            self.exports.updateNotification(n_id, package_id, tag_id, success_only)

        self.assertEqual(len(self.inserts), 0)
        self.assertEqual(len(self.updates), 0)
