from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchard_cli.state import BootState, DeviceState
from orchard_cli.transports.ssh import forget_access, ssh_argv


BOOT = '11111111-1111-4111-8111-111111111111'
SCOPE = BootState(DeviceState('j71b', 1), BOOT)


class SSHLifecycle(unittest.TestCase):
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
