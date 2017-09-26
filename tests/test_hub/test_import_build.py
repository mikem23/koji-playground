import copy
import mock
import os.path
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
                return_value=self.importer).start()
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

    def test_import_build_simple(self):
        # just an srpm
        kojihub.import_build(self.src_filename, [])

    def test_import_build_not_srpm(self):
        self.get_header_fields.return_value['sourcepackage'] = 0
        with self.assertRaises(koji.GenericError):
            kojihub.import_build(self.src_filename, [self.filename])

    def test_import_build_completed(self):
        srpm = self.src_filename
        rpms = ['foo-1.1.noarch.rpm', 'foo-bar-1.1.noarch.rpm']
        brmap = dict.fromkeys(rpms + [srpm], 1001)
        task_id = 42
        build_id = 37
        logs = {'noarch': ['build.log']}
        kojihub.import_build(srpm, rpms, brmap, task_id, build_id, logs)

        self.CG_Importer.assert_called_once()
        self.importer.do_import.assert_called_once()
        metadata = self.importer.do_import.call_args[0][0]
        # print('')
        # import pprint; pprint.pprint(metadata)
        # TODO add assertions on metadata


orig_import_rpm = kojihub.import_rpm
orig_get_rpm = kojihub.get_rpm
orig_add_rpm_sig = kojihub.add_rpm_sig


class TestImportBuildCallbacks(unittest.TestCase):

    def setUp(self):
        # There is a lot of setup here because we're letting the code
        # run deep, rather than mocking at some of the easier places.
        # Otherwise, we will miss callbacks
        self.tempdir = tempfile.mkdtemp()
        self.queries = []
        self.QueryProcessor = mock.patch('kojihub.QueryProcessor',
                side_effect=self.get_query).start()
        self.Task = mock.patch('kojihub.Task').start()
        self.get_build = mock.patch('kojihub.get_build').start()
        self.asset_policy = mock.patch('kojihub.assert_policy').start()
        self.UpdateProcessor = mock.patch('kojihub.UpdateProcessor').start()
        mock.patch('kojihub.new_typed_build').start()
        mock.patch('kojihub._singleValue').start()
        mock.patch('kojihub._dml').start()
        mock.patch('kojihub.nextval', side_effect=range(10)).start()
        mock.patch('kojihub.import_rpm', new=self.my_import_rpm).start()
        mock.patch('kojihub.get_rpm', new=self.my_get_rpm).start()
        mock.patch('kojihub.add_rpm_sig', new=self.my_add_rpm_sig).start()
        self.rpm_idx = {}
        self.exports = kojihub.RootExports()
        self.set_up_files()
        self.set_up_callbacks()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tempdir)

    def set_up_callbacks(self):
        new_callbacks = copy.deepcopy(koji.plugin.callbacks)
        mock.patch('koji.plugin.callbacks', new=new_callbacks).start()
        self.callbacks = []
        for cbtype in koji.plugin.callbacks.keys():
            koji.plugin.register_callback(cbtype, self.callback)

    def callback(self, cbtype, *args, **kwargs):
        self.callbacks.append([cbtype, args, kwargs])

    def get_query(self, *args, **kwargs):
        query = QP(*args, **kwargs)
        query.execute = mock.MagicMock()
        self.queries.append(query)
        return query

    def my_import_rpm(self, *args, **kwargs):
        # wrap original, but index results
        ret = orig_import_rpm(*args, **kwargs)
        self.rpm_idx[ret['id']] = ret
        return ret

    def my_get_rpm(self, rpminfo, **kwargs):
        if rpminfo in self.rpm_idx:
            return self.rpm_idx[rpminfo]
        # otherwise
        orig_get_rpm(rpminfo, **kwargs)

    def my_add_rpm_sig(self, *args, **kwargs):
        # we want this mock only for this call
        with mock.patch('kojihub._fetchMulti', return_value=[]):
            orig_add_rpm_sig(*args, **kwargs)

    def set_up_files(self):
        self.pathinfo = koji.PathInfo(self.tempdir)
        mock.patch('koji.pathinfo', new=self.pathinfo).start()
        rpmdir = os.path.join(os.path.dirname(__file__), 'data/rpms')
        srpm = 'mytestpkg-1.1-10.src.rpm'
        rpms = ['mytestpkg-1.1-10.noarch.rpm', 'mytestpkg-doc-1.1-10.noarch.rpm']
        files = list(rpms)
        files.append(srpm)
        os.makedirs(self.pathinfo.work() + '/upload')
        for fn in files:
            src = os.path.join(rpmdir, fn)
            dst = self.pathinfo.work() + '/upload/' + fn
            shutil.copy(src, dst)
        self.src_filename = "upload/%s" % srpm
        self.filenames = ["upload/%s" % r for r in rpms]
        # also a log
        logname = 'upload/build.log'
        fn = os.path.join(self.pathinfo.work(), logname)
        with open(fn, 'w') as fp:
            fp.write('hello world!\n')
        self.logs = {'noarch': [logname]}

    def test_import_build_callbacks_completed(self):
        taskinfo = {'id': 42, 'start_ts': 1}
        self.Task.return_value.getInfo.return_value = taskinfo
        buildinfo = {'id': 37, 'name': 'mytestpkg', 'version': '1.1',
                'epoch': 7, 'release': '10',
                'state': koji.BUILD_STATES['BUILDING'],
                'task_id': taskinfo['id'], 'source': None}
        buildinfo['build_id'] = buildinfo['id']
        self.get_build.return_value = buildinfo
        srpm = self.src_filename
        rpms = self.filenames
        brmap = dict.fromkeys(rpms + [srpm], 1001)
        build_id = 37
        kojihub.import_build(srpm, rpms, brmap, taskinfo['id'], build_id, self.logs)

        # callback assertions
        cbtypes = [c[0] for c in self.callbacks]
        self.assertEqual(cbtypes, ['preImport', 'preBuildStateChange', 'postBuildStateChange', 'postImport'])
        for c in self.callbacks:
            # no callbacks should use *args
            self.assertEqual(c[1], ())
        cb_idx = dict([(c[0], c[2]) for c in self.callbacks])
        # in this case, pre and post data is similar
        for cbtype in ['preImport', 'postImport']:
            cbargs = cb_idx[cbtype]
            keys = sorted(cbargs.keys())
            self.assertEqual(keys, ['brmap', 'build', 'build_id', 'logs',
                    'rpms', 'srpm', 'task_id', 'type'])
            self.assertEqual(cbargs['type'], 'build')
            self.assertEqual(cbargs['srpm'], self.src_filename)
            self.assertEqual(cbargs['rpms'], [self.filename])
