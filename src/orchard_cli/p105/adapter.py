"""P105 policy for the native Orchard boot and pinned recovery transport."""
import base64
from contextlib import AbstractContextManager, contextmanager
import hashlib
import io
import json
from pathlib import Path
import tarfile
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast

from orchard_hardware import firmware_path
from orchard_cli.contracts import (BootAssets, BootRequest, Connection, MixinReceipt,
                                   PrivateCapture, SelectedDevice)
from orchard_cli.progress import Clock, Progress
from orchard_cli.state import BootState, DeviceState, AmbiguousDevices
from orchard_cli.transports import ssh
from .config import PROFILE, NAME, PRODUCT, regular, settings
from . import usb, network
from .boot import boot

REBOOT_MESSAGE = 'Return the iPad to iOS, then start a fresh Orchard P105 boot.'


def list_devices() -> List[Dict[str, Any]]:
    return usb.observations()


def discover() -> SelectedDevice:
    rows = list_devices()
    if len(rows) > 1:
        raise AmbiguousDevices('Connect exactly one supported P105 device')
    if len(rows) != 1:
        raise ValueError('Connect exactly one supported P105 device')
    row = rows[0]
    DeviceState(PROFILE, row['ecid'])
    if row['mode'] == 'linux':
        row = usb.recovery()
        BootState(DeviceState(PROFILE, row['ecid']), row['boot_id'])
    return cast(SelectedDevice, {**row, 'resumed': row['mode'] == 'linux'})


@contextmanager
def connect(device: SelectedDevice) -> Iterator[Connection]:
    endpoint = usb.recovery()
    if endpoint['ecid'] != device['ecid']:
        raise ValueError('Selected P105 changed before SSH bootstrap')
    scope = BootState(DeviceState(PROFILE, device['ecid']), endpoint['boot_id'])
    network.ensure(scope)
    with ssh.session(scope.device, usb.recovery, expected_boot=device.get('boot_id')) as connection:
        yield connection


def connect_saved(device: SelectedDevice) -> AbstractContextManager[Connection]:
    if device['mode'] != 'linux' or not device.get('boot_id'):
        raise ValueError('P105 diagnostics require the selected running Linux boot')
    return ssh.saved_session(DeviceState(PROFILE, device['ecid']), usb.recovery,
                             expected_boot=device['boot_id'])


def forget_access(scope: BootState) -> None:
    if scope.device.profile != PROFILE:
        raise ValueError('P105 access scope differs')
    ssh.forget_access(scope)


def capture_inputs(device: Optional[SelectedDevice] = None) -> PrivateCapture:
    device = device or discover()
    if device['mode'] == 'ios':
        usb.verify_ios(dict(device))
    elif device['mode'] == 'linux':
        observed = usb.recovery()
        if observed['ecid'] != device['ecid'] or observed['boot_id'] != device.get('boot_id'):
            raise ValueError('Selected P105 changed before private input capture')
    else:
        raise ValueError('Capture P105 inputs from its verified iOS or Linux environment')
    config = settings()
    calibration = config['calibration']
    if calibration['ecid'] != device['ecid'] or config['target']['ecid'] != device['ecid']:
        raise ValueError('Panel calibration record belongs to another device')
    data = regular(Path(calibration['file']), private=True).read_bytes()
    if not 0 < len(data) <= 65536 or hashlib.sha256(data).hexdigest() != calibration['sha256']:
        raise ValueError('Owner panel calibration differs from its capture receipt')
    record = {'schema': 1, 'kind': 'p105-mtcl', 'profile': PROFILE, 'ecid': device['ecid'],
              'calibration': base64.b64encode(data).decode(), 'sha256': calibration['sha256']}
    return {'kind': 'p105-mtcl', 'profile': PROFILE, 'ecid': device['ecid'],
            'data': (json.dumps(record)+'\n').encode()}


def firmware_inputs(clock: Clock, progress: Progress, *, allow_download: bool = True) -> Path:
    path = Path(settings()['firmware_root'])
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Select the verified private P105 firmware root')
    recipe = json.loads(firmware_path(PROFILE).read_text())
    for entry in recipe['files'] + recipe['generated']:
        source = path / entry['destination']
        if not source.resolve().is_relative_to(path.resolve()):
            raise ValueError('Firmware escapes the owner root')
        if hashlib.sha256(regular(source).read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('P105 firmware differs from the pinned recipe: ' + entry['name'])
    return path


def prepare_mixin(inputs: Path, firmware_dir: Path, outdir: Path) -> Tuple[Path, MixinReceipt]:
    record = json.loads(regular(inputs, private=True).read_text())
    if record.get('kind') != 'p105-mtcl' or record.get('profile') != PROFILE or record.get('schema') != 1:
        raise ValueError('Expected a P105 MtCl capture record')
    DeviceState(PROFILE, record['ecid'])
    data = base64.b64decode(record['calibration'], validate=True)
    if not 0 < len(data) <= 65536 or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('P105 panel capture integrity check failed')
    recipe = json.loads(firmware_path(PROFILE).read_text())
    payload = {'lib/firmware/mtcal.bin': data}
    for entry in recipe['files']+recipe['generated']:
        path = firmware_dir / entry['destination']
        if not path.resolve().is_relative_to(firmware_dir.resolve()):
            raise ValueError('Firmware escapes the owner root')
        value = regular(path).read_bytes()
        if hashlib.sha256(value).hexdigest() != entry['sha256']:
            raise ValueError('P105 firmware differs from the pinned recipe')
        payload[entry['destination']] = value
    payload['manifest.json'] = json.dumps({'schema': 1, 'profile': PROFILE, 'ecid': record['ecid'],
        'recipe_sha256': hashlib.sha256(firmware_path(PROFILE).read_bytes()).hexdigest()}).encode()
    if outdir.exists() or outdir.is_symlink():
        raise ValueError('Choose a fresh private mixin directory')
    outdir.mkdir(parents=True, mode=0o700)
    output = outdir/'p105-mixin.tar.gz'
    with tarfile.open(output, 'w:gz') as archive:
        for name, value in sorted(payload.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(value), 0o644
            archive.addfile(member, io.BytesIO(value))
    output.chmod(0o600)
    return output, {'schema': 1, 'kind': 'private-mixin', 'bytes': output.stat().st_size,
                    'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
