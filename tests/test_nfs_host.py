"""N61 host-link selection and ownership checks use synthetic host fixtures."""
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchard_cli import nfs_host

ROOT = '11111111-1111-4111-8111-111111111111'
BOOT = '22222222-2222-4222-8222-222222222222'
LOCATION = 34734080
MAC = '02:00:0a:3d:00:01'
ROUTES = 'Destination Gateway Flags Netif Expire\ndefault 192.0.2.1 UG en0\n'
CONFIG = 'en0: flags=8863\n\tether aa:bb:cc:dd:ee:ff\n\tinet 192.0.2.10 netmask 0xffffff00\nen9: flags=8863\n\tether ' + MAC + '\n'
ASSIGNED = CONFIG + '\tinet 10.61.0.1 netmask 0xfffffffc\n'


def gadget():
    return {'IOObjectClass': 'IOUSBHostDevice', 'idVendor': 0x05ac, 'idProduct': 0x4144,
            'locationID': LOCATION, 'USB Product Name': 'Orchard n61 NFS',
            'USB Serial Number': 'PROFILE:n61 ROOT:' + ROOT + ' BOOT:' + BOOT,
            'IORegistryEntryChildren': [
                {'IOObjectClass': 'IOSerialBSDClient', 'IOCalloutDevice': '/dev/cu.usbmodem-test'},
                {'IOObjectClass': 'IOEthernetInterface', 'BSD Name': 'en9',
                 'IOMACAddress': bytes.fromhex('02000a3d0001')}]}


def data(*devices):
    return plistlib.dumps(list(devices))


class GadgetSelection(unittest.TestCase):
    def test_requires_selected_root_boot_and_unique_device(self):
        endpoint = nfs_host.select_macos(data(gadget()), LOCATION, ROOT, expected_boot=BOOT)
        self.assertEqual((endpoint['interface'], endpoint['mac'], endpoint['boot_id']), ('en9', MAC, BOOT))
        with self.assertRaisesRegex(ValueError, 'boot identity'):
            nfs_host.select_macos(data(gadget()), LOCATION, ROOT, expected_boot=ROOT)
        with self.assertRaisesRegex(ValueError, 'root identity'):
            nfs_host.select_macos(data(gadget()), LOCATION, BOOT)
        other = {'IOObjectClass': 'IOUSBHostDevice', 'idVendor': 0x05ac,
                 'idProduct': 0x1227, 'locationID': LOCATION + 1}
        with self.assertRaisesRegex(ValueError, 'exactly one boot device'):
            nfs_host.select_macos(data(gadget(), other), LOCATION, ROOT)

    def test_nested_device_cannot_supply_the_parents_serial_port(self):
        parent = gadget()
        parent['IORegistryEntryChildren'] = [parent['IORegistryEntryChildren'][1], {
            'IOObjectClass': 'IOUSBHostDevice', 'idVendor': 0x05ac, 'idProduct': 0x1000,
            'IORegistryEntryChildren': [{'IOObjectClass': 'IOSerialBSDClient',
                                         'IOCalloutDevice': '/dev/cu.unrelated'}]}]
        with self.assertRaisesRegex(ValueError, 'serial endpoint'):
            nfs_host.select_macos(data(parent), LOCATION, ROOT)

    def test_invalid_usb_tree_cannot_select_a_device(self):
        with self.assertRaisesRegex(ValueError, 'registry data'):
            nfs_host.select_macos(plistlib.dumps({'unexpected': True}), LOCATION, ROOT)
        with self.assertRaisesRegex(ValueError, 'registry tree'):
            nfs_host.select_macos(plistlib.dumps(['unexpected']), LOCATION, ROOT)


class HostLink(unittest.TestCase):
    def setUp(self):
        self.endpoint = nfs_host.select_macos(data(gadget()), LOCATION, ROOT, expected_boot=BOOT)
        self.owner = {'schema': 1, 'kind': 'orchard.n61-nfs-host-link', 'state': 'owned',
                      'address': '10.61.0.1/30', 'root_identity_sha256': 'a' * 64, **self.endpoint}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'link.json'

    def test_plans_owned_alias_and_rejects_foreign_addresses_and_routes(self):
        self.assertEqual(nfs_host.plan(self.endpoint, CONFIG, ROUTES)['action'], 'add-alias')
        with self.assertRaisesRegex(ValueError, 'without an ownership receipt'):
            nfs_host.plan(self.endpoint, ASSIGNED, ROUTES)
        own_route = ROUTES + '10.61.0/30 link#9 UCS en9\n'
        self.assertEqual(nfs_host.plan(self.endpoint, ASSIGNED, own_route, self.owner)['action'], 'already-owned')
        with self.assertRaisesRegex(ValueError, 'another session'):
            nfs_host.plan(self.endpoint, ASSIGNED, own_route, {**self.owner, 'boot_id': ROOT})
        with self.assertRaisesRegex(ValueError, 'route conflicts'):
            nfs_host.plan(self.endpoint, CONFIG, ROUTES + '10.61.0/30 link#8 UCS en8\n')
        with self.assertRaisesRegex(ValueError, 'address conflicts'):
            nfs_host.plan(self.endpoint, CONFIG + '\tinet 10.61.0.2 netmask 0xfffffffc\n', ROUTES)

    def test_failed_alias_operation_leaves_uncertain_intent(self):
        first = {'action': 'add-alias', 'endpoint': self.endpoint, 'address': '10.61.0.1/30',
                 'root_identity_sha256': 'a' * 64, 'command': ['sudo', 'ifconfig']}
        def run(argv, **kwargs):
            if argv == first['command']:
                raise subprocess.CalledProcessError(1, argv)
            return SimpleNamespace(returncode=0)
        with patch.object(nfs_host, 'inspect', return_value=first), patch.object(nfs_host.subprocess, 'run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                nfs_host.apply(Path('/root-id'), LOCATION, BOOT, self.state)
        self.assertEqual(json.loads(self.state.read_text())['state'], 'adding')
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_cleanup_refuses_active_gadget_and_retires_only_owned_alias(self):
        self.state.write_text(json.dumps(self.owner))
        self.state.chmod(0o600)
        current = {'connected': True, 'assigned': True, 'removed': False}
        def run(argv, **kwargs):
            if argv[0] == '/usr/sbin/ioreg':
                return SimpleNamespace(stdout=data(gadget()) if current['connected'] else data())
            if argv[0] == '/sbin/ifconfig':
                return SimpleNamespace(stdout=ASSIGNED if current['assigned'] else CONFIG)
            if argv[:3] == ['/usr/bin/sudo', '-n', '/sbin/ifconfig']:
                current['removed'] = True
                current['assigned'] = False
            return SimpleNamespace(returncode=0)
        with patch.object(nfs_host.sys, 'platform', 'darwin'), \
                patch.object(nfs_host, 'root_identity', return_value=({'root_id': ROOT}, 'a' * 64)), \
                patch.object(nfs_host.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'Disconnect the NFS gadget'):
                nfs_host.cleanup(Path('/root-id'), self.state)
            self.assertFalse(current['removed'])
            current['connected'] = False
            self.assertEqual(nfs_host.cleanup(Path('/root-id'), self.state)['action'], 'retired')
        self.assertTrue(current['removed'])
        self.assertEqual(json.loads(self.state.read_text())['state'], 'retired')


if __name__ == '__main__':
    unittest.main()
