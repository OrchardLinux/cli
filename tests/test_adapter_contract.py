"""Registry/adapter conformance checks, with no USB access or boot actions."""
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from orchard_cli.contracts import Adapter, UnsupportedDevice
from orchard_cli import devices
from orchard_cli.j71b import adapter

MEMBERS = ('PROFILE', 'NAME', 'REBOOT_MESSAGE', 'list_devices', 'discover', 'boot',
           'connect', 'connect_saved', 'forget_access', 'capture_adt', 'firmware_inputs', 'prepare_mixin')


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
        other = module(discover=Mock(side_effect=UnsupportedDevice('another model')))
        with patch.object(devices, 'SUPPORTED', ('first', 'second')), \
             patch.object(devices, 'load', side_effect=lambda profile: other if profile == 'first' else implementation):
            loaded, selected = devices.choose()
        self.assertIs(loaded, implementation)
        self.assertIs(selected, selection)
        implementation.discover.assert_called_once_with()

    def test_choose_reports_shared_errors_once_and_names_supported_devices(self):
        failing = module(discover=Mock(side_effect=ValueError('Connect exactly one target')))
        with patch.object(devices, 'SUPPORTED', ('first', 'second')), \
             patch.object(devices, 'load', return_value=failing):
            with self.assertRaisesRegex(ValueError, '^Connect exactly one target$'):
                devices.choose()
        refusing = module(NAME='Tablet', discover=Mock(side_effect=UnsupportedDevice('another model')))
        with patch.object(devices, 'SUPPORTED', ('first', 'second')), \
             patch.object(devices, 'load', return_value=refusing):
            with self.assertRaisesRegex(ValueError, 'supported: Tablet, First; Tablet, Second$'):
                devices.choose()
        accepting = module(discover=Mock(return_value={'mode':'dfu','location':'usb','ecid':1,'resumed':False}))
        with patch.object(devices, 'SUPPORTED', ('first', 'second')), \
             patch.object(devices, 'load', return_value=accepting):
            with self.assertRaisesRegex(ValueError, 'several adapters'):
                devices.choose()

    def test_j71b_refuses_other_models_without_claiming_an_error(self):
        for observed in ({'mode':'dfu', 'location':'usb', 'ecid':1, 'chip':0x8003, 'board':0x10},
                         {'mode':'linux', 'location':'usb', 'ecid':1, 'product':'Other Recovery',
                          'serial_port':'/dev/other', 'boot_id':'11111111-1111-4111-8111-111111111111'}):
            with self.subTest(mode=observed['mode']), patch.object(adapter.usb, 'devices', return_value=[observed]):
                with self.assertRaises(UnsupportedDevice):
                    adapter.discover()
        missing = {'mode':'dfu', 'location':'usb', 'ecid':None, 'chip':adapter.CHIP, 'board':adapter.BOARD}
        with patch.object(adapter.usb, 'devices', return_value=[missing]):
            with self.assertRaises(ValueError) as raised:
                adapter.discover()
            self.assertNotIsInstance(raised.exception, UnsupportedDevice)

    def test_device_listing_merges_observations_without_selection(self):
        observed = [{'mode':'dfu','location':'usb'}]
        implementation = module(list_devices=Mock(return_value=observed),
                                discover=Mock(side_effect=AssertionError('Unexpected discovery')))
        with patch.object(devices, 'SUPPORTED', ('first', 'second')), \
             patch.object(devices, 'load', return_value=implementation):
            self.assertEqual(devices.list_devices(), observed)

    def test_profile_selection_from_adt_model(self):
        def tree(model):
            name = b'name'.ljust(32, b'\0') + struct.pack('<I', 12) + b'device-tree\0'
            value = model + b'\0'
            prop = b'model'.ljust(32, b'\0') + struct.pack('<I', len(value)) + value + b'\0' * (-len(value) % 4)
            return struct.pack('<II', 2, 0) + name + prop
        self.assertEqual(devices.profile_for_adt(tree(b'iPad7,5')), 'j71b')
        with self.assertRaisesRegex(ValueError, 'specify --profile'):
            devices.profile_for_adt(tree(b'iPad1,1'))


if __name__ == '__main__':unittest.main()
