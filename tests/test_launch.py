"""Shared launch uses boot artifacts, fresh firmware and device discovery only."""
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchard_cli import launch
from orchard_cli.contracts import Connection
from orchard_cli.progress import Progress
from orchard_cli.protocol import DeviceError
from orchard_cli.state import BootState, DeviceState

BOOT='11111111-1111-4111-8111-111111111111'


class LaunchFlow(unittest.TestCase):
    def run_flow(self, *, lost=False, discovered=True, answer='launch minimal', manifest='boot.toml'):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,ORCHARD_HOME=directory):
            scope=BootState(DeviceState('j71b',1),BOOT)
            connection=Connection(['not-ssh'],scope,Mock())
            receipt={'release':'alpine','root_sha256':'a'*64,'directory':'minimal'}
            adapter=Mock(PROFILE='j71b',NAME='iPad')
            adapter.connect.return_value=nullcontext(connection)
            adapter.capture_inputs.return_value={'kind':'apple-adt','profile':'j71b','ecid':1,'data':b'fresh ADT'}
            def mixin(adt,firmware,dest):
                dest.mkdir();path=dest/'mixin.tar.gz';path.write_bytes(b'x')
                return path,{'bytes':1,'sha256':'a'*64}
            adapter.prepare_mixin.side_effect=mixin
            row={'target':'partition','directory':'minimal','receipt':receipt}
            client=Mock()
            def call(operation,request=None,**kwargs):
                if operation=='status':return {'mode':'launcher','release':'launcher','installation_attempted':False}
                if operation=='prepare':return {'status':'complete'}
                if operation=='installations':return {'boot_id':BOOT,'selection_token':'token',
                                                       'installations':[row] if discovered else []}
                if operation=='launch':
                    if lost:raise DeviceError('connection retired')
                    return {'requested':True,'receipt':receipt}
                raise AssertionError('Launch must not use an installation/storage mutation RPC: '+operation)
            client.call.side_effect=call
            ready={'ready':True,'release':'alpine','ecid':1,'boot_id':BOOT,'profile':'j71b'}
            with patch.object(launch,'choose',return_value=(adapter,{'resumed':False,'ecid':1})), \
                 patch.object(launch,'resolve',return_value=({'release':'launcher'}, {'recovery':Path('boot')})) as resolve, \
                 patch.object(launch,'launch_manifest',return_value='https://example.invalid/j71b.boot.toml') as selected_manifest, \
                 patch.object(launch,'Client',return_value=client), \
                 patch.object(launch,'wait_ready',return_value=ready), \
                 patch.object(launch,'finish_ssh_access') as finish:
                answers=iter(['y','',answer])
                if not discovered or answer!='launch minimal':
                    with self.assertRaises(ValueError):launch.launch(manifest,input_fn=lambda _:next(answers),progress=Progress(io.StringIO()))
                    self.assertNotIn('launch',[c.args[0] for c in client.call.call_args_list])
                    return
                report=launch.launch(manifest,input_fn=lambda _:next(answers),progress=Progress(io.StringIO()))
                self.assertTrue(report['ready'])
                self.assertEqual(report['operation'],'launch')
                self.assertNotIn('installed',report)
                self.assertEqual(sum(c.args[0]=='launch' for c in client.call.call_args_list),1)
                self.assertEqual(resolve.call_args.kwargs['purpose'],'boot')
                if manifest is None:
                    selected_manifest.assert_called_once_with('j71b')
                    self.assertEqual(resolve.call_args.args[0], 'https://example.invalid/j71b.boot.toml')
                else:
                    selected_manifest.assert_not_called()
                    self.assertEqual(resolve.call_args.args[0], manifest)
                adapter.firmware_inputs.assert_called_once()
                self.assertFalse(adapter.firmware_inputs.call_args.kwargs['allow_download'])
                finish.assert_called_once()
                saved=json.loads((scope.root/'last-launch.json').read_text())
                self.assertTrue(saved['ready'])

    def test_boot_only_resolution_and_no_install_or_format(self):self.run_flow()
    def test_no_manifest_selects_assets_for_detected_device(self):self.run_flow(manifest=None)

    def test_launch_source_is_device_owned(self):
        from orchard_cli import releases
        from orchard_hardware import device
        expected = device('j71b')['boot']['launch_manifest']
        self.assertEqual(releases.launch_manifest('j71b'), expected)
        with patch.object(releases, 'hardware_device', return_value={'boot': {}}):
            with self.assertRaisesRegex(ValueError, 'No launch assets'):
                releases.launch_manifest('unsupported')

    def test_command_accepts_launch_without_arguments(self):
        from orchard_cli import command
        with patch('sys.argv', ['orchard', 'launch']), patch.object(launch, 'launch', return_value={}) as run:
            command.main()
        run.assert_called_once_with(None, directory=None, target=None,
                                    allow_firmware_download=False, progress=unittest.mock.ANY)
    def test_unknown_handoff_transport_is_not_replayed(self):self.run_flow(lost=True)
    def test_no_compatible_roots_stops_before_launch(self):self.run_flow(discovered=False)
    def test_missing_exact_confirmation_stops_before_launch(self):self.run_flow(answer='yes')


if __name__=='__main__':unittest.main()
