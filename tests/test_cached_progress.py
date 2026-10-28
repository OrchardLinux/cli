"""Device-local extraction meters remain accurate, bounded and backwards compatible."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orchard_cli.contracts import Connection
from orchard_cli.progress import Progress
from orchard_cli.protocol import Client, DeviceError
from orchard_cli.state import BootState, DeviceState


class Terminal(io.StringIO):
    def isatty(self):
        return True


def phase(name='cached-extraction'):
    return {'protocol': 1, 'event': 'phase', 'phase': name}


def event(count, total=1048576, complete=False):
    return {'protocol': 1, 'event': 'progress', 'phase': 'cached-extraction',
            'bytes': count, 'total': total, 'complete': complete}


class CachedProgress(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cache = patch('orchard_cli.cache.cache_root', return_value=Path(self.folder.name))
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_tty_updates_and_verbose_does_not_print_raw_progress(self):
        output = Terminal()
        progress = Progress(output, verbose=True)
        progress.event(phase())
        with patch('orchard_cli.progress.time.monotonic', side_effect=[10, 11, 12]):
            for record in (event(0), event(524288), event(1048576, complete=True)):
                progress.event(record)
        text = output.getvalue()
        self.assertEqual(text.count('Cached extraction'), 3)
        self.assertIn(' 50%', text)
        self.assertIn('100%', text)
        self.assertNotIn("'bytes':", text)
        self.assertNotIn('USB', text)
        self.assertIn("'bytes': 524288", Path(progress.log_path).read_text())
        self.assertFalse(progress.active_line)

    def test_nonterminal_output_is_sparse_but_completion_is_always_visible(self):
        output = io.StringIO()
        progress = Progress(output)
        progress.event(phase())
        with patch('orchard_cli.progress.time.monotonic', side_effect=[0, 1, 11, 12]):
            for record in (event(0), event(10), event(20), event(1048576, complete=True)):
                progress.event(record)
        text = output.getvalue()
        self.assertEqual(text.count('Cached extraction'), 3)
        self.assertNotIn('\r', text)
        self.assertIn('100%', text)

    def test_tty_updates_are_throttled(self):
        output = Terminal()
        progress = Progress(output)
        progress.event(phase())
        with patch('orchard_cli.progress.time.monotonic', side_effect=[0, 0.1, 0.25, 0.26]):
            for record in (event(0), event(10), event(20), event(1048576, complete=True)):
                progress.event(record)
        self.assertEqual(output.getvalue().count('Cached extraction'), 3)

    def test_near_completion_does_not_round_up_to_100(self):
        output = Terminal()
        progress = Progress(output)
        progress.event(phase())
        with patch('orchard_cli.progress.time.monotonic', side_effect=[0, 1]):
            progress.event(event(0))
            progress.event(event(1048575))
        self.assertIn(' 99%', output.getvalue())
        self.assertNotIn('100%', output.getvalue())
        progress.reset_device_progress()
        self.assertFalse(progress.active_line)
        self.assertNotIn('100%', output.getvalue())

    def test_invalid_records_are_diagnostics_not_meter_updates(self):
        output = Terminal()
        progress = Progress(output)
        progress.event(phase())
        progress.event(event(0))
        records = [event(True), event(-1), event(1, total=True), event(1, total=0),
                   event(1, total=2**64), event(1.5), event('1'), event(2, total=1),
                   event(1, complete=True), event(1048576), event(0, complete='yes')]
        before = output.getvalue()
        for record in records:
            progress.event(record)
        self.assertEqual(output.getvalue(), before)
        self.assertEqual(progress.device_progress['bytes'], 0)
        self.assertIn("'bytes': -1", Path(progress.log_path).read_text())

    def test_backwards_changed_total_and_duplicate_completion_are_ignored(self):
        output = Terminal()
        progress = Progress(output)
        progress.event(phase())
        with patch('orchard_cli.progress.time.monotonic', side_effect=range(20)):
            progress.event(event(0))
            progress.event(event(100))
            before = output.getvalue()
            progress.event(event(99))
            progress.event(event(101, total=2097152))
            self.assertEqual(output.getvalue(), before)
            progress.event(event(1048576, complete=True))
            before = output.getvalue()
            progress.event(event(1048576, complete=True))
            progress.event(event(0))
            self.assertEqual(output.getvalue(), before)

    def test_phase_transition_ends_incomplete_line_and_resets(self):
        output = Terminal()
        progress = Progress(output)
        progress.event(phase())
        progress.event(event(0))
        progress.event(phase('finalize'))
        self.assertFalse(progress.active_line)
        self.assertIsNone(progress.device_progress)
        self.assertNotIn('100%', output.getvalue())
        progress.event(phase())
        progress.event(event(0, total=2097152))
        self.assertEqual(progress.device_progress['total'], 2097152)

    def test_old_and_unknown_events_stay_compatible(self):
        output = Terminal()
        progress = Progress(output)
        for record in (phase(), {'protocol': 1, 'event': 'progress', 'message': 'touch'},
                       {'protocol': 1, 'event': 'progress', 'phase': 'future', 'bytes': 1},
                       phase('finalize')):
            progress.event(record)
        self.assertEqual(output.getvalue(), '')
        self.assertIsNone(progress.device_progress)

    def test_unsolicited_progress_or_missing_initial_zero_is_ignored(self):
        output = Terminal()
        progress = Progress(output)
        progress.event(event(0))
        progress.event(phase())
        progress.event(event(100))
        self.assertEqual(output.getvalue(), '')
        self.assertIsNone(progress.device_progress)

    def test_rpc_error_closes_partial_meter_and_next_call_can_restart(self):
        output = Terminal()
        progress = Progress(output)
        scope = BootState(DeviceState('device', 1), '11111111-1111-4111-8111-111111111111')
        records = [phase(), event(0), event(100),
                   {'protocol': 1, 'event': 'error', 'message': 'integrity mismatch'}]
        program = 'import sys; print(' + repr('\n'.join(map(json.dumps, records))) + '); sys.exit(1)'
        client = Client(Connection([sys.executable, '-c', program], scope, lambda: None), progress)
        for _ in range(2):
            with self.assertRaisesRegex(DeviceError, 'integrity mismatch'):
                client.call('install', timeout=5)
            self.assertFalse(progress.active_line)
            self.assertIsNone(progress.device_progress)
        self.assertEqual(output.getvalue().count('Cached extraction'), 2)
        self.assertNotIn('100%', output.getvalue())

    def test_rpc_success_resets_completed_meter(self):
        output = Terminal()
        progress = Progress(output)
        scope = BootState(DeviceState('device', 1), '11111111-1111-4111-8111-111111111111')
        records = [phase(), event(0), event(1048576, complete=True), phase('finalize'),
                   {'protocol': 1, 'event': 'result', 'operation': 'install', 'result': {'ok': True}}]
        program = 'print(' + repr('\n'.join(map(json.dumps, records))) + ')'
        client = Client(Connection([sys.executable, '-c', program], scope, lambda: None), progress)
        self.assertTrue(client.call('install', timeout=5)['ok'])
        self.assertIsNone(progress.device_progress)
        self.assertIn('100%', output.getvalue())


if __name__ == '__main__':
    unittest.main()
