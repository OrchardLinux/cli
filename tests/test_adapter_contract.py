"""Registry/adapter conformance checks, with no USB access or boot actions."""
import unittest
from unittest.mock import patch

import orchard_hardware

from orchard_cli.contracts import Adapter
from orchard_cli import devices
from orchard_cli.apple import device as adapter_module


class AdapterContract(unittest.TestCase):
    def test_every_described_device_satisfies_the_contract_without_discovery(self):
        self.assertIn('j71b', devices.profiles())
        with patch.object(adapter_module.usb, 'devices', side_effect=AssertionError('Unexpected USB access')):
            for profile in devices.profiles():
                with self.subTest(profile=profile):
                    adapter = devices.load(profile)
                    self.assertIsInstance(adapter, Adapter)
                    self.assertEqual(adapter.PROFILE, profile)
                    self.assertTrue(adapter.NAME)
                    self.assertIn(adapter.NAME, adapter.REBOOT_MESSAGE)

    def test_identity_comes_from_the_hardware_descriptor(self):
        adapter = devices.load('j71b')
        self.assertEqual((adapter.CHIP, adapter.BOARD, adapter.PRODUCT), (0x8010, 0x18, 'J71b Recovery'))
        self.assertTrue(adapter.INSTALL)

    def test_unknown_profile_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'not installed'):
            devices.load('no-such-board')

    def test_dfu_discovery_returns_explicit_resume_state_without_mutating_observation(self):
        adapter = devices.load('j71b')
        observed = {'mode': 'dfu', 'location': 'usb', 'ecid': 1,
                    'chip': adapter.CHIP, 'board': adapter.BOARD}
        with patch.object(adapter_module.usb, 'devices', return_value=[observed]):
            selected = adapter.discover()
        self.assertIs(selected['resumed'], False)
        self.assertEqual(selected['ecid'], 1)
        self.assertNotIn('resumed', observed)

    def test_choose_matches_the_connected_device_against_every_descriptor(self):
        adapter = devices.load('j71b')
        observed = {'mode': 'dfu', 'location': 'usb', 'ecid': 1,
                    'chip': adapter.CHIP, 'board': adapter.BOARD}
        with patch.object(adapter_module.usb, 'devices', return_value=[observed]), \
                patch('orchard_cli.apple.usb.devices', return_value=[observed]):
            loaded, selected = devices.choose()
        self.assertEqual(loaded.PROFILE, 'j71b')
        self.assertEqual(selected['ecid'], 1)

    def test_unknown_connected_device_is_named_in_the_error(self):
        observed = {'mode': 'dfu', 'location': 'usb', 'ecid': 1, 'chip': 0x8999, 'board': 0x7f}
        with patch('orchard_cli.apple.usb.devices', return_value=[observed]):
            with self.assertRaisesRegex(ValueError, 'not one Orchard supports'):
                devices.choose()

    def test_n61_uses_generic_adapter_and_keeps_installation_and_recovery_disabled(self):
        descriptor = {'device': {'profile': 'n61', 'name': 'iPhone 6', 'apple_products': ['iPhone7,2']},
                      'match': {'dfu': {'chip': 0x7000, 'board': 0x06}},
                      'firmware': {'recipe': 'firmware.json'},
                      'support': {'install': False, 'status': 'experimental'}}
        original = orchard_hardware.device
        observed = {'mode': 'dfu', 'location': 'usb', 'ecid': 1, 'chip': 0x7000, 'board': 0x06}
        with patch.object(orchard_hardware, 'profiles', return_value=['j71b', 'n61']), \
                patch.object(orchard_hardware, 'device', side_effect=lambda name:
                             descriptor if name == 'n61' else original(name)), \
                patch.object(adapter_module.usb, 'devices', return_value=[observed]):
            adapter, selected = devices.choose()
            self.assertEqual((adapter.PROFILE, adapter.APPLE_PRODUCT), ('n61', 'iPhone7,2'))
            self.assertFalse(adapter.INSTALL)
            self.assertIsNone(adapter.PRODUCT)
            self.assertFalse(selected['resumed'])
            self.assertEqual(selected['ecid'], observed['ecid'])
            with self.assertRaisesRegex(ValueError, 'no Orchard recovery system'):
                adapter.recovery_endpoint()
            observed['board'] = 0x07
            with self.assertRaisesRegex(ValueError, 'not one Orchard supports'):
                devices.choose()


if __name__ == '__main__':
    unittest.main()
