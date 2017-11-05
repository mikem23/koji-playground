import mock
import unittest

import kojihub


class TestPolicyData(unittest.TestCase):

    def setUp(self):
        self.get_build_target = mock.patch('kojihub.get_build_target').start()
        self.get_channel_id = mock.patch('kojihub.get_channel_id').start()
        self.opts = {
                'arch': 'noarch',
                'parent': None,
                'label': None,
                'owner': 1,
                }

    def tearDown(self):
        mock.patch.stopall()

    def test_policy_data_simple(self):
        method = 'some_method'
        arglist = [1,2,3]
        opts = self.opts.copy()
        expect = opts.copy()
        expect['user_id'] = opts['owner']
        expect['method'] = method
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)

    def test_policy_data_req(self):
        method = 'some_method'
        arglist = [1,2,3]
        opts = self.opts.copy()
        expect = opts.copy()
        expect['user_id'] = opts['owner']
        expect['method'] = method
        opts['channel'] = 'test'
        expect['req_channel'] = 'test'
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)

    def test_policy_data_build(self):
        method = 'build'
        arglist = [
                'SOURCE',
                'TARGET',
                {'skip_tag': True},
                ]
        opts = self.opts.copy()
        expect = opts.copy()
        expect['user_id'] = opts['owner']
        expect['method'] = method
        opts['channel'] = 'test'
        self.get_build_target.return_value = {'name': 'TARGET'}
        opts = self.opts.copy()
        expect['target'] = 'TARGET'
        expect['scratch'] = False
        expect['source'] = 'SOURCE'
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_called_with('TARGET', strict=True)

        # again with null target
        self.get_build_target.reset_mock()
        arglist[1] = None
        expect['target'] = None
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_not_called()

    def test_policy_data_livemedia(self):
        method = 'livemedia'
        buildopts = {}
        arglist = [
                'NAME',
                'VERSION',
                ['ARCH'],
                'TARGET',
                'KSFILE',
                buildopts,
                ]
        opts = self.opts.copy()
        expect = opts.copy()
        expect['user_id'] = opts['owner']
        expect['method'] = method
        opts['channel'] = 'test'
        self.get_build_target.return_value = {'name': 'TARGET'}
        opts = self.opts.copy()
        expect['target'] = 'TARGET'
        expect['scratch'] = False
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_called_with('TARGET', strict=True)

        # again with scratch
        self.get_build_target.reset_mock()
        buildopts['scratch'] = True
        expect['scratch'] = True
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_called_with('TARGET', strict=True)

    def test_policy_data_image(self):
        method = 'image'
        buildopts = {}
        arglist = [
                'NAME',
                'VERSION',
                ['ARCH'],
                'TARGET',
                'INST_TREE',
                buildopts,
                ]
        opts = self.opts.copy()
        expect = opts.copy()
        expect['user_id'] = opts['owner']
        expect['method'] = method
        opts['channel'] = 'test'
        self.get_build_target.return_value = {'name': 'TARGET'}
        opts = self.opts.copy()
        expect['target'] = 'TARGET'
        expect['scratch'] = False
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_called_with('TARGET', strict=True)

        # again with scratch
        self.get_build_target.reset_mock()
        buildopts['scratch'] = True
        expect['scratch'] = True
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_called_with('TARGET', strict=True)

    def test_policy_data_indirection(self):
        method = 'indirectionimage'
        buildopts = {'target': 'TARGET', 'scratch': False}
        arglist = [buildopts]
        opts = self.opts.copy()
        expect = opts.copy()
        expect['user_id'] = opts['owner']
        expect['method'] = method
        opts['channel'] = 'test'
        self.get_build_target.return_value = {'name': 'TARGET'}
        opts = self.opts.copy()
        expect['target'] = 'TARGET'
        expect['scratch'] = False
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
        self.get_build_target.assert_called_with('TARGET', strict=True)
