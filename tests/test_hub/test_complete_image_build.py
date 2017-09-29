import copy
import json
import mock
import os
import os.path
import shutil
import tempfile
import unittest

import koji
import koji.util
import kojihub


orig_import_archive_internal = kojihub.import_archive_internal
IP = kojihub.InsertProcessor
UP = kojihub.UpdateProcessor


class TestCompleteImageBuild(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.pathinfo = koji.PathInfo(self.tempdir)
        mock.patch('koji.pathinfo', new=self.pathinfo).start()
        self.hostcalls = kojihub.HostExports()
        self.context = mock.patch('kojihub.context').start()
        mock.patch('kojihub.Host').start()
        self.Task = mock.patch('kojihub.Task').start()
        self.Task.return_value.assertHost = mock.MagicMock()
        self.get_build = mock.patch('kojihub.get_build').start()
        mock.patch('kojihub.get_rpm', new=self.my_get_rpm).start()
        self.get_image_build = mock.patch('kojihub.get_image_build').start()
        mock.patch('kojihub.get_archive_type', new=self.my_get_archive_type).start()
        mock.patch('kojihub.lookup_name', new=self.my_lookup_name).start()
        mock.patch.object(kojihub.BuildRoot, 'load', new=self.my_buildroot_load).start()
        mock.patch('kojihub.import_archive_internal',
                    new=self.my_import_archive_internal).start()
        self._dml = mock.patch('kojihub._dml').start()
        mock.patch('kojihub.build_notification').start()
        mock.patch('kojihub.assert_policy').start()
        mock.patch('kojihub.check_volume_policy',
                return_value={'id':0, 'name': 'DEFAULT'}).start()
        self.set_up_callbacks()
        self.rpms = {}
        mock.patch('kojihub.InsertProcessor', new=self.get_insert).start()
        mock.patch('kojihub.UpdateProcessor', new=self.get_update).start()
        self.inserts = []
        self.updates = []
        mock.patch('kojihub.nextval', new=self.my_nextval).start()
        self.sequences = {}

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tempdir)

    def get_insert(self, *a, **kw):
        insert = IP(*a, **kw)
        insert.execute = mock.MagicMock()
        self.inserts.append(insert)
        return insert

    def get_update(self, *a, **kw):
        update = UP(*a, **kw)
        update.execute = mock.MagicMock()
        self.updates.append(update)
        return update

    def set_up_files(self, name):
        datadir = os.path.join(os.path.dirname(__file__), 'data/image', name)
        # load image result data for our test build
        data = json.load(file(datadir + '/data.json'))
        self.db_expect = json.load(file(datadir + '/db.json'))
        for arch in data:
            taskdir = koji.pathinfo.task(data[arch]['task_id'])
            os.makedirs(taskdir)
            filenames = data[arch]['files'] +  data[arch]['logs']
            for filename in filenames:
                path = os.path.join(taskdir, filename)
                with file(path, 'w') as fp:
                    fp.write('Test file for %s\n%s\n' % (arch, filename))
        self.image_data = data

    def get_expected_files(self, buildinfo):
        data = self.image_data
        imgdir = koji.pathinfo.imagebuild(buildinfo)
        logdir = koji.pathinfo.build_logs(buildinfo)
        paths = []
        for arch in data:
            for filename in data[arch]['files']:
                paths.append(os.path.join(imgdir, filename))
            for filename in data[arch]['logs']:
                paths.append(os.path.join(logdir, 'image', filename))
        return paths

    def my_nextval(self, sequence):
        self.sequences.setdefault(sequence, 1000)
        self.sequences[sequence] += 1
        return self.sequences[sequence]

    def my_get_rpm(self, rpminfo, **kw):
        key = '%(name)s-%(version)s-%(release)s.%(arch)s' % rpminfo
        ret = self.rpms.get(key)
        if ret is not None:
            return ret
        ret = rpminfo.copy()
        ret['id'] = len(self.rpms) + 1000
        self.rpms[key] = rpminfo
        return ret

    def my_lookup_name(self, table, info, **kw):
        if table == 'btype':
            return {
                    'id': 'BTYPEID:%s' % info,
                    'name': 'BTYPE:%s' % info,
                    }
        else:
            raise Exception("Cannot fake call")

    def my_get_archive_type(self, *a, **kw):
        return dict.fromkeys(['id', 'name', 'description', 'extensions'],
                'ARCHIVETYPE')

    @staticmethod
    def my_buildroot_load(br, id):
        # br is the BuildRoot instance
        br.id = id
        br.is_standard = True
        br.data = {
                'br_type': koji.BR_TYPES['STANDARD'],
                'id': id,
                }

    def my_import_archive_internal(self, *a, **kw):
        # this is kind of odd, but we need this to fake the archiveinfo
        share = {}
        old_ip = kojihub.InsertProcessor
        def my_ip(table, *a, **kw):
            if table == 'archiveinfo':
                data = kw['data']
                data.setdefault('archive_id', 'ARCHIVE_ID')
                share['archiveinfo'] = data
                # TODO: need to add id
            return old_ip(table, *a, **kw)
        def my_ga(archive_id, **kw):
            return share['archiveinfo']
        with mock.patch('kojihub.InsertProcessor', new=my_ip), \
                    mock.patch('kojihub.get_archive', new=my_ga):
            return orig_import_archive_internal(*a, **kw)

    def set_up_callbacks(self):
        new_callbacks = copy.deepcopy(koji.plugin.callbacks)
        mock.patch('koji.plugin.callbacks', new=new_callbacks).start()
        self.callbacks = []
        for cbtype in koji.plugin.callbacks.keys():
            koji.plugin.register_callback(cbtype, self.callback)

    def callback(self, cbtype, *args, **kwargs):
        self.callbacks.append([cbtype, args, kwargs])

    def test_complete_image_build(self):
        self.set_up_files('import_1')
        buildinfo = {
                'id': 137,
                'task_id': 'TASK_ID',
                'name': 'some-image',
                'version': '1.2.3.4',
                'release': '3',
                'source': None,
                'state': koji.BUILD_STATES['BUILDING'],
                'volume_id': 0,
                }
        image_info = {'build_id': buildinfo['id']}
        self.get_build.return_value = buildinfo
        self.get_image_build.return_value = image_info

        # run the import call
        self.hostcalls.completeImageBuild('TASK_ID', 'BUILD_ID', self.image_data)

        # make sure we wrote the files we expect
        expected = self.get_expected_files(buildinfo)
        files = []
        for dirpath, dirnames, filenames in os.walk(self.tempdir + '/packages'):
            files.extend([os.path.join(dirpath, fn) for fn in filenames])
        self.assertEqual(set(files), set(expected))

        # check callbacks
        cbtypes = [c[0] for c in self.callbacks]
        cb_expect = [
            'preImport',    # build
            'preImport',    # archive 1...
            'postImport',
            'preImport',    # archive 2...
            'postImport',
            'preImport',    # archive 3...
            'postImport',
            'preImport',    # archive 4...
            'postImport',
            'preImport',    # archive 5...
            'postImport',
            'postImport',   # build
            'preBuildStateChange',  # building -> completed
            'postBuildStateChange',
            ]
        self.assertEqual(cbtypes, cb_expect)
        cb_idx = {}
        for c in self.callbacks:
            # no callbacks should use *args
            self.assertEqual(c[1], ())
            cbtype = c[0]
            if 'type' in c[2]:
                key = "%s:%s" % (cbtype, c[2]['type'])
            else:
                key = cbtype
            cb_idx.setdefault(key, [])
            cb_idx[key].append(c[2])
        key_expect = [
                'postBuildStateChange', 'preBuildStateChange',
                'preImport:archive', 'postImport:archive',
                'preImport:image', 'postImport:image',
                ]
        self.assertEqual(set(cb_idx.keys()), set(key_expect))
        for key in ['preImport:image']:
            callbacks = cb_idx[key]
            self.assertEqual(len(callbacks), 1)
            for cbargs in callbacks:
                keys = set(cbargs.keys())
                k_expect = set(['type', 'image'])
                self.assertEqual(keys, k_expect)
                self.assertEqual(cbargs['type'], 'image')
        for key in ['postImport:image']:
            callbacks = cb_idx[key]
            self.assertEqual(len(callbacks), 1)
            for cbargs in callbacks:
                keys = set(cbargs.keys())
                k_expect = set(['type', 'image', 'build', 'fullpath'])
                self.assertEqual(keys, k_expect)
                self.assertEqual(cbargs['type'], 'image')
                self.assertEqual(cbargs['build'], buildinfo)
        for key in ['preImport:archive', 'postImport:archive']:
            callbacks = cb_idx[key]
            self.assertEqual(len(callbacks), 5)
            for cbargs in callbacks:
                keys = set(cbargs.keys())
                k_expect = set(['filepath', 'build_type', 'build', 'fileinfo', 'type', 'archive'])
                self.assertEqual(keys, k_expect)
                self.assertEqual(cbargs['type'], 'archive')
                self.assertEqual(cbargs['build'], buildinfo)

        # db operations
        # with our other mocks, we should never reach _dml
        self._dml.assert_not_called()
        inserts = []
        for insert in self.inserts:
            info = [str(insert), insert.data, insert.rawdata]
            inserts.append(info)
        updates = []
        for update in self.updates:
            info = [str(update), update.data, update.rawdata]
            updates.append(info)
        data = {'inserts': inserts, 'updates': updates}
        self.assertEqual(data, self.db_expect)
