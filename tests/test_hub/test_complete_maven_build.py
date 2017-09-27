import json
import mock
import os
import os.path
import shutil
import tempfile
import unittest

import koji
import kojihub


orig_import_archive_internal = kojihub.import_archive_internal


class TestCompleteMavenBuild(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.pathinfo = koji.PathInfo(self.tempdir)
        mock.patch('koji.pathinfo', new=self.pathinfo).start()
        self.hostcalls = kojihub.HostExports()
        self.context = mock.patch('kojihub.context').start()
        self.context.opts = {'EnableMaven': True}
        mock.patch('kojihub.Host').start()
        self.Task = mock.patch('kojihub.Task').start()
        self.Task.return_value.assertHost = mock.MagicMock()
        self.get_build = mock.patch('kojihub.get_build').start()
        self.get_maven_build = mock.patch('kojihub.get_maven_build').start()
        self.get_archive_type = mock.patch('kojihub.get_archive_type').start()
        mock.patch('kojihub.lookup_name', new=self.my_lookup_name).start()
        mock.patch('kojihub.import_archive_internal',
                    new=self.my_import_archive_internal).start()
        mock.patch('kojihub._dml').start()
        mock.patch('kojihub._fetchSingle').start()
        mock.patch('kojihub.build_notification').start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tempdir)

    def set_up_files(self, name):
        datadir = os.path.join(os.path.dirname(__file__), 'data/maven', name)
        # load maven result data for our test build
        data = json.load(file(datadir + '/data.json'))
        data['task_id'] = 9999
        taskdir = koji.pathinfo.task(data['task_id'])
        for subdir in data['files']:
            path = os.path.join(taskdir, subdir)
            os.makedirs(path)
            for fn in data['files'][subdir]:
                src = os.path.join(datadir, subdir, fn)
                dst = os.path.join(path, fn)
                shutil.copy(src, dst)
        for fn in data['logs']:
            src = os.path.join(datadir, fn)
            dst = os.path.join(taskdir, fn)
            shutil.copy(src, dst)
        self.maven_data = data

    def my_lookup_name(self, table, info, **kw):
        if table == 'btype':
            return mock.MagicMock()
        else:
            raise Exception("Cannot fake call")

    def my_import_archive_internal(self, *a, **kw):
        # this is kind of odd, but we need this to fake the archiveinfo
        share = {}
        def my_ip(table, *a, **kw):
            if table == 'archiveinfo':
                share['archiveinfo'] = kw['data']
                # TODO: need to add id
            return mock.MagicMock()
        def my_ga(archive_id, **kw):
            return share['archiveinfo']
        with mock.patch('kojihub.InsertProcessor', new=my_ip), \
                    mock.patch('kojihub.get_archive', new=my_ga):
            orig_import_archive_internal(*a, **kw)

    def set_up_callbacks(self):
        new_callbacks = copy.deepcopy(koji.plugin.callbacks)
        mock.patch('koji.plugin.callbacks', new=new_callbacks).start()
        self.callbacks = []
        for cbtype in koji.plugin.callbacks.keys():
            koji.plugin.register_callback(cbtype, self.callback)

    def callback(self, cbtype, *args, **kwargs):
        self.callbacks.append([cbtype, args, kwargs])

    def test_complete_maven_build(self):
        self.set_up_files('import_1')
        buildinfo = koji.maven_info_to_nvr(self.maven_data['maven_info'])
        buildinfo['release'] = 1
        self.hostcalls.completeMavenBuild('TASK_ID', 'BUILD_ID', self.maven_data, None)
