from __future__ import absolute_import

from six.moves import StringIO
import mock

import koji
from koji_cli.commands import handle_add_tag_inheritance
from . import utils


class TestAddTagInheritance(utils.CliTestCase):
    def setUp(self):
        self.options = mock.MagicMock()
        self.options.debug = False
        self.session = mock.MagicMock()
        self.session.getAPIVersion.return_value = koji.API_VERSION

    @mock.patch('sys.stderr', new_callable=StringIO)
    def test_add_tag_inheritance_without_option(self, stderr):
        expected = "Usage: %s add-tag-inheritance [options] <tag> <parent-tag>\n" \
                   "(Specify the --help global option for a list of other help options)\n\n" \
                   "%s: error: This command takes exctly two argument: " \
                   "a tag name or ID and that tag's new parent name " \
                   "or ID\n" % (self.progname, self.progname)
        with self.assertRaises(SystemExit) as ex:
            handle_add_tag_inheritance(self.options, self.session, [])
        self.assertExitCode(ex, 2)
        self.assert_console_message(stderr, expected)

    @mock.patch('sys.stderr', new_callable=StringIO)
    def test_add_tag_inheritance_non_exist_tag(self, stderr):
        tag = 'test-tag'
        parent_tag = 'parent-test-tag'
        expected = "Usage: %s add-tag-inheritance [options] <tag> <parent-tag>\n" \
                   "(Specify the --help global option for a list of other help options)\n\n" \
                   "%s: error: No such tag: %s\n" % (self.progname, self.progname, tag)
        self.session.getTag.return_value = None
        with self.assertRaises(SystemExit) as ex:
            handle_add_tag_inheritance(self.options, self.session, [tag, parent_tag])
        self.assertExitCode(ex, 2)
        self.assert_console_message(stderr, expected)

    @mock.patch('sys.stderr', new_callable=StringIO)
    def test_add_tag_inheritance_non_exist_parent_tag(self, stderr):
        side_effect_result = [{'arches': 'x86_64',
                               'extra': {},
                               'id': 1,
                               'locked': False,
                               'maven_include_all': False,
                               'maven_support': False,
                               'name': 'test-tag',
                               'perm': None,
                               'perm_id': None},
                              None]
        tag = 'test-tag'
        parent_tag = 'parent-test-tag'
        expected = "Usage: %s add-tag-inheritance [options] <tag> <parent-tag>\n" \
                   "(Specify the --help global option for a list of other help options)\n\n" \
                   "%s: error: No such tag: %s\n" % (self.progname, self.progname, parent_tag)
        self.session.getTag.side_effect = side_effect_result
        with self.assertRaises(SystemExit) as ex:
            handle_add_tag_inheritance(self.options, self.session, [tag, parent_tag])
        self.assertExitCode(ex, 2)
        self.assert_console_message(stderr, expected)
