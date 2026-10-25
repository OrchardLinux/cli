"""Host confirmation, receipt pinning and no-replay behavior for formatting."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from orchard_cli.progress import Clock
from orchard_cli.protocol import DeviceError
from orchard_cli.service import initialize_partition, select_destination


class Initialization(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.report = Path(self.temp.name) / 'format.json'
        self.row = {'id': 'partition', 'filesystem': 'unformatted', 'bytes': 16 * 1024**3}
        self.observation = {'boot_id': 'boot', 'selection_token': 'before', 'needed_bytes': 2 * 1024**3}
        self.client = Mock()
        self.receipt = None

    def tearDown(self):
        self.temp.cleanup()

    def call(self, operation, request=None, **kwargs):
        if operation == 'format-target':
            self.assertEqual(json.loads(self.report.read_text())['status'], 'unknown')
            self.receipt = {**{k: request[k] for k in ('boot_id', 'operation_id', 'target')},
                            'status': 'complete', 'filesystem': 'ext4',
                            'filesystem_uuid': '11111111-1111-4111-8111-111111111111'}
            return self.receipt
        if operation == 'targets':
            self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')
            return {'boot_id': 'boot', 'selection_token': 'after', 'partitions': [
                {**self.row, 'filesystem': 'ext4', 'filesystem_uuid': self.receipt['filesystem_uuid']}]}
        raise AssertionError(operation)

    def initialize(self, answer='format partition'):
        return initialize_partition(self.client, Clock(), self.observation, self.row, self.report,
                                    input_fn=lambda _: answer, output=lambda _: None)

    def test_verified_receipt_then_new_selection(self):
        self.client.call.side_effect = self.call
        observation, row = self.initialize()
        self.assertEqual(observation['selection_token'], 'after')
        self.assertEqual(row['filesystem'], 'ext4')
        self.assertEqual([call.args[0] for call in self.client.call.call_args_list], ['format-target', 'targets'])

    def test_default_no_wrong_confirmation_and_insufficient_capacity_do_not_dispatch(self):
        for answer in ('', 'yes', 'format other'):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, 'not confirmed'):
                self.initialize(answer)
        self.row['bytes'] = 1
        with self.assertRaisesRegex(ValueError, 'too small'):
            self.initialize()
        self.client.call.assert_not_called()
        self.assertFalse(self.report.exists())

    def test_timeout_and_mismatched_receipt_record_unknown_without_retry(self):
        for failure in ('timeout', 'wrong-boot', 'wrong-id', 'wrong-target', 'invalid-uuid'):
            self.client.reset_mock()
            def response(operation, request=None, **kwargs):
                if failure == 'timeout':
                    raise DeviceError('transport lost')
                result = self.call(operation, request, **kwargs)
                key = {'wrong-boot': 'boot_id', 'wrong-id': 'operation_id',
                       'wrong-target': 'target', 'invalid-uuid': 'filesystem_uuid'}[failure]
                return {**result, key: 'wrong'}
            self.client.call.side_effect = response
            with self.subTest(failure=failure), self.assertRaisesRegex(RuntimeError, 'Do not retry'):
                self.initialize()
            self.assertEqual(self.client.call.call_count, 1)
            self.assertEqual(json.loads(self.report.read_text())['status'], 'unknown')

    def test_changed_target_after_known_success_stops_without_reformatting(self):
        def response(operation, request=None, **kwargs):
            result = self.call(operation, request, **kwargs)
            return {**result, 'boot_id': 'different'} if operation == 'targets' else result
        self.client.call.side_effect = response
        with self.assertRaisesRegex(ValueError, 'rediscovery'):
            self.initialize()
        self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')
        self.assertEqual(self.client.call.call_count, 2)

    def test_unsupported_signature_never_formats(self):
        self.client.call.return_value = {'selection_token': 'before', 'partitions': [
            {**self.row, 'filesystem': 'crypto_LUKS', 'label': 'Linux'}]}
        with self.assertRaisesRegex(ValueError, 'unsupported signature'):
            select_destination(self.client, Clock(), input_fn=lambda _: '', output=lambda _: None,
                               format_report=self.report)
        self.assertEqual(self.client.call.call_count, 1)
        self.assertFalse(self.report.exists())


if __name__ == '__main__':
    unittest.main()
