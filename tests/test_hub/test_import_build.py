import copy
import mock
import shutil
import tempfile
import unittest

import koji
import kojihub

IP = kojihub.InsertProcessor


class TestImportRPM(unittest.TestCase):
    def setUp(self):
        self.InsertProcessor = mock.patch('kojihub.InsertProcessor',
                                          side_effect=self.getInsert).start()
        self.inserts = []
        self.tempdir = tempfile.mkdtemp()
        self.filename = self.tempdir + "/name-version-release.arch.rpm"
        # Touch a file
        with open(self.filename, 'w'):
            pass
        self.src_filename = self.tempdir + "/name-version-release.src.rpm"
        # Touch a file
        with open(self.src_filename, 'w'):
            pass

        self.rpm_header_retval = {
            'filename': 'name-version-release.arch.rpm',
            1000: 'name',
            1001: 'version',
            1002: 'release',
            1003: 'epoch',
            1006: 'buildtime',
            1022: 'arch',
            1044: 'name-version-release.arch',
            1106: 'sourcepackage',
            261: 'payload hash',
        }

    def tearDown(self):
        shutil.rmtree(self.tempdir)
        mock.patch.stopall()


    def getInsert(self, *args, **kwargs):
        insert = IP(*args, **kwargs)
        insert.execute = mock.MagicMock()
        self.inserts.append(insert)
        return insert

    def test_nonexistant_rpm(self):
        with self.assertRaises(koji.GenericError):
            kojihub.import_rpm("this does not exist")

    @mock.patch('kojihub.lookup_namespace')
    @mock.patch('kojihub.get_build')
    @mock.patch('koji.get_rpm_header')
    def test_import_rpm_failed_build(self, get_rpm_header, get_build,
                lookup_namespace):
        get_rpm_header.return_value = self.rpm_header_retval
        get_build.return_value = {
            'state': koji.BUILD_STATES['FAILED'],
            'name': 'name',
            'version': 'version',
            'release': 'release',
        }
        with self.assertRaises(koji.GenericError):
            kojihub.import_rpm(self.filename)

    @mock.patch('kojihub.lookup_namespace')
    @mock.patch('kojihub.new_typed_build')
    @mock.patch('kojihub._dml')
    @mock.patch('kojihub._singleValue')
    @mock.patch('kojihub.get_build')
    @mock.patch('koji.get_rpm_header')
    def test_import_rpm_completed_build(self, get_rpm_header, get_build,
                _singleValue, _dml, new_typed_build, lookup_namespace):
        get_rpm_header.return_value = self.rpm_header_retval
        get_build.return_value = {
            'state': koji.BUILD_STATES['COMPLETE'],
            'name': 'name',
            'version': 'version',
            'release': 'release',
            'id': 12345,
        }
        _singleValue.return_value = 9876
        lookup_namespace.return_value = {'id': 0, 'name': 'DEFAULT'}
        kojihub.import_rpm(self.filename)

        data = {
            'build_id': 12345,
            'name': 'name',
            'arch': 'arch',
            'buildtime': 'buildtime',
            'payloadhash': '7061796c6f61642068617368',
            'epoch': 'epoch',
            'version': 'version',
            'buildroot_id': None,
            'release': 'release',
            'external_repo_id': 0,
            'namespace_id': 0,
            'id': 9876,
            'size': 0,
        }
        self.assertEqual(len(self.inserts), 1)
        insert = self.inserts[0]
        self.assertEqual(insert.table, 'rpminfo')
        self.assertEqual(insert.data, data)
        self.assertEqual(insert.rawdata, {})

    @mock.patch('kojihub.lookup_namespace')
    @mock.patch('kojihub.new_typed_build')
    @mock.patch('kojihub._dml')
    @mock.patch('kojihub._singleValue')
    @mock.patch('kojihub.get_build')
    @mock.patch('koji.get_rpm_header')
    def test_import_rpm_completed_source_build(self, get_rpm_header, get_build,
                _singleValue, _dml, new_typed_build, lookup_namespace):
        retval = copy.copy(self.rpm_header_retval)
        retval.update({
            'filename': 'name-version-release.arch.rpm',
            1044: 'name-version-release.src',
            1022: 'src',
            1106: 1,
        })
        get_rpm_header.return_value = retval
        get_build.return_value = {
            'state': koji.BUILD_STATES['COMPLETE'],
            'name': 'name',
            'version': 'version',
            'release': 'release',
            'id': 12345,
        }
        _singleValue.return_value = 9876
        lookup_namespace.return_value = {'id': 0, 'name': 'DEFAULT'}
        kojihub.import_rpm(self.src_filename)

        data = {
            'build_id': 12345,
            'name': 'name',
            'arch': 'src',
            'buildtime': 'buildtime',
            'payloadhash': '7061796c6f61642068617368',
            'epoch': 'epoch',
            'version': 'version',
            'buildroot_id': None,
            'release': 'release',
            'namespace_id': 0,
            'external_repo_id': 0,
            'id': 9876,
            'size': 0,
        }
        self.assertEqual(len(self.inserts), 1)
        insert = self.inserts[0]
        self.assertEqual(insert.table, 'rpminfo')
        self.assertEqual(insert.data, data)
        self.assertEqual(insert.rawdata, {})


