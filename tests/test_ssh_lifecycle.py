import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchard_cli.state import BootState, DeviceState
from orchard_cli.progress import Clock, Progress
from orchard_cli.protocol import DeviceError
from orchard_cli.service import finish_ssh_access
from orchard_cli.transports.ssh import forget_access, ssh_argv


BOOT = '11111111-1111-4111-8111-111111111111'
SCOPE = BootState(DeviceState('j71b', 1), BOOT)


class SSHLifecycle(unittest.TestCase):
    def run_choice(self, answer, result=None, error=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'last-install.json'
            report = {'installed': True, 'ready': True, 'release': 'new'}
            def call(operation, request, **kwargs):
                self.assertTrue(json.loads(path.read_text())['ready'])
                self.assertEqual(operation, 'revoke-ssh')
                self.assertEqual(request, {'release': 'new', 'boot_id': BOOT, 'profile':'j71b', 'ecid':1})
                if error:raise error
                return result or {'ssh_access': 'revoked', 'release': 'new', 'boot_id': BOOT, 'profile':'j71b', 'ecid':1}
            client, adapter = Mock(), Mock()
            client.call.side_effect = call
            def input_fn(prompt):
                self.assertEqual(prompt, 'Keep SSH access for this boot session? [y/N] ')
                if isinstance(answer, BaseException):raise answer
                return answer
            try:
                finish_ssh_access(client, adapter, {'boot_id': BOOT, 'profile':'j71b', 'ecid':1}, report, path,
                                  Clock(), input_fn, Progress(io.StringIO()), scope=SCOPE)
            finally:
                self.last = json.loads(path.read_text())
                self.client, self.adapter = client, adapter

    def test_explicit_yes_retains_keys_and_never_revokes(self):
        self.run_choice('yes')
        self.assertEqual(self.last['ssh_access'], 'retained')
        self.client.call.assert_not_called()
        self.adapter.forget_access.assert_not_called()

    def test_empty_no_and_eof_revoke_after_success(self):
        for answer in ('', 'n', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__):
                self.run_choice(answer)
                self.assertEqual(self.last['ssh_access'], 'revoked')
                self.adapter.forget_access.assert_called_once_with(SCOPE)

    def test_disconnect_is_unknown_never_replayed_or_called_success(self):
        with self.assertRaisesRegex(RuntimeError, 'Do not reinstall'):
            self.run_choice('', error=DeviceError('Disconnected'))
        self.assertEqual(self.last['ssh_access'], 'unknown')
        self.assertTrue(self.last['ready'])
        self.client.call.assert_called_once()
        self.adapter.forget_access.assert_not_called()

    def test_wrong_boot_receipt_is_not_accepted(self):
        with self.assertRaises(RuntimeError):
            self.run_choice('', result={'ssh_access': 'revoked', 'release': 'new', 'boot_id': 'different'})
        self.assertEqual(self.last['ssh_access'], 'unknown')
        self.adapter.forget_access.assert_not_called()

    def test_wrong_device_receipt_does_not_delete_another_devices_credentials(self):
        with self.assertRaises(RuntimeError):
            self.run_choice('', result={'ssh_access':'revoked','release':'new','boot_id':BOOT,
                                        'profile':'j71b','ecid':2})
        self.assertEqual(self.last['ssh_access'], 'unknown')
        self.adapter.forget_access.assert_not_called()

    def test_final_rpc_cannot_leave_a_multiplexed_connection_alive(self):
        argv = ssh_argv('identity', 'known_hosts', '172.16.42.1', 22)
        for option in ('ControlMaster=no', 'ControlPath=none', 'ControlPersist=no'):
            self.assertIn(option, argv)

    def test_only_transport_credentials_are_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boot = root / 'private/devices/j71b/0000000000000001/boots' / BOOT.replace('-', '')
            access = boot / 'access'
            access.mkdir(parents=True)
            for name in ('identity', 'known_hosts', 'endpoint.json'):(access/name).write_text('credential')
            retained = boot / 'last-install.json'
            retained.write_text('receipt')
            with patch('orchard_cli.state.cache_root', return_value=root):forget_access(SCOPE)
            self.assertFalse(access.exists())
            self.assertEqual(retained.read_text(), 'receipt')


if __name__ == '__main__':unittest.main()
