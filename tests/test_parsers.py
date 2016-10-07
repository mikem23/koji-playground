#!/usr/bin/python

"""Test the __init__.py module"""

import os
import rpm
import unittest

import koji

class INITTestCase(unittest.TestCase):
    """Main test case container"""

    def test_parse_NVR(self):
        """Test the parse_NVR method"""

        self.assertRaises(AttributeError, koji.parse_NVR, None)
        self.assertRaises(AttributeError, koji.parse_NVR, 1)
        self.assertRaises(AttributeError, koji.parse_NVR, {})
        self.assertRaises(AttributeError, koji.parse_NVR, [])
        self.assertRaises(koji.GenericError, koji.parse_NVR, "")
        self.assertRaises(koji.GenericError, koji.parse_NVR, "foo")
        self.assertRaises(koji.GenericError, koji.parse_NVR, "foo-1")
        self.assertRaises(koji.GenericError, koji.parse_NVR, "foo-1-")
        self.assertRaises(koji.GenericError, koji.parse_NVR, "foo--1")
        self.assertRaises(koji.GenericError, koji.parse_NVR, "--1")
        ret = koji.parse_NVR("foo-1-2")
        self.assertEqual(ret['name'], "foo")
        self.assertEqual(ret['version'], "1")
        self.assertEqual(ret['release'], "2")
        self.assertEqual(ret['epoch'], "")
        ret = koji.parse_NVR("12:foo-1-2")
        self.assertEqual(ret['name'], "foo")
        self.assertEqual(ret['version'], "1")
        self.assertEqual(ret['release'], "2")
        self.assertEqual(ret['epoch'], "12")

    def test_parse_NVRA(self):
        """Test the parse_NVRA method"""

        self.assertRaises(AttributeError, koji.parse_NVRA, None)
        self.assertRaises(AttributeError, koji.parse_NVRA, 1)
        self.assertRaises(AttributeError, koji.parse_NVRA, {})
        self.assertRaises(AttributeError, koji.parse_NVRA, [])
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo-1")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo-1-")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo--1")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "--1")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo-1-1")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo-1-1.")
        self.assertRaises(koji.GenericError, koji.parse_NVRA, "foo-1.-1")
        ret = koji.parse_NVRA("foo-1-2.i386")
        self.assertEqual(ret['name'], "foo")
        self.assertEqual(ret['version'], "1")
        self.assertEqual(ret['release'], "2")
        self.assertEqual(ret['epoch'], "")
        self.assertEqual(ret['arch'], "i386")
        self.assertEqual(ret['src'], False)
        ret = koji.parse_NVRA("12:foo-1-2.src")
        self.assertEqual(ret['name'], "foo")
        self.assertEqual(ret['version'], "1")
        self.assertEqual(ret['release'], "2")
        self.assertEqual(ret['epoch'], "12")
        self.assertEqual(ret['arch'], "src")
        self.assertEqual(ret['src'], True)


class HeaderTestCase(unittest.TestCase):
    rpm_path = os.path.join(os.path.dirname(__file__), 'data/rpms/test-deps-1-1.fc24.x86_64.rpm')

    def setUp(self):
        self.fd = open(self.rpm_path)

    def tearDown(self):
        self.fd.close()

    def test_get_rpm_header(self):
        self.assertRaises(IOError, koji.get_rpm_header, 'nonexistent_path')
        self.assertRaises(AttributeError, koji.get_rpm_header, None)
        self.assertIsInstance(koji.get_rpm_header(self.rpm_path), rpm.hdr)
        self.assertIsInstance(koji.get_rpm_header(self.fd), rpm.hdr)
        # TODO:
        # test ts

    def test_get_header_fields(self):
        # incorrect
        self.assertRaises(IOError, koji.get_header_fields, 'nonexistent_path', [])
        self.assertRaises(koji.GenericError, koji.get_header_fields, self.rpm_path, 'nonexistent_header')
        self.assertEqual(koji.get_header_fields(self.rpm_path, []), {})

        # correct
        self.assertEqual(['REQUIRES'], koji.get_header_fields(self.rpm_path, ['REQUIRES']).keys())
        self.assertEqual(['PROVIDES', 'REQUIRES'], sorted(koji.get_header_fields(self.rpm_path, ['REQUIRES', 'PROVIDES'])))
        hdr = koji.get_rpm_header(self.rpm_path)
        self.assertEqual(['REQUIRES'], koji.get_header_fields(hdr, ['REQUIRES']).keys())

if __name__ == '__main__':
    unittest.main()
