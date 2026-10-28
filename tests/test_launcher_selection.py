"""Installations keep the launcher they were installed for."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchard_cli import releases

DESCRIPTOR = {'boot': {
    'launch_manifest': 'https://example.invalid/latest/download/board-apple-launcher.boot.toml',
    'legacy_launch_manifest': 'https://example.invalid/download/v0.1.10/board-launcher.boot.toml',
}}


class LauncherSelection(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target in ('orchard_cli.state.cache_root',):
            p = patch(target, return_value=Path(temporary.name))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(releases, 'hardware_device', return_value=DESCRIPTOR)
        p.start()
        self.addCleanup(p.stop)

    def test_install_without_record_keeps_the_legacy_launcher(self):
        self.assertEqual(releases.launch_manifest('board', 1), DESCRIPTOR['boot']['legacy_launch_manifest'])

    def test_recorded_install_uses_the_launcher_it_was_installed_for(self):
        releases.record_launcher('board', 1)
        self.assertEqual(releases.launch_manifest('board', 1), DESCRIPTOR['boot']['launch_manifest'])
        self.assertEqual(releases.launch_manifest('board', 2), DESCRIPTOR['boot']['legacy_launch_manifest'])

    def test_without_a_device_the_current_launcher_is_used(self):
        self.assertEqual(releases.launch_manifest('board'), DESCRIPTOR['boot']['launch_manifest'])


if __name__ == '__main__':
    unittest.main()
