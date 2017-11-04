import mock
import unittest

import kojihub


class TestPolicyData(unittest.TestCase):

    def setUp(self):
        self.get_build_target = mock.patch('kojihub.get_build_target').start()
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
        data = kojihub.channel_policy_data(method, arglist, opts)
        self.assertEqual(data, expect)
