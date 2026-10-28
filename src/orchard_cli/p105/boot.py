"""Qualified direct-iBEC transport with a durable record before each mutation."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
from typing import Any, Dict, Iterator, Mapping, Tuple, cast

from orchard_cli.contracts import BootAssets, BootRequest, SelectedDevice
from orchard_cli.progress import Progress
from orchard_cli.state import DeviceState, private_directory
from . import usb
from .config import PROFILE, regular, settings, tool

KLOADER_SHA = 'bec212b4d99a1e3b9fa4add663fa884a2435045d1e47051415f4e26e197f8132'
IBEC_SHA = 'e987b101800bedf03c086aa55854ff594fcce1b0f55ea1424f3ccf180a40abce'


def save(path: Path, value: Dict[str, Any]) -> None:
    temporary = path.with_suffix('.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class IOSConnection:
    def __init__(self, device: SelectedDevice, port: int, config: Dict[str, Any]):
        self.device, self.port, self.config = device, port, config

    def run(self, command: str, content: bytes = b'', *, transition: bool = False) -> bytes:
        usb.verify_ios(dict(self.device))
        ios = self.config['ios']
        hosts = regular(Path(ios['known_hosts']), private=True)
        argv = ['ssh', '-F', '/dev/null', '-p', str(self.port), '-o', 'HostKeyAlias=p105-ios-usb',
                '-o', 'UserKnownHostsFile=' + str(hosts), '-o', 'StrictHostKeyChecking=yes',
                '-o', 'HostKeyAlgorithms=+ssh-rsa', '-o', 'ConnectTimeout=8',
                '-o', 'IdentitiesOnly=yes', '-o', 'ControlMaster=no']
        read_fd = write_fd = -1
        try:
            if 'identity' in ios:
                argv += ['-i', str(regular(Path(ios['identity']), private=True)), '-o', 'BatchMode=yes']
            else:
                password = regular(Path(ios['password_file']), private=True).read_bytes().strip()
                if not password or len(password) > 1024 or b'\n' in password or b'\r' in password:
                    raise ValueError('Invalid private iOS SSH password input')
                read_fd, write_fd = os.pipe()
                os.write(write_fd, password+b'\n')
                os.close(write_fd)
                write_fd = -1
                argv = [tool('sshpass'), '-d', str(read_fd), *argv, '-o', 'PreferredAuthentications=password',
                        '-o', 'NumberOfPasswordPrompts=1']
            result = subprocess.run([*argv, 'root@127.0.0.1', command], input=content,
                capture_output=True, timeout=45, pass_fds=(read_fd,) if read_fd >= 0 else ())
            if transition:
                if result.returncode not in (0, 255) or b'ORCHARD_DIRECT_IBEC_EXEC' not in result.stdout.splitlines():
                    raise RuntimeError('iBEC transition outcome is unknown; inspect the device before another boot')
            elif result.returncode:
                raise RuntimeError('Pinned iOS SSH operation failed')
            return result.stdout
        finally:
            for fd in (read_fd, write_fd):
                if fd >= 0:
                    os.close(fd)


@contextmanager
def ios_connection(device: SelectedDevice) -> Iterator[IOSConnection]:
    config = settings()
    if config['target'] != {'udid': device.get('udid'), 'ecid': device['ecid']}:
        raise ValueError('Owner configuration belongs to another iPad')
    usb.verify_ios(dict(device))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    proxy = subprocess.Popen([tool('iproxy'), '-u', device['udid'], str(port), '22', '-s', '127.0.0.1'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+10
        while True:
            if proxy.poll() is not None:
                raise RuntimeError('Selected iOS USB proxy exited')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Selected iOS USB proxy timed out')
                time.sleep(0.1)
        yield IOSConnection(device, port, config)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()


def verify_assets(device: SelectedDevice, assets: BootAssets, manifest: BootRequest) -> None:
    paths = cast(Mapping[str, Path], assets)
    if manifest.get('target_ecid') != device['ecid']:
        raise ValueError('Private boot manifest belongs to another device')
    components = Path(settings()['components'])
    receipt = json.loads(regular(components/'components.json', private=True).read_text())
    if (receipt.get('device_ecid') != device['ecid'] or receipt.get('mode') != 'orchard-recovery' or
            receipt.get('private') is not True or receipt.get('publishable') is not False or
            receipt.get('flash_controller') != 'disabled'):
        raise ValueError('P105 components lack the selected recovery identity or flash fence')
    for role, name in (('recovery','staging-bundle.bin'),('loader','staging-loader.bin')):
        if receipt['components'][name]['sha256'] != manifest['assets'][role]['sha256']:
            raise ValueError('Boot manifest differs from the verified P105 component receipt')
    for role in ('recovery', 'loader', 'ibec', 'kloader'):
        path = regular(paths[role])
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['assets'][role]['sha256']:
            raise ValueError('P105 boot asset changed: ' + role)
    for role, digest in (('kloader', KLOADER_SHA), ('ibec', IBEC_SHA)):
        if manifest['assets'][role]['sha256'] != digest:
            raise ValueError('P105 requires the qualified ' + role + ' recipe')
    loader = assets['loader'].read_bytes()
    bundle = assets['recovery'].read_bytes()
    if not 0 < len(loader) <= 32768 or not bundle.startswith(loader.ljust(32768, b'\0')):
        raise ValueError('P105 bundle and staging loader differ')


def upload(device: SelectedDevice, path: Path) -> None:
    usb.ibec(device['ecid'])
    result = subprocess.run([tool('irecovery'), '-i', str(device['ecid']), '-f', str(path)],
                            capture_output=True, timeout=180)
    if result.returncode or not re.search(rb'(?<![0-9])100(?:\.0+)?%', result.stdout):
        raise RuntimeError('P105 upload outcome is unknown; inspect the device before another boot')


def pending_attempt(folder: Path, device: SelectedDevice, manifest: BootRequest) -> Tuple[Path, Dict[str, Any]]:
    candidates = list(folder.glob('*.json'))
    if not candidates:
        raise ValueError('iBEC requires a saved Orchard transition; start from iOS')
    receipt = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    regular(receipt, private=True)
    record = json.loads(receipt.read_text())
    age = time.time() - receipt.stat().st_mtime
    if (not isinstance(record, dict) or record.get('phase') != 'awaiting-ibec' or
            record.get('ecid') != device['ecid'] or record.get('assets') != dict(manifest['assets']) or
            not 0 <= age <= 900):
        raise ValueError('Only the latest verified iBEC wait can continue; inspect unknown outcomes before a fresh iOS boot')
    return receipt, record


def enter_ios(device: SelectedDevice, assets: BootAssets, manifest: BootRequest,
              receipt: Path, record: Dict[str, Any], progress: Progress) -> None:
    paths = cast(Mapping[str, Path], assets)
    with ios_connection(device) as connection:
        directory = '/tmp/orchard-p105-ibec-'+secrets.token_hex(16)
        connection.run('umask 077; mkdir '+directory)
        for role, name in (('kloader', 'kloader'), ('ibec', 'ibec.raw')):
            content = paths[role].read_bytes()
            target = directory+'/'+name
            connection.run('set -C; umask 077; cat > '+target+' && chmod '+('700' if role == 'kloader' else '600')+' '+target, content)
            if connection.run('cat '+target) != content:
                raise ValueError('Staged iOS payload differs from its source')
        record['phase'] = 'transition-outcome-unknown'
        save(receipt, record)
        checks = ['test -d '+directory, 'test ! -L '+directory]
        for role, name in (('kloader', 'kloader'), ('ibec', 'ibec.raw')):
            target = directory+'/'+name
            checks.append('test -f '+target+'; test ! -L '+target+'; d=$(/usr/bin/openssl dgst -sha256 < '+target+'); '
                          '[ "${d##* }" = "'+manifest['assets'][role]['sha256']+'" ]')
        checks += ['echo ORCHARD_DIRECT_IBEC_EXEC', 'exec '+directory+'/kloader '+directory+'/ibec.raw']
        progress.phase('Entering iBEC on the selected iPad')
        connection.run('set -eu; '+'; '.join(checks), transition=True)
    record['phase'] = 'awaiting-ibec'
    save(receipt, record)


def boot(device: SelectedDevice, assets: BootAssets, manifest: BootRequest, progress: Progress) -> None:
    if manifest.get('target_ecid') != device['ecid']:
        raise ValueError('Private boot manifest belongs to another device')
    if device['mode'] == 'linux':
        current = usb.recovery()
        if current['ecid'] != device['ecid'] or current['boot_id'] != device.get('boot_id'):
            raise ValueError('Selected running P105 boot changed')
        return
    verify_assets(device, assets, manifest)
    paths = cast(Mapping[str, Path], assets)
    folder = private_directory(DeviceState(PROFILE, device['ecid']).root / 'boot-attempts')
    if device['mode'] == 'ibec':
        receipt, record = pending_attempt(folder, device, manifest)
        progress.phase('Continuing the saved iBEC wait for the same verified boot files')
    elif device['mode'] == 'ios':
        record = {'ecid': device['ecid'], 'assets': dict(manifest['assets']), 'phase': 'validated'}
        receipt = folder / (secrets.token_hex(16)+'.json')
        save(receipt, record)
        enter_ios(device, assets, manifest, receipt, record, progress)
    else:
        raise ValueError('Start a P105 boot from iOS or its saved iBEC transition')
    progress.phase('Reconnect the iPad USB cable once; waiting for the selected iBEC')
    deadline = time.monotonic()+600
    while True:
        try:
            usb.ibec(device['ecid'])
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise RuntimeError('iBEC wait expired; reconnect USB and repeat this command to continue its saved wait')
            time.sleep(1)
    for role in ('recovery', 'loader'):
        record['phase'] = role+'-upload-outcome-unknown'
        save(receipt, record)
        upload(device, paths[role])
    record['phase'] = 'awaiting-linux'
    save(receipt, record)
    progress.phase('Waiting for identified P105 RAM recovery')
    deadline = time.monotonic()+120
    while True:
        try:
            current = usb.recovery()
            if current['ecid'] != device['ecid']:
                raise RuntimeError('Another device appeared after the P105 upload')
            record.update(phase='complete', boot_id=current['boot_id'])
            save(receipt, record)
            device.update(cast(SelectedDevice, {**current, 'resumed': True}))
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise RuntimeError('P105 recovery did not enumerate; retain the saved boot attempt for inspection')
            time.sleep(0.5)
