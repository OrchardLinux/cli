"""Guided host workflow. No hardware activation, storage parsing or shell recipes."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import uuid

from .state import exclusive_operation, private_directory
from .contracts import Adapter
from .progress import Clock, Progress
from .protocol import Client, DeviceError
from .releases import default_manifest, resolve
from .devices import choose


def ask(clock, prompt, input_fn=input):
    with clock.exclude('input'):
        return input_fn(prompt).strip()


def initialize_partition(client, clock, observation, row, report_path, *, input_fn=input, output=print):
    if report_path is None:
        raise ValueError('Formatting requires a persistent host report path')
    if row['bytes'] < observation['needed_bytes']:
        raise ValueError('Partition is too small for this image; nothing was formatted')
    output('This Linux-data partition has no recognized filesystem signatures; that does not prove it is empty.')
    output('Formatting will permanently destroy any contents of this partition. No backup will be created.')
    phrase = 'format ' + row['id']
    answer = ask(clock, f'Type "{phrase}" to confirm: ', input_fn)
    if answer != phrase:
        raise ValueError('Partition formatting was not confirmed; no storage changes were made')
    request = {'selection_token': observation['selection_token'], 'target': row['id'],
               'boot_id': observation['boot_id'], 'operation_id': uuid.uuid4().hex, 'confirmation': answer}
    report = {**request, 'status': 'unknown'}
    # Persist uncertainty before dispatch. A crash or lost SSH result must never
    # turn into an implicit second formatting request.
    save_report(report_path, report)
    try:
        receipt = client.call('format-target', request, timeout=240)
        if (receipt.get('status') != 'complete' or receipt.get('filesystem') != 'ext4' or
                any(receipt.get(key) != request[key] for key in ('boot_id', 'operation_id', 'target')) or
                not uuid.UUID(receipt['filesystem_uuid']).int):
            raise ValueError('Formatting receipt does not match the request')
    except BaseException as error:
        save_report(report_path, {**report, 'error': str(error)})
        raise RuntimeError('Formatting outcome could not be verified. Do not retry formatting; '
                           'inspect the partition and saved report: ' + str(report_path)) from error
    save_report(report_path, receipt)
    refreshed = client.call('targets')
    matches = [p for p in refreshed['partitions'] if p['id'] == row['id']]
    if (refreshed['boot_id'] != request['boot_id'] or
            refreshed['selection_token'] == request['selection_token'] or len(matches) != 1 or
            matches[0]['filesystem'] != 'ext4' or
            matches[0]['filesystem_uuid'] != receipt['filesystem_uuid'] or matches[0]['bytes'] != row['bytes']):
        raise ValueError('Formatted target changed during rediscovery; installation stopped')
    return refreshed, matches[0]


def select_destination(client, clock, *, input_fn=input, output=print, directory='linux', format_report=None):
    observation = client.call('targets')
    rows = observation['partitions']
    if not rows:
        raise ValueError('No available GPT Linux-data partition; no storage changes were made')
    output('Available Linux-data partitions:')
    for number, row in enumerate(rows, 1):
        free = f" · {row['free_bytes'] / 1024**3:.1f} GiB free" if 'free_bytes' in row else ''
        output(f"  {number}. {row['label']} · {row['bytes'] / 1024**3:.1f} GiB total{free} · {row['filesystem']}")
        if len(rows) > 1:
            output(f"     ID: {row['id']}")
    single = len(rows) == 1
    answer = ask(clock, 'Select partition number [1]: ' if single else 'Select partition number: ', input_fn)
    if single and not answer:
        answer = '1'
    if not answer.isdecimal() or not 1 <= int(answer) <= len(rows):
        raise ValueError('No valid partition was selected')
    row = rows[int(answer) - 1]
    if row['filesystem'] == 'unformatted':
        observation, row = initialize_partition(client, clock, observation, row, format_report,
                                                input_fn=input_fn, output=output)
    if row['filesystem'] != 'ext4':
        raise ValueError('Selected partition has an unsupported signature; nothing was formatted')
    while True:
        request = {'selection_token': observation['selection_token'], 'target': row['id'], 'directory': directory}
        existing = client.call('destination', request)
        replace = existing['exists']
        if replace:
            output(f'/{directory} already exists.')
            output(f'  1. Replace /{directory} — permanently delete its contents')
            output('  2. Keep it and install in another directory')
            output('  3. Cancel')
            action = ask(clock, 'Choose an action: ', input_fn)
            if action == '2':
                directory = ask(clock, 'New root directory: ', input_fn)
                continue
            if action != '1':
                raise ValueError('Installation cancelled; nothing was deleted')
        available = existing['free_bytes'] + (existing['reclaimable_bytes'] if replace else 0)
        needed = existing['needed_bytes']
        output('Installation plan:')
        output(f'  Destination: /{directory}')
        label = 'Available after replacement' if replace else 'Available'
        output(f'  {label}: {available / 1024**3:.1f} GiB')
        output(f'  Estimated needed, including margin: {needed / 1024**3:.1f} GiB')
        if available < needed or not existing['inodes_sufficient']:
            raise ValueError('Insufficient space or free inodes; nothing was deleted. '
                             'Free space or choose another partition or directory.')
        if replace:
            phrase = 'replace ' + directory
            output('No backup will be created. Other directories will not be touched.')
            output('If installation fails, the deleted contents cannot be restored.')
        else:
            phrase = 'install ' + directory
        confirmation = ask(clock, f'Type "{phrase}" to confirm: ', input_fn)
        if confirmation != phrase:
            raise ValueError('Installation destination was not confirmed')
        return {**request, 'replace': replace, 'destination_identity': existing['identity']}


def save_report(path, report):
    # Persist commitment before handoff; a reporting/transport failure must not
    # erase evidence that the root was successfully installed.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.install-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(report, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def wait_ready(client, progress, timeout=90, *, context='Root installation completed'):
    deadline = time.monotonic() + timeout
    last_report = None
    last_error = ''
    while True:
        try:
            status = client.call('status', timeout=8)
            connected = True
            last_error = ''
            if status.get('failure'):
                raise RuntimeError('Device reported a failure; use orchard diagnostics: ' + str(status['failure']))
            if status['ready']:
                return status
        except DeviceError as error:
            if error.reported:
                raise
            last_error = str(error)
            connected = False
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(context + ', but system readiness could not be confirmed. '
                               'Do not reinstall solely for this reporting timeout. ' + last_error)
        message = ('Waiting for system readiness; diagnostics connected' if connected else
                   'Waiting for the installed system USB diagnostic connection')
        if message != last_report:
            progress.phase(message)
            last_report = message
        time.sleep(0.5)


def finish_ssh_access(client, adapter: Adapter, status, report, path, clock, input_fn, progress, *, scope):
    if not scope.matches(status) or status.get('profile') != scope.device.profile:
        raise ValueError('Readiness identity differs from the credential scope')
    # Installation success is durable before asking or touching diagnostics.
    report['ssh_access'] = 'pending'
    save_report(path, report)
    progress.phase('System readiness verified')
    try:
        answer = ask(clock, 'Keep SSH access for this boot session? [y/N] ', input_fn)
    except (EOFError, KeyboardInterrupt):
        answer = ''
    if answer.lower() in ('y', 'yes'):
        report['ssh_access'] = 'retained'
        save_report(path, report)
        return
    report['ssh_access'] = 'revoking'
    save_report(path, report)
    try:
        result = client.call('revoke-ssh', {'boot_id': scope.boot_id, 'ecid': scope.device.ecid,
                                            'profile': scope.device.profile, 'release': report['release']}, timeout=30)
        if (result.get('ssh_access') != 'revoked' or not scope.matches(result) or
                result.get('profile') != scope.device.profile or result.get('release') != report['release']):
            raise ValueError('Unexpected SSH retirement receipt')
    except (DeviceError, ValueError, OSError, KeyError) as error:
        report['ssh_access'] = 'unknown'
        report['ssh_error'] = str(error)
        save_report(path, report)
        raise RuntimeError('System readiness succeeded, but SSH retirement was not confirmed. '
                           'Do not reinstall. Reboot to discard this boot session. ' + str(error)) from error
    report['ssh_access'] = 'revoked'
    save_report(path, report)
    # The active transport's temporary key is removed by its context manager.
    # Delete the retained diagnostic copy only after the device's final receipt.
    try:
        adapter.forget_access(scope)
    except (OSError, ValueError) as error:
        report['credential_cleanup_error'] = str(error)
        save_report(path, report)
        raise RuntimeError('SSH access was revoked, but local credential cleanup failed: ' + str(error)) from error
    progress.phase('SSH access revoked for this boot; cached credentials removed')


def caveats(adapter):
    """What to know before installing: features that are not working, free-text caveats."""
    import orchard_hardware
    labels = orchard_hardware.features()
    states = {'partial': 'partial', 'untested': 'untested', 'missing': 'not working yet'}
    rows = [f'- {label}: {states[adapter.FEATURES[name]]}' for name, label in labels.items()
            if adapter.FEATURES.get(name) in states]
    lines = ['Caveats:', *rows, *(f'- {text}' for text in adapter.CAVEATS)] if rows or adapter.CAVEATS else []
    if adapter.STATUS != 'supported':
        lines.append('This device has not been tested yet, proceed at your own risk!')
    return lines


@exclusive_operation
def install(manifest=None, *, input_fn=input, progress=None):
    clock, progress = Clock(), progress or Progress()
    adapter, device = choose()  # Definition of installation timing begins here.
    if not adapter.INSTALL:
        raise ValueError(f'Installing on the {adapter.NAME} is not available yet; '
                         'RAM boot is available for testing')
    progress.phase(f'{adapter.NAME} ({adapter.STATUS})')
    for line in caveats(adapter):
        progress.phase(line)
    answer = ask(clock, f'Install Linux on {adapter.NAME}? [Y/n] ', input_fn)
    if answer.lower() not in ('', 'y', 'yes'):
        progress.phase('Cancelled; no download or device mutation performed')
        return {'cancelled': True}
    record = assets = None
    firmware = None
    if not device['resumed']:
        progress.phase('Downloading installation files')
        with ThreadPoolExecutor(max_workers=2) as pool:
            firmware_future = pool.submit(adapter.firmware_inputs, clock, progress.task('Firmware'))
            record, assets = resolve(manifest or default_manifest(adapter.PROFILE), adapter.PROFILE, progress, clock=clock)
            firmware = firmware_future.result()
        progress.phase('Downloads verified; firmware extracted')
        adapter.boot(device, assets, record, progress)
    progress.phase('Waiting for authenticated recovery SSH')
    with adapter.connect(device) as connection:
        private = private_directory(connection.scope.root)
        identity = {'profile': connection.scope.device.profile, 'ecid': connection.scope.device.ecid,
                    'boot_id': connection.scope.boot_id}
        with tempfile.TemporaryDirectory(prefix='installation-', dir=private) as directory:
            work = Path(directory)
            client = Client(connection, progress)
            status = client.call('status')
            if status.get('installation_attempted') or status.get('failure'):
                if 'abort' in status.get('capabilities', []):
                    progress.phase('Cleaning up the previous failed attempt')
                    try:
                        result = client.call('abort')
                    except DeviceError as error:
                        raise RuntimeError(str(error) + '\n' + adapter.REBOOT_MESSAGE) from error
                    raise RuntimeError('The previous installation attempt has ended.\n' + result['message'])
                raise RuntimeError('This older recovery cannot clean up the failed attempt. '
                                   'Staging was retained; the corrected recovery will clean it before target selection.\n' +
                                   adapter.REBOOT_MESSAGE)
            if record is None:
                record, assets = resolve(manifest or default_manifest(adapter.PROFILE), adapter.PROFILE, progress, clock=clock)
            assert assets is not None  # Resolution always supplies record and verified paths together.
            if status['release'] != record['release']:
                raise ValueError('Running recovery is not the selected release.\n' + adapter.REBOOT_MESSAGE)
            if firmware is None:
                firmware = adapter.firmware_inputs(clock, progress)
            progress.phase('Preparing device hardware…')
            mixin, mixin_record = adapter.prepare_mixin(firmware, work / 'mixin')
            with mixin.open('rb') as source:
                client.call('prepare', {'release': record['release'], **mixin_record},
                            source=source, size=mixin_record['bytes'], timeout=180)
            root_path = record.get('default_root', '/linux')
            destination = select_destination(client, clock, input_fn=input_fn, directory=root_path.lstrip('/'),
                                             format_report=private / 'last-format.json')
            progress.phase('Installing ' + record.get('name', 'Linux') + '…')
            with assets['root'].open('rb') as source:
                result = client.call('install', {**destination, **record['assets']['root']},
                                     source=source, size=record['assets']['root']['bytes'], timeout=1200)
            snapshot = clock.result()
            save_report(private / 'last-install.json', {
                **identity, 'installed': True, 'ready': None, 'release': record['release'],
                'resumed': bool(device['resumed']), 'device_install': result,
                'timing': {'install_seconds': None, 'root_commit_elapsed_seconds': snapshot['elapsed_seconds'],
                           'excluded_seconds': snapshot['excluded_seconds']}})
            progress.phase('Starting installed system')
            try:
                client.call('handoff', timeout=20)
            except DeviceError as error:
                if error.reported:
                    raise
                # Handoff can retire SSH after accepting the request. Inspect
                # readiness using the SAME pinned credentials, never resend it.
                progress.phase('Handoff connection retired; checking system state')
            confirmed = wait_ready(client, progress)
            timing = clock.result()  # Stop at readiness, not after host cleanup.
            report = {**identity, 'installed': True, 'ready': True, 'resumed': bool(device['resumed']),
                      'release': record['release'], 'device_install': result, 'timing': timing}
            path = private / 'last-install.json'
            finish_ssh_access(client, adapter, confirmed, report, path, clock, input_fn, progress,
                              scope=connection.scope)
    save_report(path, report)
    progress.phase(record.get('name', 'Linux') + ' is ready')
    return report
