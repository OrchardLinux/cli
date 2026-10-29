"""Host confirmation, receipt pinning and no-replay behavior for formatting."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

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

    def test_initialization_keeps_existing_confirmation_whitespace_handling(self):
        self.client.call.side_effect = self.call
        self.initialize(' format partition ')
        self.assertEqual(self.client.call.call_args_list[0].args[1]['confirmation'], 'format partition')

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


class Reset(unittest.TestCase):
    def setUp(self):
        Initialization.setUp(self)
        self.row.update(filesystem='ext4', label='Linux',
                        filesystem_uuid='22222222-2222-4222-8222-222222222222')
        self.observation['partitions'] = [self.row]
        self.messages = []
        self.reset_calls = 0
        self.failure = None

    tearDown = Initialization.tearDown

    def call(self, operation, request=None, **kwargs):
        if operation == 'reset-target':
            self.reset_calls += 1
            self.assertEqual(json.loads(self.report.read_text())['status'], 'unknown')
            self.assertEqual(request['confirmation'], 'reset partition')
            self.assertEqual(request['expected_filesystem_uuid'], self.row['filesystem_uuid'])
            if isinstance(self.failure, BaseException):
                raise self.failure
            self.receipt = {**{key: request[key] for key in ('boot_id', 'operation_id', 'target')},
                            'operation': operation, 'status': 'complete', 'filesystem': 'ext4',
                            'previous_filesystem_uuid': self.row['filesystem_uuid'],
                            'filesystem_uuid': '11111111-1111-4111-8111-111111111111'}
            return {**self.receipt, **(self.failure or {})}
        if operation == 'targets':
            if not self.receipt:
                return self.observation
            return {**self.observation, 'selection_token': 'after', 'partitions': [
                {**self.row, 'filesystem_uuid': self.receipt['filesystem_uuid']}]}
        if operation == 'destination':
            self.assertEqual(request['selection_token'], 'after' if self.receipt else 'before')
            return {'exists': False, 'identity': None, 'free_bytes': 8 * 1024**3,
                    'reclaimable_bytes': 0, 'needed_bytes': 2 * 1024**3, 'inodes_sufficient': True}
        raise AssertionError(operation)

    def select(self, answers, capabilities=('reset-ext4-v1',)):
        self.client.call.side_effect = self.call
        values = iter(answers)
        def answer(_):
            value = next(values)
            if isinstance(value, BaseException):
                raise value
            return value
        return select_destination(self.client, Clock(), input_fn=answer, output=self.messages.append,
                                  format_report=self.report, capabilities=capabilities)

    def test_reset_rediscovery_then_destination_and_complete_receipt(self):
        result = self.select(('', '2', 'reset partition', 'install linux'))
        self.assertEqual(result['selection_token'], 'after')
        self.assertEqual(self.reset_calls, 1)
        self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')
        text = '\n'.join(self.messages)
        for warning in ('EVERYTHING', 'personal files', 'cached images', 'GPT', 'not secure erasure',
                        'ID partition', self.row['filesystem_uuid']):
            self.assertIn(warning, text)
        self.assertEqual([call.args[0] for call in self.client.call.call_args_list],
                         ['targets', 'reset-target', 'targets', 'destination'])

    def test_post_reset_capacity_failure_retains_destruction_warning_and_receipt(self):
        actual = self.call
        for reason in ('space', 'inodes'):
            self.receipt, self.reset_calls = None, 0
            self.client.reset_mock()
            def response(operation, request=None, **kwargs):
                result = actual(operation, request, **kwargs)
                if operation == 'destination':
                    result.update(free_bytes=0 if reason == 'space' else result['free_bytes'],
                                  inodes_sufficient=reason != 'inodes')
                return result
            with patch.object(self, 'call', side_effect=response):
                with self.subTest(reason=reason), self.assertRaises(RuntimeError) as error:
                    self.select(('', '2', 'reset partition'))
            message = str(error.exception)
            self.assertIn('reset completed', message)
            self.assertIn('previous contents on partition partition were destroyed', message)
            self.assertIn('Insufficient space or free inodes', message)
            self.assertIn(str(self.report), message)
            self.assertNotIn('nothing was deleted', message)
            self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')
            self.assertEqual(self.reset_calls, 1)
            self.assertEqual([c.args[0] for c in self.client.call.call_args_list],
                             ['targets', 'reset-target', 'targets', 'destination'])

    def test_post_reset_cancel_interrupt_and_inspection_failure_preserve_outcome(self):
        actual = self.call
        for failure in ('wrong confirmation', EOFError(), KeyboardInterrupt(), DeviceError('inspection failed')):
            self.receipt, self.reset_calls = None, 0
            self.client.reset_mock()
            def response(operation, request=None, **kwargs):
                if operation == 'destination' and isinstance(failure, DeviceError):
                    raise failure
                return actual(operation, request, **kwargs)
            with patch.object(self, 'call', side_effect=response):
                with self.subTest(failure=failure), self.assertRaises(RuntimeError) as error:
                    self.select(('', '2', 'reset partition', failure))
            self.assertIn('reset completed', str(error.exception))
            self.assertIn('were destroyed', str(error.exception))
            self.assertIn('Do not repeat reset', str(error.exception))
            self.assertNotIn('nothing was deleted', str(error.exception))
            self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')
            self.assertEqual(self.reset_calls, 1)

    def test_keep_is_default_and_old_recovery_does_not_offer_reset(self):
        for capabilities, answers in ((('reset-ext4-v1',), ('', '', 'install linux')),
                                      (('reset-ext4-v1',), ('', '1', 'install linux')),
                                      ((), ('', 'install linux'))):
            self.messages.clear()
            self.select(answers, capabilities)
            self.assertEqual(self.reset_calls, 0)
            self.assertFalse(self.report.exists())
            self.assertEqual(any('Reset filesystem' in text for text in self.messages), bool(capabilities))

    def test_cancel_wrong_consent_eof_and_interrupt_do_not_mutate(self):
        for tail in (('3',), ('invalid',), (EOFError(),), (KeyboardInterrupt(),),
                     ('2', ''), ('2', 'yes'), ('2', 'reset other'),
                     ('2', EOFError()), ('2', KeyboardInterrupt())):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                self.select(('', *tail))
            self.assertEqual(self.reset_calls, 0)
            self.assertFalse(self.report.exists())
            self.assertNotIn('destination', [c.args[0] for c in self.client.call.call_args_list])

    def test_typed_confirmation_cancellation_does_not_claim_preparation_made_no_writes(self):
        for answer in ('', 'yes', 'reset other', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer), self.assertRaises(ValueError) as caught:
                self.select(('', '2', answer))
            message = str(caught.exception)
            self.assertIn('no formatting or reset was performed', message)
            self.assertIn('Installation did not start', message)
            self.assertNotIn('no storage changes', message)
            self.assertEqual(self.reset_calls, 0)
            self.assertFalse(self.report.exists())
            self.assertNotIn('destination', [c.args[0] for c in self.client.call.call_args_list])

    def test_reset_confirmation_requires_literal_phrase_without_whitespace_normalization(self):
        for answer in (' reset partition', 'reset partition ', '\treset partition',
                       'reset partition\t', '\nreset partition', 'reset partition\n',
                       '\u00a0reset partition\u00a0', 'reset  partition', 'RESET partition'):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, 'not confirmed'):
                self.select(('', '2', answer))
            self.assertEqual(self.reset_calls, 0)
            self.assertFalse(self.report.exists())
            self.assertNotIn('destination', [c.args[0] for c in self.client.call.call_args_list])

    def test_uncertain_dispatch_or_receipt_never_retries_or_selects_destination(self):
        failures = [DeviceError('lost SSH'), KeyboardInterrupt(), EOFError(),
                    {'operation': 'format-target'}, {'boot_id': 'other'}, {'operation_id': 'other'},
                    {'target': 'other'}, {'previous_filesystem_uuid': 'other'},
                    {'filesystem_uuid': self.row['filesystem_uuid']}, {'filesystem_uuid': 'invalid'},
                    {'filesystem': 'ext3'}, {'status': 'unknown'}]
        for failure in failures:
            self.failure, self.receipt, self.reset_calls = failure, None, 0
            self.client.reset_mock()
            with self.subTest(failure=failure), self.assertRaisesRegex(RuntimeError, 'Do not retry'):
                self.select(('', '2', 'reset partition'))
            self.assertEqual(self.reset_calls, 1)
            self.assertEqual(json.loads(self.report.read_text())['status'], 'unknown')
            self.assertNotIn('destination', [c.args[0] for c in self.client.call.call_args_list])

    def test_rediscovery_failures_keep_success_receipt_and_stop_installation(self):
        actual = self.call
        for change in ('lost', 'boot', 'token', 'uuid', 'size', 'missing'):
            self.receipt, self.reset_calls = None, 0
            self.client.reset_mock()
            def response(operation, request=None, **kwargs):
                result = actual(operation, request, **kwargs)
                if operation != 'targets' or not self.receipt:
                    return result
                if change == 'lost':
                    raise DeviceError('lost SSH')
                if change == 'boot':
                    return {**result, 'boot_id': 'other'}
                if change == 'token':
                    return {**result, 'selection_token': 'before'}
                row = result['partitions'][0]
                return {**result, 'partitions': [] if change == 'missing' else [
                    {**row, **({'bytes': 1} if change == 'size' else {'filesystem_uuid': 'other'})}]}
            self.client.call.side_effect = response
            answers = iter(('', '2', 'reset partition'))
            with self.subTest(change=change), self.assertRaises((ValueError, RuntimeError)):
                select_destination(self.client, Clock(), input_fn=lambda _: next(answers), output=lambda _: None,
                                   format_report=self.report, capabilities=('reset-ext4-v1',))
            self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')
            self.assertEqual(self.reset_calls, 1)
            self.assertNotIn('destination', [c.args[0] for c in self.client.call.call_args_list])


if __name__ == '__main__':
    unittest.main()
