"""Sequential devices/boots never share credentials, reports or active operations."""
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchard_cli.contracts import Connection
from orchard_cli.protocol import Client, DeviceError
from orchard_cli.state import BootState, DeviceState, EndpointUnavailable, operation_lock, private_directory
from orchard_cli.transports import ssh
from orchard_cli.apple import usb, linux_usb

BOOT_A = '11111111-1111-4111-8111-111111111111'
BOOT_B = '22222222-2222-4222-8222-222222222222'


class DeviceIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scoped = patch('orchard_cli.state.cache_root', return_value=self.root)
        scoped.start()
        self.addCleanup(scoped.stop)

    def seed(self, scope):
        access = private_directory(scope.root / 'access')
        values = {'profile':scope.device.profile, 'ecid':scope.device.ecid, 'boot_id':scope.boot_id,
                  'host':'172.16.42.1', 'port':22}
        for name, content in (('identity', str(scope.device.ecid)), ('known_hosts', 'pinned'),
                              ('endpoint.json', json.dumps(values))):
            (access / name).write_text(content)
            (access / name).chmod(0o600)
        (scope.root / 'last-install.json').write_text('receipt')
        return access

    def test_two_devices_and_two_boots_have_distinct_private_state(self):
        scopes = [BootState(DeviceState('j71b', 1), BOOT_A),
                  BootState(DeviceState('j71b', 2), BOOT_A),
                  BootState(DeviceState('j71b', 1), BOOT_B)]
        paths = [self.seed(scope) for scope in scopes]
        self.assertEqual(len(set(paths)), 3)
        ssh.forget_access(scopes[0])
        self.assertFalse(paths[0].exists())
        self.assertTrue(all(path.exists() for path in paths[1:]))
        self.assertTrue(all((scope.root / 'last-install.json').exists() for scope in scopes))

    def test_new_boot_cannot_fall_back_to_old_credentials_or_profile_cache(self):
        old = BootState(DeviceState('j71b',1), BOOT_A)
        access = self.seed(old)
        endpoint = {'ecid':1,'boot_id':BOOT_B,'location':'same-port','serial_port':'serial'}
        legacy = self.root / 'private/j71b/access'
        legacy.mkdir(parents=True)
        (legacy / 'identity').write_text('must not be tried')
        with patch.object(ssh,'verify_peer') as peer:
            with self.assertRaisesRegex(ValueError,'No retained'):
                with ssh.saved_session(old.device,lambda:endpoint,expected_boot=BOOT_B):
                    self.fail('Expired credentials were reused')
            peer.assert_not_called()
        self.assertTrue(access.exists())
        self.assertTrue((legacy / 'identity').exists())

    def test_changed_identity_or_extra_target_stops_before_rpc_dispatch(self):
        scope = BootState(DeviceState('j71b',1), BOOT_A)
        endpoint = {'ecid':1,'boot_id':BOOT_A,'location':'port','serial_port':'serial'}
        selected = Mock(return_value={**endpoint,'ecid':2})
        connection = Connection(['ssh'],scope,ssh.endpoint_check(scope,endpoint,selected))
        client = Client(connection,Mock())
        with patch('orchard_cli.protocol.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError,'changed'):
                client.call('install')
            launch.assert_not_called()
        for devices in ([{'mode':'linux'}, {'mode':'dfu'}], [{'mode':'pongo'}, {'mode':'pongo'}]):
            with patch.object(usb,'devices',return_value=devices), self.assertRaisesRegex(ValueError,'exactly one'):
                usb.sole_target()

    def test_temporary_absence_is_readiness_pollable_but_identity_change_is_not(self):
        check = Mock(side_effect=EndpointUnavailable('not connected'))
        client = Client(Connection(['ssh'],BootState(DeviceState('j71b',1),BOOT_A),check),Mock())
        with patch('orchard_cli.protocol.subprocess.Popen') as launch:
            with self.assertRaises(DeviceError):
                client.call('status')
            launch.assert_not_called()

    def test_pinned_peer_must_match_usb_ecid_boot_and_profile(self):
        scope = BootState(DeviceState('j71b',1),BOOT_A)
        check = Mock()
        connection = Connection(['ssh'],scope,check)
        for change in ({'ecid':2}, {'ecid':True}, {'boot_id':BOOT_B}, {'profile':'another'}):
            with patch.object(ssh,'Client') as client:
                client.return_value.call.return_value = {'profile':'j71b','ecid':1,'boot_id':BOOT_A,**change}
                with self.subTest(change=change), self.assertRaisesRegex(ValueError,'Pinned SSH peer differs'):
                    ssh.verify_peer(connection)
                client.return_value.call.assert_called_once_with('status',timeout=8)
        check.assert_not_called()

    def test_operation_lock_is_global_across_processes_and_released_on_failure(self):
        code = 'from orchard_cli.state import operation_lock\nwith operation_lock(): print("acquired")'
        env = {**os.environ,'ORCHARD_HOME':str(self.root)}
        with operation_lock():
            blocked = subprocess.run([sys.executable,'-I','-c',code],env=env,capture_output=True,text=True)
            self.assertNotEqual(blocked.returncode,0)
            self.assertIn('Another Orchard device operation',blocked.stderr)
        free = subprocess.run([sys.executable,'-I','-c',code],env=env,capture_output=True,text=True)
        self.assertEqual(free.returncode,0,free.stderr)
        self.assertIn('acquired',free.stdout)

    def test_linked_scope_components_are_refused(self):
        private_directory(self.root/'private/devices')
        outside = self.root/'outside'
        outside.mkdir()
        (self.root/'private/devices/j71b').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Linked private state'):
            private_directory(BootState(DeviceState('j71b',1),BOOT_A).root)
        self.assertEqual(list(outside.iterdir()),[])

    def test_macos_usb_descriptor_identifies_device_and_boot(self):
        raw = plistlib.dumps([{'IOObjectClass':'IOUSBHostDevice','idVendor':0x05ac,'idProduct':0x4142,
                               'locationID':123,'USB Serial Number':f'ECID:0000000000000002 BOOT:{BOOT_B}',
                               'USB Product Name':'J71b Recovery','IORegistryEntryChildren':[
                                   {'IOObjectClass':'IOSerialBSDClient','IOCalloutDevice':'/dev/cu.device'}]}])
        with patch.object(usb.sys,'platform','darwin'), patch.object(usb.subprocess,'run',return_value=
                subprocess.CompletedProcess([],0,raw,b'')):
            rows = usb.devices()
        self.assertEqual((rows[0]['ecid'],rows[0]['boot_id']),(2,BOOT_B))
        self.assertEqual(rows[0]['serial_port'],'/dev/cu.device')

    def test_macos_hubs_do_not_inherit_child_device_serial_ports(self):
        phone = {'IOObjectClass':'IOUSBHostDevice','idVendor':0x05ac,'idProduct':0x4144,
                 'locationID':123,'USB Product Name':'Orchard n61_ram',
                 'IORegistryEntryChildren':[
                     {'IOObjectClass':'IOUSBHostInterface','IORegistryEntryChildren':[
                         {'IOObjectClass':'IOSerialBSDClient','IOCalloutDevice':'/dev/cu.phone'}]}]}
        other = {'IOObjectClass':'IOUSBHostDevice','idVendor':0x1234,'idProduct':1,
                 'locationID':124,'IORegistryEntryChildren':[
                     {'IOObjectClass':'IOSerialBSDClient','IOCalloutDevice':'/dev/cu.other'}]}
        inner = {'IOObjectClass':'IOUSBHostDevice','idVendor':0x05ac,'idProduct':0x8004,
                 'locationID':120,'USB Product Name':'AppleUSB2Hub',
                 'IORegistryEntryChildren':[phone,other]}
        outer = {'IOObjectClass':'IOUSBHostDevice','idVendor':0x05ac,'idProduct':0x8004,
                 'locationID':100,'USB Product Name':'AppleUSB2Hub',
                 'IORegistryEntryChildren':[inner]}
        raw = plistlib.dumps([outer])
        with patch.object(usb.sys,'platform','darwin'), patch.object(usb.subprocess,'run',return_value=
                subprocess.CompletedProcess([],0,raw,b'')):
            rows = {row['location']:row for row in usb.devices()}
        self.assertEqual(set(rows),{100,120,123})
        self.assertIsNone(rows[100]['serial_port'])
        self.assertIsNone(rows[120]['serial_port'])
        self.assertEqual(rows[123]['serial_port'],'/dev/cu.phone')
        self.assertEqual(rows[123]['product'],'Orchard n61_ram')

    def test_a8_usb_identity_is_parsed_numerically_on_both_hosts(self):
        serial = 'CPID:7000 BDID:06 ECID:0000123456789ABC'
        expected = (0x7000, 0x06, 0x123456789abc)
        node = {'IOObjectClass':'IOUSBHostDevice', 'idVendor':0x05ac, 'idProduct':0x1227,
                'locationID':123, 'USB Serial Number':serial, 'USB Product Name':'Apple DFU'}
        with patch.object(usb.sys,'platform','darwin'), patch.object(usb.subprocess,'run',return_value=
                subprocess.CompletedProcess([],0,plistlib.dumps([node]),b'')):
            mac = usb.devices()[0]
        self.assertEqual(tuple(mac[k] for k in ('chip','board','ecid')),expected)

        node = self.root/'1-1'
        node.mkdir()
        for name,value in {'idVendor':'05ac','idProduct':'1227','serial':serial}.items():
            (node/name).write_text(value)
        linux = linux_usb.devices(self.root)[0]
        self.assertEqual(tuple(linux[k] for k in ('chip','board','ecid')),expected)

    def test_linux_usb_descriptor_identifies_device_and_boot_without_serial_commands(self):
        node = self.root/'1-2'
        node.mkdir()
        for name,value in {'idVendor':'05ac','idProduct':'4142','product':'J71b Recovery',
                           'serial':f'ECID:0000000000000002 BOOT:{BOOT_B}'}.items():
            (node/name).write_text(value)
        (node/'iface/tty/ttyACM0').mkdir(parents=True)
        rows = linux_usb.devices(self.root)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['ecid'],rows[0]['boot_id']),(2,BOOT_B))


if __name__ == '__main__':
    unittest.main()
