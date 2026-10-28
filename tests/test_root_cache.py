"""Device-cache negotiation never turns an uncertain install into a retry."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from orchard_cli.contracts import Connection
from orchard_cli.progress import Clock, Progress
from orchard_cli.protocol import Client, DeviceError
from orchard_cli.service import install_root, select_destination
from orchard_cli.state import BootState, DeviceState


class RootCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'root.tar.zst'
        self.payload = b'public-root-image'
        self.path.write_bytes(self.payload)
        self.receipt = {'sha256': hashlib.sha256(self.payload).hexdigest(), 'bytes': len(self.payload)}
        self.destination = {'selection_token': 'token', 'target': 'partition', 'directory': 'omarchy',
                            'replace': True, 'destination_identity': [1, 2, 3, 4], 'cache_root': True}
        self.output = io.StringIO()
        self.progress = Progress(self.output)

    def run_install(self, client, enabled=True):
        return install_root(client, self.destination, self.receipt, self.path,
                            self.progress, cache_enabled=enabled)

    def test_hit_does_not_open_or_send_host_archive(self):
        self.path.unlink()  # Reuse must not even open the local archive here.
        client = Mock()
        client.call.side_effect = [{**self.receipt, 'cached': True}, {'status': 'complete', 'root_cache': 'reused'}]
        result = self.run_install(client)
        self.assertEqual(result['root_cache'], 'reused')
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['root-cache', 'install'])
        request = client.call.call_args.args[1]
        self.assertEqual(request['root_cache'], 'reuse')
        self.assertEqual(request['destination_identity'], self.destination['destination_identity'])
        self.assertEqual(client.call.call_args.kwargs, {'timeout': 1200})
        self.assertIn('extracting locally', self.output.getvalue())

    def test_miss_transfers_complete_archive_once(self):
        client = Mock()
        streams = []
        def call(operation, request, **kwargs):
            self.assertEqual(request['sha256'], self.receipt['sha256'])
            self.assertEqual(request['bytes'], self.receipt['bytes'])
            if operation == 'root-cache':
                self.assertNotIn('source', kwargs)
                return {**self.receipt, 'cached': False}
            self.assertEqual(operation, 'install')
            self.assertEqual(request['root_cache'], 'store')
            self.assertEqual(kwargs['size'], len(self.payload))
            streams.append(kwargs['source'].read())
            return {'status': 'complete'}
        client.call.side_effect = call
        self.run_install(client)
        self.assertEqual(streams, [self.payload])
        self.assertIn('saving it in the device cache', self.output.getvalue())

    def test_changed_image_probes_new_receipt_and_uploads_on_miss(self):
        self.payload = b'changed-build'
        self.path.write_bytes(self.payload)
        self.receipt = {'sha256': hashlib.sha256(self.payload).hexdigest(), 'bytes': len(self.payload)}
        self.test_miss_transfers_complete_archive_once()

    def test_old_runtime_uses_original_stream_without_cache_operation(self):
        client = Mock()
        self.destination.pop('cache_root')
        def call(operation, request, **kwargs):
            self.assertEqual(operation, 'install')
            self.assertNotIn('root_cache', request)
            self.assertNotIn('cache_root', request)
            self.assertEqual(kwargs['source'].read(), self.payload)
            return {'status': 'complete'}
        client.call.side_effect = call
        self.run_install(client, enabled=False)
        self.assertEqual(client.call.call_count, 1)

    def test_invalid_probe_cannot_select_reuse_or_trigger_upload(self):
        good = {**self.receipt, 'cached': True}
        invalid = [None, [], {}, {**good, 'cached': 1}, {**good, 'cached': 'false'},
                   {**good, 'sha256': '0' * 64}, {**good, 'bytes': 1},
                   {**good, 'bytes': str(self.receipt['bytes'])}, {**good, 'bytes': True}]
        for response in invalid:
            with self.subTest(response=response):
                client = Mock()
                client.call.return_value = response
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    self.run_install(client)
                self.assertEqual(client.call.call_count, 1)

    def test_probe_error_stops_without_mutation(self):
        client = Mock()
        client.call.side_effect = DeviceError('unsafe cache path', reported=True)
        with self.assertRaisesRegex(DeviceError, 'unsafe cache'):
            self.run_install(client)
        self.assertEqual(client.call.call_count, 1)

    def test_failed_cached_or_streamed_install_is_never_retried(self):
        for hit in (True, False):
            for reported in (True, False):
                with self.subTest(hit=hit, reported=reported):
                    client = Mock()
                    client.call.side_effect = [{**self.receipt, 'cached': hit},
                                               DeviceError('install failed', reported=reported)]
                    with self.assertRaisesRegex(DeviceError, 'install failed'):
                        self.run_install(client)
                    self.assertEqual(client.call.call_count, 2)

    def test_destination_requests_cache_capacity_before_confirmation(self):
        client = Mock()
        def call(operation, request=None, **kwargs):
            if operation == 'targets':
                return {'selection_token': 'token', 'partitions': [
                    {'id': 'partition', 'bytes': 20 * 1024**3, 'filesystem': 'ext4', 'label': 'Linux'}]}
            self.assertEqual(operation, 'destination')
            self.assertIs(request['cache_root'], True)
            self.assertEqual(kwargs['timeout'], 1200)
            return {'exists': False, 'identity': None, 'free_bytes': 10 * 1024**3,
                    'reclaimable_bytes': 0, 'needed_bytes': 3 * 1024**3, 'inodes_sufficient': True}
        client.call.side_effect = call
        answers = iter(('', 'install omarchy'))
        result = select_destination(client, Clock(), directory='omarchy', cache_root=True,
                                    input_fn=lambda _: next(answers), output=lambda _: None)
        self.assertIs(result['cache_root'], True)
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['targets', 'destination'])

    def test_protocol_accepts_read_only_probe_without_payload(self):
        program = ('import sys,json; assert sys.stdin.buffer.read() == b""; '
                   'print(json.dumps({"protocol":1,"event":"result","operation":"root-cache",'
                   '"result":{"cached":False,"sha256":"abc","bytes":3}}))')
        scope = BootState(DeviceState('device', 1), '11111111-1111-4111-8111-111111111111')
        client = Client(Connection([sys.executable, '-c', program], scope, lambda: None), self.progress)
        result = client.call('root-cache', {'sha256': 'abc', 'bytes': 3}, timeout=5)
        self.assertIs(result['cached'], False)

    def test_cache_reclaimable_capacity_applies_to_new_directory(self):
        for reclaimed, success in ((1024**3, True), (0, False), (-1, False), ('lots', False)):
            with self.subTest(reclaimed=reclaimed):
                client = Mock()
                client.call.side_effect = [
                    {'selection_token': 'token', 'partitions': [
                        {'id': 'partition', 'bytes': 10 * 1024**3, 'filesystem': 'ext4', 'label': 'Linux'}]},
                    {'exists': False, 'identity': None, 'free_bytes': 2 * 1024**3,
                     'reclaimable_bytes': 0, 'cache_reclaimable_bytes': reclaimed,
                     'needed_bytes': 3 * 1024**3, 'inodes_sufficient': True}]
                answers = iter(('', 'install linux'))
                invoke = lambda: select_destination(client, Clock(), cache_root=True,
                    input_fn=lambda _: next(answers), output=lambda _: None)
                if success:
                    self.assertFalse(invoke()['replace'])
                else:
                    with self.assertRaisesRegex(ValueError, 'nothing was deleted'):
                        invoke()


if __name__ == '__main__':
    unittest.main()