class TestImportBuild(unittest.TestCase):
    def setUp(self):
        self.InsertProcessor = mock.patch('kojihub.InsertProcessor',
                                          side_effect=self.getInsert).start()
        self.inserts = []
        self.tempdir = tempfile.mkdtemp()
        self.filename = self.tempdir + "/name-version-release.arch.rpm"
        # Touch a file
        with open(self.filename, 'w'):
            pass
        self.src_filename = self.tempdir + "/name-version-release.src.rpm"
        # Touch a file
        with open(self.src_filename, 'w'):
            pass

        self.rpm_header_retval = {
            'filename': 'name-version-release.arch.rpm',
            1000: 'name',
            1001: 'version',
            1002: 'release',
            1003: 'epoch',
            1006: 'buildtime',
            1022: 'arch',
            1044: 'name-version-release.arch',
            1106: 'sourcepackage',
            261: 'payload hash',
        }

    def tearDown(self):
        shutil.rmtree(self.tempdir)
        mock.patch.stopall()

    def getInsert(self, *args, **kwargs):
        insert = IP(*args, **kwargs)
        insert.execute = mock.MagicMock()
        self.inserts.append(insert)
        return insert

    @mock.patch('kojihub.lookup_namespace')
    @mock.patch('kojihub.nextval')
    @mock.patch('kojihub.new_typed_build')
    @mock.patch('kojihub._dml')
    @mock.patch('kojihub._singleValue')
    @mock.patch('kojihub.get_build')
    @mock.patch('kojihub.add_rpm_sig')
    @mock.patch('koji.rip_rpm_sighdr')
    @mock.patch('kojihub.import_rpm_file')
    @mock.patch('kojihub.import_rpm')
    @mock.patch('kojihub.QueryProcessor')
    @mock.patch('kojihub.context')
    @mock.patch('kojihub.new_package')
    @mock.patch('koji.get_rpm_header')
    @mock.patch('koji.pathinfo.work')
    def test_import_build_completed_build(self, work, get_rpm_header,
                new_package, context, query, import_rpm, import_rpm_file,
                rip_rpm_sighdr, add_rpm_sig, get_build, _singleValue, _dml,
                new_typed_build, nextval, lookup_namespace):

        rip_rpm_sighdr.return_value = (0, 0)

        processor = mock.MagicMock()
        processor.executeOne.return_value = None
        query.return_value = processor

        context.session.user_id = 99

        work.return_value = '/'

        retval = copy.copy(self.rpm_header_retval)
        retval.update({
            'filename': 'name-version-release.arch.rpm',
            1044: 'name-version-release.src',
            1022: 'src',
            1106: 1,
        })
        get_rpm_header.return_value = retval
        binfo = {
            'state': koji.BUILD_STATES['COMPLETE'],
            'name': 'name',
            'version': 'version',
            'release': 'release',
            'id': 12345,
        }
        # get_build called once to check for existing,
        # then later to get the build info
        get_build.side_effect = [None, binfo]

        kojihub.import_build(self.src_filename, [self.filename])

        data = {
            'task_id': None,
            'extra': None,
            'start_time': 'NOW',
            'epoch': 'epoch',
            'completion_time': 'NOW',
            'state': 1,
            'version': 'version',
            'source': None,
            'volume_id': 0,
            'owner': 99,
            'release': 'release',
            'namespace_id': 0,
            'pkg_id': new_package.return_value,
            'id': nextval.return_value,
        }
        self.assertEqual(len(self.inserts), 1)
        insert = self.inserts[0]
        self.assertEqual(insert.table, 'build')
        self.assertEqual(insert.data, data)
        self.assertEqual(insert.rawdata, {})
