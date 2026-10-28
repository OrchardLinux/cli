"""Launch an existing installation through a verified shared recovery image."""
from pathlib import Path
import tempfile

from .devices import choose
from .progress import Clock, Progress
from .protocol import Client, DeviceError
from .releases import resolve, launch_manifest
from .service import ask, save_report, wait_ready, finish_ssh_access
from .state import exclusive_operation, private_directory


@exclusive_operation
def launch(manifest=None, *, directory=None, target=None, allow_firmware_download=False,
           input_fn=input, progress=None, profile=None):
    clock, progress = Clock(), progress or Progress()
    adapter, device = choose(profile) if profile else choose()
    if ask(clock, f'Launch an installed system on {adapter.NAME}? [y/N] ', input_fn).lower() not in ('y','yes'):
        return {'cancelled':True}
    manifest = manifest if manifest is not None else launch_manifest(adapter.PROFILE)
    record, assets = resolve(manifest, adapter.PROFILE, progress, clock=clock, purpose='boot')
    if not device['resumed']:
        adapter.boot(device, assets, record, progress)
    with adapter.connect(device) as connection:
        private = private_directory(connection.scope.root)
        path = private / 'last-launch.json'
        client = Client(connection, progress)
        status = client.call('status')
        if (status.get('mode') != 'launcher' or status['release'] != record['release'] or
                status.get('failure') or status.get('installation_attempted')):
            raise ValueError('A fresh boot of the selected shared launcher is required; no launch was attempted')
        firmware = adapter.firmware_inputs(clock, progress, allow_download=allow_firmware_download)
        with tempfile.TemporaryDirectory(prefix='launch-',dir=private) as temporary:
            work = Path(temporary)
            adt = work / 'device.adt'
            capture = adapter.capture_inputs(device)
            if capture['profile'] != adapter.PROFILE or capture['ecid'] != device['ecid']:
                raise ValueError('Private input capture differs from the selected device')
            adt.write_bytes(capture['data'])
            adt.chmod(0o600)
            mixin, receipt = adapter.prepare_mixin(adt,firmware,work/'mixin')
            with mixin.open('rb') as source:
                client.call('prepare',{'release':record['release'],**receipt},source=source,
                            size=receipt['bytes'],timeout=180)
        observation = client.call('installations',timeout=180)
        if observation['boot_id'] != connection.scope.boot_id:
            raise ValueError('Installation discovery belongs to another boot')
        rows = [r for r in observation['installations'] if
                (directory is None or r['directory']==directory.lstrip('/')) and
                (target is None or r['target']==target)]
        if not rows:
            raise ValueError('No matching compatible installation; nothing was formatted or installed. '+
                             str(observation.get('rejected',[])))
        for number,row in enumerate(rows,1):
            print(f"  {number}. /{row['directory']} · {row['receipt']['release']} · {row['target']}")
        answer = ask(clock,'Select installation [1]: ' if len(rows)==1 else 'Select installation: ',input_fn)
        answer = '1' if not answer and len(rows)==1 else answer
        if not answer.isdecimal() or not 1<=int(answer)<=len(rows):
            raise ValueError('No installation selected')
        selected = rows[int(answer)-1]
        phrase = 'launch '+selected['directory']
        if ask(clock,f'Type "{phrase}" to start this installation: ',input_fn) != phrase:
            raise ValueError('Launch was not confirmed')
        request = {'target':selected['target'],'directory':selected['directory'],
                   'selection_token':observation['selection_token'],'boot_id':observation['boot_id'],
                   'confirmation':phrase}
        identity = {'profile':adapter.PROFILE,'ecid':connection.scope.device.ecid,'boot_id':connection.scope.boot_id}
        report = {**identity,'operation':'launch','ready':None,'release':selected['receipt']['release'],
                  'installation':selected['receipt'],'launch_status':'unknown','request':request}
        save_report(path,report)
        try:
            result = client.call('launch',request,timeout=60)
            if result.get('requested') is not True or result.get('receipt')!=selected['receipt']:
                raise ValueError('Launch receipt differs from selection')
        except DeviceError as error:
            if error.reported:
                raise
            progress.phase('Launch connection retired; checking system state without replaying request')
        confirmed = wait_ready(client,progress,context='Installed-system launch was requested')
        if confirmed['release']!=selected['receipt']['release'] or not connection.scope.matches(confirmed):
            raise ValueError('Ready system differs from selected installation')
        report.update(ready=True,launch_status='complete',timing=clock.result())
        save_report(path,report)
        finish_ssh_access(client,adapter,confirmed,report,path,clock,input_fn,progress,scope=connection.scope)
    return report
