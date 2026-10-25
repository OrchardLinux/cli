"""Host workflow regression tests."""
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orchard_cli.contracts import Connection
from orchard_cli.state import BootState, DeviceState
from orchard_cli.progress import Clock, Progress
from orchard_cli.protocol import Client, DeviceError
from orchard_cli.service import install, select_destination, wait_ready
from orchard_cli.apple.ipsw import hcd_from_binary


BOOT = '11111111-1111-4111-8111-111111111111'
SCOPE = BootState(DeviceState('device', 1), BOOT)


class HostWorkflow(unittest.TestCase):
    def setUp(self):
        self.state_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_temp.cleanup)
        self.state_patch = patch('orchard_cli.state.cache_root', return_value=Path(self.state_temp.name))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
    def test_cancel_before_asset_access_download_or_mutation(self):
        from unittest.mock import Mock
        adapter = Mock(NAME='Supported tablet', PROFILE='device')
        with patch('orchard_cli.service.choose', return_value=(adapter, {'mode': 'dfu'})), \
             patch('orchard_cli.service.resolve') as assets:
            result = install(input_fn=lambda _: 'n', progress=Progress(io.StringIO()))
            self.assertTrue(result['cancelled'])
            assets.assert_not_called()
            adapter.firmware_inputs.assert_not_called()
            adapter.boot.assert_not_called()

    def test_guided_run_reaches_desktop_with_one_mixin_and_one_root_stream(self):
        for filesystem in ('ext4', 'unformatted'):
            with self.subTest(filesystem=filesystem):
                self.guided_run(filesystem)

    def guided_run(self, filesystem):
        from contextlib import contextmanager
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'private/devices/device/0000000000000001/boots' / BOOT.replace('-', '')
            image, mixin = root / 'root', root / 'mixin'
            image.write_bytes(b'root-stream')
            mixin.write_bytes(b'private-data')
            adapter = Mock(NAME='Supported tablet', PROFILE='device')
            adapter.capture_adt.return_value = b'fresh-adt'
            adapter.prepare_mixin.return_value = (mixin, {'bytes': 12, 'sha256': 'mixin-digest'})
            import threading
            downloads_started = threading.Barrier(2)
            downloads_done = set()
            def acquire_firmware(*args):
                downloads_started.wait(timeout=2)
                downloads_done.add('firmware')
                return root
            adapter.firmware_inputs.side_effect = acquire_firmware
            def boot(*args):
                self.assertEqual(downloads_done, {'firmware', 'assets'})
            adapter.boot.side_effect = boot
            @contextmanager
            def connection(device):
                yield Connection(['authenticated-transport'], SCOPE, lambda: None)
            adapter.connect = connection
            class Device:
                prepared = installed = handed_off = formatted = False
                def call(self, operation, request=None, **kwargs):
                    if operation == 'status':
                        return {'release': '1.0', 'boot_id': BOOT, 'ecid':1, 'profile':'device', 'ready': self.handed_off}
                    if operation == 'prepare':
                        assert not self.prepared and kwargs['source'].read() == b'private-data'
                        self.prepared = True
                        return {'status': 'complete'}
                    if operation == 'targets':
                        return {'selection_token': 'new-token' if self.formatted else 'token',
                                'boot_id': BOOT, 'needed_bytes': 512 * 1024**2, 'partitions': [
                            {'id': 'target', 'bytes': 16 * 1024**3, 'label': 'Linux',
                             'filesystem': 'ext4' if self.formatted else filesystem,
                             'filesystem_uuid': '11111111-1111-4111-8111-111111111111'}]}
                    if operation == 'format-target':
                        assert self.prepared and not self.formatted and filesystem == 'unformatted'
                        saved = json.loads((private / 'last-format.json').read_text())
                        assert saved['status'] == 'unknown' and saved['operation_id'] == request['operation_id']
                        assert request['confirmation'] == 'format target' and request['selection_token'] == 'token'
                        self.formatted = True
                        return {**{k: request[k] for k in ('boot_id', 'operation_id', 'target')},
                                'status': 'complete', 'filesystem': 'ext4',
                                'filesystem_uuid': '11111111-1111-4111-8111-111111111111'}
                    if operation == 'destination':
                        assert request['selection_token'] == ('new-token' if self.formatted else 'token')
                        return {'exists': False, 'identity': None, 'free_bytes': 10 * 1024**3,
                                'reclaimable_bytes': 0, 'needed_bytes': 2 * 1024**3, 'inodes_sufficient': True}
                    if operation == 'install':
                        assert self.prepared and not self.installed and kwargs['source'].read() == b'root-stream'
                        self.installed = True
                        return {'status': 'complete'}
                    if operation == 'handoff':
                        assert self.installed and not self.handed_off
                        self.handed_off = True
                        return {'requested': True}
                    raise AssertionError(operation)
            device = Device()
            answers = iter(('y', '', *(['format target'] if filesystem == 'unformatted' else []),
                            'install linux', 'y'))
            record = {'release': '1.0', 'assets': {'root': {'bytes': 11, 'sha256': 'root-digest'}}}
            def acquire_assets(*args, **kwargs):
                downloads_started.wait(timeout=2)
                downloads_done.add('assets')
                return record, {'root': image}
            def check_committed_before_readiness(client, progress):
                saved = json.loads((private / 'last-install.json').read_text())
                self.assertTrue(saved['installed'])
                self.assertIsNone(saved['ready'])
                self.assertIsNone(saved['timing']['install_seconds'])
                return wait_ready(client, progress)
            with patch('orchard_cli.service.choose', return_value=(adapter, {'mode':'dfu', 'location':'opaque', 'ecid':1, 'resumed':False})), \
                 patch('orchard_cli.service.resolve', side_effect=acquire_assets), \
                 patch('orchard_cli.service.Client', return_value=device), \
                 patch('orchard_cli.state.cache_root', return_value=root), \
                 patch('orchard_cli.service.wait_ready', side_effect=check_committed_before_readiness):
                result = install(input_fn=lambda _: next(answers), progress=Progress(io.StringIO()))
            self.assertTrue(result['ready'])
            self.assertTrue(device.handed_off)
            self.assertEqual(device.formatted, filesystem == 'unformatted')
            if device.formatted:
                self.assertEqual(json.loads((private / 'last-format.json').read_text())['status'], 'complete')
            receipt = private / 'last-install.json'
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertIn('install_seconds', json.loads(receipt.read_text())['timing'])

    def test_target_ui_offers_keep_and_another_directory(self):
        class Device:
            def __init__(self):
                self.calls = []
            def call(self, operation, request=None):
                self.calls.append((operation, request))
                if operation == 'targets':
                    return {'selection_token': 'token', 'partitions': [
                        {'id': 'partition', 'bytes': 1024**3, 'label': 'Linux', 'filesystem': 'ext4'}]}
                return {'exists': request['directory'] == 'linux', 'identity': None,
                        'free_bytes': 10 * 1024**3, 'reclaimable_bytes': 1024**3,
                        'needed_bytes': 2 * 1024**3, 'inodes_sufficient': True}
        answers = iter(('', '2', 'chosen', 'install chosen'))
        device = Device()
        result = select_destination(device, Clock(), input_fn=lambda _: next(answers), output=lambda _: None)
        self.assertEqual(result['directory'], 'chosen')
        self.assertFalse(result['replace'])
        self.assertEqual([c[0] for c in device.calls], ['targets', 'destination', 'destination'])

    def test_replacement_plan_and_capacity_gate(self):
        class Device:
            free = 4 * 1024**3
            reclaimable = 12 * 1024**3
            inodes = True
            def call(self, operation, request=None):
                if operation == 'targets':
                    return {'selection_token': 'token', 'partitions': [
                        {'id': 'partition', 'bytes': 60 * 1024**3, 'label': 'Linux', 'filesystem': 'ext4', 'free_bytes': self.free}]}
                return {'exists': True, 'identity': [1, 2, 3, 4], 'free_bytes': self.free,
                        'reclaimable_bytes': self.reclaimable, 'needed_bytes': 14 * 1024**3,
                        'inodes_sufficient': self.inodes}
        client = Device()
        output = []
        answers = iter(('', '1', 'replace linux'))
        result = select_destination(client, Clock(), input_fn=lambda _: next(answers), output=output.append)
        self.assertTrue(result['replace'])
        self.assertEqual(result['destination_identity'], [1, 2, 3, 4])
        self.assertIn('No backup will be created.', '\n'.join(output))
        self.assertIn('16.0 GiB', '\n'.join(output))
        for insufficient_inodes in (False, True):
            client.reclaimable = 12 * 1024**3 if insufficient_inodes else 0
            client.inodes = not insufficient_inodes
            answers = iter(('', '1'))  # Must stop before asking for destructive confirmation.
            with self.assertRaisesRegex(ValueError, 'nothing was deleted'):
                select_destination(client, Clock(), input_fn=lambda _: next(answers), output=lambda _: None)
        answers = iter(('', '3'))
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            select_destination(client, Clock(), input_fn=lambda _: next(answers), output=lambda _: None)

    def test_clock_excludes_input_and_download_once(self):
        with patch('orchard_cli.progress.time.monotonic', side_effect=[0, 1, 4, 5, 7, 12]):
            clock = Clock()
            with clock.exclude('input'):
                pass
            with clock.exclude('download'):
                pass
            report = clock.result()
        self.assertEqual(report['install_seconds'], 7)
        self.assertEqual(report['elapsed_seconds'], 12)

    def test_serial_bootstrap_consumes_only_versioned_connection_data(self):
        from orchard_cli.transports.ssh import bootstrap_reply
        row = {'protocol': 1, 'event': 'result', 'operation': 'bootstrap',
               'result': {'profile': 'device', 'ecid': 1, 'boot_id': BOOT,
                          'host': '172.16.42.1', 'port': 22, 'host_key': 'ssh-ed25519 ' + 'A' * 68}}
        self.assertEqual(bootstrap_reply(0, json.dumps(row), SCOPE)[:2], ('172.16.42.1', 22))
        row['result']['host'] = 'host; mount /dev/example'
        with self.assertRaises(ValueError):
            bootstrap_reply(0, json.dumps(row), SCOPE)

    def test_authenticated_readiness_retries_only_read_only_probes(self):
        from unittest.mock import Mock
        from orchard_cli.transports.ssh import wait_authenticated
        results = [subprocess.CompletedProcess([], 255, b'', b'Connection refused'),
                   subprocess.CompletedProcess([], 0, b'', b'')]
        with patch('orchard_cli.transports.ssh.subprocess.run', side_effect=results) as run, \
             patch('orchard_cli.transports.ssh.time.sleep'):
            wait_authenticated(['ssh', '-o', 'StrictHostKeyChecking=yes', 'root@device'], Mock())
        self.assertEqual(len(run.call_args_list), 2)
        self.assertTrue(all(call.args[0][-1] == 'true' for call in run.call_args_list))
        self.assertTrue(all('StrictHostKeyChecking=yes' in call.args[0] for call in run.call_args_list))

    def test_authenticated_readiness_never_retries_rejected_pins(self):
        from unittest.mock import Mock
        from orchard_cli.transports.ssh import wait_authenticated
        with patch('orchard_cli.transports.ssh.subprocess.run', return_value=
                   subprocess.CompletedProcess([], 255, b'', b'Host key verification failed')) as run:
            with self.assertRaisesRegex(ValueError, 'authentication rejected'):
                wait_authenticated(['ssh', 'root@device'], Mock())
        self.assertEqual(run.call_count, 1)

    def test_failed_session_is_cleaned_before_asset_or_mixin_work(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            adapter = Mock(NAME='Supported tablet', PROFILE='device')
            adapter.REBOOT_MESSAGE = 'A fresh recovery boot is required. Run: orchard install'
            @contextmanager
            def connection(device):
                yield Connection(['authenticated-transport'], SCOPE, lambda: None)
            adapter.connect = connection
            client = Mock()
            client.call.side_effect = [
                {'release': '1.0', 'installation_attempted': True, 'capabilities': ['abort']},
                {'message': 'Incomplete staging removed. ' + adapter.REBOOT_MESSAGE}]
            with patch('orchard_cli.service.choose', return_value=(adapter, {'resumed': True})), \
                 patch('orchard_cli.service.Client', return_value=client), \
                 patch('orchard_cli.state.cache_root', return_value=Path(directory)), \
                 patch('orchard_cli.service.resolve', side_effect=AssertionError('must inspect before assets')):
                with self.assertRaisesRegex(RuntimeError, 'orchard install') as error:
                    install(input_fn=lambda _: 'y', progress=Progress(io.StringIO()))
            self.assertNotIn('do not retry', str(error.exception))
            adapter.firmware_inputs.assert_not_called()
            adapter.prepare_mixin.assert_not_called()
            adapter.boot.assert_not_called()
            self.assertEqual(client.call.call_args.args[0], 'abort')

    def test_terminal_progress_updates_in_place_and_logs_are_sparse(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        for interactive in (True, False):
            output = Terminal() if interactive else io.StringIO()
            with patch('orchard_cli.progress.time.monotonic', side_effect=[0, 1, 2, 3]):
                progress = Progress(output)
                progress.transfer(1, 3)
                progress.transfer(2, 3)
                progress.transfer(3, 3)
            text = output.getvalue()
            if interactive:
                self.assertEqual(text.count('\r'), 3)
                self.assertEqual(text.count('\n'), 1)
            else:
                self.assertNotIn('\r', text)
                self.assertEqual(text.count('\n'), 2)

    def test_recovery_detection_does_not_require_the_dfu_location(self):
        from orchard_cli.j71b import adapter
        endpoint = {'mode': 'linux', 'location': 'new-enumeration',
                    'product': 'J71b Recovery', 'serial_port': '/dev/recovery', 'ecid': 1, 'boot_id': BOOT}
        original = {'location': 'dfu-enumeration', 'ecid': 1}
        with patch.object(adapter.usb, 'devices', return_value=[endpoint]):
            self.assertEqual(adapter.apple_boot.wait_mode(original, 'linux', chip=adapter.CHIP,
                             board=adapter.BOARD, product=adapter.PRODUCT, seconds=0), endpoint)
        with patch.object(adapter, 'read_adt', return_value=b'fresh'), \
             patch.object(adapter, 'validate_adt', return_value=2):
            with self.assertRaisesRegex(ValueError, 'identity differs'):
                adapter.capture_adt(original)

    def test_recovery_detection_refuses_ambiguous_or_wrong_endpoints(self):
        from orchard_cli.j71b import adapter
        endpoint = {'mode': 'linux', 'location': 'usb',
                    'product': 'J71b Recovery', 'serial_port': '/dev/recovery', 'ecid': 1, 'boot_id': BOOT}
        with patch.object(adapter.usb, 'devices', return_value=[endpoint, {**endpoint, 'location': 'other'}]):
            with self.assertRaisesRegex(ValueError, 'exactly one target'):
                adapter.apple_boot.wait_mode({'location': 'usb'}, 'linux', chip=adapter.CHIP,
                                             board=adapter.BOARD, product=adapter.PRODUCT, seconds=0)
        with patch.object(adapter.usb, 'devices', return_value=[{**endpoint, 'product': 'Other device'}]):
            with self.assertRaisesRegex(RuntimeError, 'observed: linux \\(Other device\\)'):
                adapter.apple_boot.wait_mode({'location': 'usb'}, 'linux', chip=adapter.CHIP,
                                             board=adapter.BOARD, product=adapter.PRODUCT, seconds=0)

    def test_running_recovery_can_be_identified_without_reboot(self):
        from orchard_cli.j71b import adapter
        from unittest.mock import Mock
        endpoint = {'mode': 'linux', 'location': 'usb',
                    'product': 'J71b Recovery', 'serial_port': '/dev/recovery', 'ecid': 1, 'boot_id': BOOT}
        with patch.object(adapter.usb, 'devices', return_value=[endpoint]), \
             patch.object(adapter, 'read_adt', return_value=b'fresh'), \
             patch.object(adapter, 'validate_adt', return_value=1), \
             patch.object(adapter.apple_boot.pongo, 'upload') as upload:
            device = adapter.discover()
            self.assertTrue(device['resumed'])
            self.assertEqual(device['ecid'], 1)
            adapter.boot(device, {}, {}, Mock())
            upload.assert_not_called()

    def test_protocol_stream_reports_device_failure_without_retransmission(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'attempts'
            program = ('import sys,json,pathlib; p=pathlib.Path(' + repr(str(marker)) + '); '
                       'p.write_text(p.read_text()+"x" if p.exists() else "x"); '
                       'sys.stdin.buffer.read(); '
                       'print(json.dumps({"protocol":1,"event":"error","message":"integrity mismatch"})); '
                       'sys.exit(1)')
            client = Client(Connection([sys.executable, '-c', program], SCOPE, lambda: None), Progress(io.StringIO()))
            with self.assertRaisesRegex(DeviceError, 'integrity mismatch'):
                client.call('install', source=io.BytesIO(b'archive'), size=7, timeout=5)
            self.assertEqual(marker.read_text(), 'x')

    def test_reported_handoff_failure_stops_without_a_readiness_wait(self):
        from unittest.mock import Mock
        client = Mock()
        client.call.return_value = {'ready': False, 'failure': {'error': 'handoff stopped'}}
        with patch('orchard_cli.service.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'handoff stopped'):
                wait_ready(client, Progress(io.StringIO()))
            sleep.assert_not_called()

    def test_protocol_rejects_incompatible_responses(self):
        program = 'print(\'{"protocol":2,"event":"result","result":{}}\')'
        client = Client(Connection([sys.executable, '-c', program], SCOPE, lambda: None), Progress(io.StringIO()))
        with self.assertRaisesRegex(DeviceError, 'Incompatible protocol'):
            client.call('status', timeout=5)

    def test_hcd_extraction_requires_the_pinned_bytes(self):
        command = b'\x4c\xfc\x0c' + b'\x00' * 4 + b'BRCMcfgS' + b'\x4e\xfc\x00'
        expected = hashlib.sha256(command).hexdigest()
        self.assertEqual(hcd_from_binary(b'prefix' + command + b'suffix', expected), command)
        with self.assertRaises(ValueError):
            hcd_from_binary(command, '0' * 64)

