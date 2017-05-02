import unittest


import StringIO as stringio

import os

import sys

import mock

from mock import call

from koji_cli.commands import handle_block_pkg

class TestBlockPkg(unittest.TestCase):

    # Show long diffs in error output...
    maxDiff = None

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    @mock.patch('koji_cli.commands.activate_session')
    def test_handle_block_pkg(self, activate_session_mock, stdout):
        tag = 'tag'
        dsttag = {'name': tag, 'id': 1}
        package = 'package'
        args = [tag, package]
        options = mock.MagicMock()

        # Mock out the xmlrpc server
        session = mock.MagicMock()

        session.getTag.return_value = dsttag
        session.listPackages.return_value = [
            {'package_name': package, 'package_id': 1}]
        # Run it and check immediate output
        # args: tag, package
        # expected: success
        rv = handle_block_pkg(options, session, args)
        actual = stdout.getvalue()
        expected = ''
        self.assertMultiLineEqual(actual, expected)
        # Finally, assert that things were called as we expected.
        activate_session_mock.assert_called_once_with(session, options)
        session.getTag.assert_called_once_with(tag)
        session.listPackages.assert_called_once_with(
            tagID=dsttag['id'], inherited=True)
        session.packageListBlock.assert_called_once_with(
            tag, package)
        session.multiCall.assert_called_once_with(strict=True)
        self.assertNotEqual(rv, 1)

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    @mock.patch('koji_cli.commands.activate_session')
    def test_handle_block_pkg_multi_pkg(self, activate_session_mock, stdout):
        tag = 'tag'
        dsttag = {'name': tag, 'id': 1}
        packages = ['package1', 'package2', 'package3']
        args = [tag] + packages
        options = mock.MagicMock()

        # Mock out the xmlrpc server
        session = mock.MagicMock()

        session.getTag.return_value = dsttag
        session.listPackages.return_value = [
            {'package_name': 'package1', 'package_id': 1},
            {'package_name': 'package2', 'package_id': 2},
            {'package_name': 'package3', 'package_id': 3},
            {'package_name': 'other_package', 'package_id': 4}
        ]
        # Run it and check immediate output
        # args: tag, package1, package2, package3
        # expected: success
        rv = handle_block_pkg(options, session, args)
        actual = stdout.getvalue()
        expected = ''
        self.assertMultiLineEqual(actual, expected)
        # Finally, assert that things were called as we expected.
        activate_session_mock.assert_called_once_with(session, options)
        self.assertEqual(
            session.mock_calls, [
                call.getTag(tag), call.listPackages(
                    tagID=dsttag['id'], inherited=True), call.packageListBlock(
                    tag, packages[0]), call.packageListBlock(
                    tag, packages[1]), call.packageListBlock(
                        tag, packages[2]), call.multiCall(
                            strict=True)])
        self.assertNotEqual(rv, 1)

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    @mock.patch('koji_cli.commands.activate_session')
    def test_handle_block_pkg_no_package(self, activate_session_mock, stdout):
        tag = 'tag'
        dsttag = {'name': tag, 'id': 1}
        packages = ['package1', 'package2', 'package3']
        args = [tag] + packages
        options = mock.MagicMock()

        # Mock out the xmlrpc server
        session = mock.MagicMock()

        session.getTag.return_value = dsttag
        session.listPackages.return_value = [
            {'package_name': 'package1', 'package_id': 1},
            {'package_name': 'package3', 'package_id': 3},
            {'package_name': 'other_package', 'package_id': 4}]
        # Run it and check immediate output
        # args: tag, package1, package2, package3
        # expected: failed: can not find package2 under tag
        rv = handle_block_pkg(options, session, args)
        actual = stdout.getvalue()
        expected = 'Package package2 doesn\'t exist in tag tag\n'
        self.assertMultiLineEqual(actual, expected)
        # Finally, assert that things were called as we expected.
        activate_session_mock.assert_called_once_with(session, options)
        session.getTag.assert_called_once_with(tag)
        session.listPackages.assert_called_once_with(
            tagID=dsttag['id'], inherited=True)
        session.packageListBlock.assert_not_called()
        session.multiCall.assert_not_called()
        self.assertEqual(rv, 1)

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    @mock.patch('koji_cli.commands.activate_session')
    def test_handle_block_pkg_tag_no_exists(
            self, activate_session_mock, stdout):
        tag = 'tag'
        dsttag = None
        packages = ['package1', 'package2', 'package3']
        args = [tag] + packages
        options = mock.MagicMock()

        # Mock out the xmlrpc server
        session = mock.MagicMock()

        session.getTag.return_value = dsttag
        # Run it and check immediate output
        # args: tag, package1, package2, package3
        # expected: failed: tag does not exist
        rv = handle_block_pkg(options, session, args)
        actual = stdout.getvalue()
        expected = 'No such tag: tag\n'
        self.assertMultiLineEqual(actual, expected)
        # Finally, assert that things were called as we expected.
        activate_session_mock.assert_called_once_with(session, options)
        session.getTag.assert_called_once_with(tag)
        session.listPackages.assert_not_called()
        session.packageListBlock.assert_not_called()
        self.assertEqual(rv, 1)

    @mock.patch('sys.stdout', new_callable=stringio.StringIO)
    @mock.patch('sys.stderr', new_callable=stringio.StringIO)
    @mock.patch('koji_cli.commands.activate_session')
    def test_handle_block_pkg_help(
            self, activate_session_mock, stderr, stdout):
        args = []
        options = mock.MagicMock()

        progname = os.path.basename(sys.argv[0]) or 'koji'

        # Mock out the xmlrpc server
        session = mock.MagicMock()

        # Run it and check immediate output
        with self.assertRaises(SystemExit) as cm:
            handle_block_pkg(options, session, args)
        actual_stdout = stdout.getvalue()
        actual_stderr = stderr.getvalue()
        expected_stdout = ''
        expected_stderr = """Usage: %s block-pkg [options] tag package [package2 ...]
(Specify the --help global option for a list of other help options)

%s: error: Please specify a tag and at least one package
""" % (progname, progname)
        self.assertMultiLineEqual(actual_stdout, expected_stdout)
        self.assertMultiLineEqual(actual_stderr, expected_stderr)

        # Finally, assert that things were called as we expected.
        activate_session_mock.assert_not_called()
        session.getTag.assert_not_called()
        session.listPackages.assert_not_called()
        session.packageListBlock.assert_not_called()
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
