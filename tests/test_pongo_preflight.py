"""Preflight checks the library without interacting with USB devices."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from orchard_cli.transports import pongo


class PongoPreflightTests(unittest.TestCase):
    def test_preflight_binds_symbols_without_initializing_usb(self):
        library = MagicMock()
        with patch.object(pongo, 'library_path', return_value=library):
            pongo.preflight()
        library.libusb_init.assert_not_called()
        library.libusb_get_device_list.assert_not_called()
        library.libusb_open.assert_not_called()

    def test_preflight_rejects_a_missing_library(self):
        with patch.object(pongo, 'find_library', return_value=None), \
             patch.object(pongo.shutil, 'which', return_value=None), \
             patch.object(pongo.C, 'CDLL', side_effect=OSError('not found')) as load, \
             self.assertRaisesRegex(pongo.PongoError, 'libusb-1.0 is unavailable'):
            pongo.preflight()
        load.assert_called()

    def test_preflight_rejects_an_incompatible_library(self):
        with patch.object(pongo, 'library_path', return_value=SimpleNamespace()), \
             self.assertRaisesRegex(pongo.PongoError, 'missing required symbol libusb_init'):
            pongo.preflight()


if __name__ == '__main__':
    unittest.main()
