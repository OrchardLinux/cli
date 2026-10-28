"""Registry/adapter conformance checks, with no USB access or boot actions."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from orchard_cli.contracts import Adapter
from orchard_cli import devices
from orchard_cli.j71b import adapter

MEMBERS = ('PROFILE', 'NAME', 'REBOOT_MESSAGE', 'list_devices', 'discover', 'boot',
           'connect', 'connect_saved', 'forget_access', 'capture_inputs', 'firmware_inputs', 'prepare_mixin')


def module(**changes):
    values = {name:getattr(adapter,name) for name in MEMBERS}
    values.update(changes)
    return SimpleNamespace(**values)


class AdapterContract(unittest.TestCase):
    def test_registered_module_satisfies_the_contract_without_discovery(self):
        with patch.object(adapter, 'discover', side_effect=AssertionError('Unexpected USB access')):
            self.assertIs(devices.load('j71b'), adapter)
            self.assertIsInstance(adapter, Adapter)

    def test_missing_operation_is_rejected_not_filled_by_a_fallback(self):
        incomplete = module()
        del incomplete.connect_saved
        with patch.object(devices.importlib, 'import_module', return_value=incomplete):
            with self.assertRaisesRegex(ValueError, 'Incomplete adapter contract'):
                devices.load('j71b')

    def test_registry_profile_mismatch_is_rejected(self):
        with patch.object(devices.importlib, 'import_module', return_value=module(PROFILE='other')):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                devices.load('j71b')

    def test_invalid_presentation_metadata_is_rejected(self):
        for values in ({'NAME':''}, {'NAME':None}, {'REBOOT_MESSAGE':None}):
            with patch.object(devices.importlib, 'import_module', return_value=module(**values)):
                with self.assertRaisesRegex(ValueError, 'Invalid adapter presentation'):
                    devices.load('j71b')

    def test_dfu_discovery_returns_explicit_resume_state_without_mutating_observation(self):
        observed = {'mode':'dfu', 'location':'usb', 'ecid':1,
                    'chip':adapter.CHIP, 'board':adapter.BOARD}
        with patch.object(adapter.usb, 'devices', return_value=[observed]):
            selected = adapter.discover()
        self.assertIs(selected['resumed'], False)
        self.assertEqual(selected['ecid'], 1)
        self.assertNotIn('resumed', observed)

    def test_choose_passes_the_validated_selection_through_unchanged(self):
        selection = {'mode':'dfu','location':'usb','ecid':1,'resumed':False}
        implementation = module(discover=Mock(return_value=selection))
        with patch.object(devices, 'load', return_value=implementation), patch.object(devices, 'SUPPORTED', ('j71b',)):
            loaded, selected = devices.choose()
        self.assertIs(loaded, implementation)
        self.assertIs(selected, selection)
        implementation.discover.assert_called_once_with()


if __name__ == '__main__':unittest.main()
