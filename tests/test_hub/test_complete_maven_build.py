import json
import mock
import os
import os.path
import shutil
import tempfile
import unittest

import koji
import kojihub


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
        mock.patch('kojihub._dml').start()
        mock.patch('kojihub._fetchSingle').start()

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
        self.maven_data = data

    def my_lookup_name(self, table, info, **kw):
        if table == 'btype':
            return mock.MagicMock()
        else:
            raise Exception("Cannot fake call")

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




