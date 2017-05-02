import os
import unittest

import StringIO as stringio

import mock

import loadcli
cli = loadcli.cli


class TestListCommands(unittest.TestCase):

    def setUp(self):
        self.session = mock.MagicMock()
        self.original_parser = cli.OptionParser
        cli.OptionParser = mock.MagicMock()
        self.parser = cli.OptionParser.return_value
        self.options = mock.MagicMock(name='options')
        self.options.profile = 'koji'
        self.options.configFile = None
        self.options.topdir = None
        self.options.cert = None
        self.options.serverca = None
        self.options.pluginpath = None
        self.options.pkgurl = None
        self.args = mock.MagicMock()
        self.parser.parse_args.return_value = (self.options, self.args)

    def tearDown(self):
        cli.OptionParser = self.original_parser

    # Show long diffs in error output...
    maxDiff = None

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    def test_list_commands(self, stdout):
        stdout.seek(0)
        stdout.truncate(0)
        with self.assertRaises(SystemExit):
            cli.get_options()
        actual = stdout.getvalue()
        actual = actual.replace('nosetests', 'koji')
        filename = os.path.dirname(__file__) + '/data/list-commands.txt'
        with open(filename, 'rb') as f:
            expected = f.read().decode('ascii')
        self.assertMultiLineEqual(actual, expected)

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    def test_handle_admin_help(self, stdout):
        stdout.seek(0)
        stdout.truncate(0)
        self.options.admin = True
        cli.handle_help(self.options, self.session, self.args)
        actual = stdout.getvalue()
        actual = actual.replace('nosetests', 'koji')
        filename = os.path.dirname(__file__) + '/data/list-commands-admin.txt'
        with open(filename, 'rb') as f:
            expected = f.read().decode('ascii')
        self.assertMultiLineEqual(actual, expected)
