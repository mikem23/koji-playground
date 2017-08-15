import copy
import mock
import shutil
import tempfile
import unittest

import koji
import kojihub


class TestImportRPM(unittest.TestCase):
    def setUp(self):
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

    def test_nonexistant_rpm(self):
        with self.assertRaises(koji.GenericError):
            kojihub.import_rpm("this does not exist")

    @mock.patch('kojihub.get_build')
    @mock.patch('koji.get_rpm_header')
    def test_import_rpm_failed_build(self, get_rpm_header, get_build):
        get_rpm_header.return_value = self.rpm_header_retval
        get_build.return_value = {
            'state': koji.BUILD_STATES['FAILED'],
            'name': 'name',
            'version': 'version',
            'release': 'release',
        }
        with self.assertRaises(koji.GenericError):
            kojihub.import_rpm(self.filename)

    @mock.patch('kojihub.new_typed_build')
    @mock.patch('kojihub._dml')
    @mock.patch('kojihub._singleValue')
    @mock.patch('kojihub.get_build')
    @mock.patch('koji.get_rpm_header')
    def test_import_rpm_completed_build(self, get_rpm_header, get_build,
                                        _singleValue, _dml,
                                        new_typed_build):
        get_rpm_header.return_value = self.rpm_header_retval
        get_build.return_value = {
            'state': koji.BUILD_STATES['COMPLETE'],
            'name': 'name',
            'version': 'version',
            'release': 'release',
            'id': 12345,
        }
        _singleValue.return_value = 9876
        kojihub.import_rpm(self.filename)
        fields = [
            'build_id',
            'name',
            'arch',
            'buildtime',
            'payloadhash',
            'epoch',
            'version',
            'buildroot_id',
            'release',
            'external_repo_id',
            'id',
            'size',
        ]
        statement = 'INSERT INTO rpminfo (%s) VALUES (%s)' % (
            ", ".join(fields),
            ", ".join(['%%(%s)s' % field for field in fields])
        )
        values = {
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
            'id': 9876,
            'size': 0,
        }
        _dml.assert_called_once_with(statement, values)

    @mock.patch('kojihub.new_typed_build')
    @mock.patch('kojihub._dml')
    @mock.patch('kojihub._singleValue')
    @mock.patch('kojihub.get_build')
    @mock.patch('koji.get_rpm_header')
    def test_import_rpm_completed_source_build(self, get_rpm_header, get_build,
                                               _singleValue, _dml,
                                               new_typed_build):
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
        kojihub.import_rpm(self.src_filename)
        fields = [
            'build_id',
            'name',
            'arch',
            'buildtime',
            'payloadhash',
            'epoch',
            'version',
            'buildroot_id',
            'release',
            'external_repo_id',
            'id',
            'size',
        ]
        statement = 'INSERT INTO rpminfo (%s) VALUES (%s)' % (
            ", ".join(fields),
            ", ".join(['%%(%s)s' % field for field in fields])
        )
        values = {
            'build_id': 12345,
            'name': 'name',
            'arch': 'src',
            'buildtime': 'buildtime',
            'payloadhash': '7061796c6f61642068617368',
            'epoch': 'epoch',
            'version': 'version',
            'buildroot_id': None,
            'release': 'release',
            'external_repo_id': 0,
            'id': 9876,
            'size': 0,
        }
        _dml.assert_called_once_with(statement, values)


QP = kojihub.QueryProcessor


class TestImportBuild(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.queries = []
        self.QueryProcessor = mock.patch('kojihub.QueryProcessor',
                side_effect=self.get_query).start()
        self.importer = mock.MagicMock()
        self.CG_Importer = mock.patch('kojihub.CG_Importer',
                new=self.importer).start()
        self.Task_getInfo = mock.MagicMock()
        self.Task = mock.patch('kojihub.Task', new=self.Task_getInfo).start()
        self.get_header_fields = mock.patch('koji.get_header_fields').start()
        self.exports = kojihub.RootExports()
        self.setup_rpm()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tempdir)

    def get_query(self, *args, **kwargs):
        query = QP(*args, **kwargs)
        query.execute = mock.MagicMock()
        self.queries.append(query)
        return query

    def setup_rpm(self):
        self.filename = self.tempdir + "/name-version-release.arch.rpm"
        # Touch a file
        with open(self.filename, 'w'):
            pass
        self.src_filename = self.tempdir + "/name-version-release.src.rpm"
        # Touch a file
        with open(self.src_filename, 'w'):
            pass

        self.get_header_fields.return_value = {
            'sourcepackage': 1,
            'name': 'name',
            'version': 'version',
            'release': 'release',
            'arch': 'arch',
            'epoch': 'epoch',
            'buildtime': 'buildtime',
            'sigmd5': 'sigmd5',
        }

    def test_import_build_completed_build(self):

        kojihub.import_build(self.src_filename, [self.filename])
