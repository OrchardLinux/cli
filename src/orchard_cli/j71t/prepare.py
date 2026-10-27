"""Prepare verified J71t firmware from explicit inputs into an explicit output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from orchard_cli.staging import stage as stage_firmware
from orchard_hardware.apple.adt import validate as validate_adt
from orchard_cli.integrity import digest
from .calibration import GENERATOR, prepare as prepare_calibration

from .config import MANIFEST, APPLE_PRODUCT, BOARD_CONFIG, PROFILE


def verify(directory: Path, manifest: Path, adt: Path) -> dict:
    record = json.loads((directory / 'firmware.json').read_text())
    device_id = f'{validate_adt(adt.read_bytes()):016x}'
    if (record.get('schema') != 1 or record.get('profile') != PROFILE or
            record.get('device_id') != device_id or record.get('adt_sha256') != digest(adt) or
            record.get('manifest_sha256') != digest(manifest)):
        raise ValueError('Prepared firmware identity differs')
    files = record['files']
    actual = set()
    for path in (directory / 'firmware').rglob('*'):
        if path.is_symlink():
            raise ValueError('Prepared firmware contains a symlink')
        if path.is_file():
            actual.add(path.relative_to(directory / 'firmware').as_posix())
    if actual != set(files):
        raise ValueError('Prepared firmware file set differs')
    for name, expected in files.items():
        path = directory / 'firmware' / name
        if path.stat().st_size != expected['bytes'] or digest(path) != expected['sha256']:
            raise ValueError(f'Prepared firmware differs: {name}')
    spec = json.loads(manifest.read_text())
    for row in spec['generated']:
        receipt = json.loads((directory / 'calibration' / (row['name'] + '.json')).read_text())
        if (receipt.get('adt_sha256') != digest(adt) or
                files[row['destination']]['sha256'] != receipt.get('sha256')):
            raise ValueError('Calibration differs from its ADT receipt: ' + row['name'])
    return record


def check_outdir(outdir: Path):
    if (outdir.is_symlink() or
            (outdir.exists() and (not outdir.is_dir() or any(outdir.iterdir())))):
        raise ValueError('Outdir must be absent or an empty directory')


def prepare(adt: Path, firmware_dir: Path, outdir: Path) -> Path:
    manifest = MANIFEST
    spec = json.loads(manifest.read_text())
    if spec.get('device') != APPLE_PRODUCT:
        raise ValueError('Wrong J71t firmware manifest')
    if any(row.get('generator') != GENERATOR for row in spec['generated']):
        raise ValueError('Unsupported J71t firmware generator')
    if not firmware_dir.is_dir() or firmware_dir.is_symlink():
        raise ValueError('Expected a regular firmware input directory')
    check_outdir(outdir)
    firmware_dir = firmware_dir.resolve()
    outdir = outdir.resolve()
    if outdir == firmware_dir or firmware_dir in outdir.parents:
        raise ValueError('Output must not be inside the firmware inputs')
    device_id = f'{validate_adt(adt.read_bytes()):016x}'
    outdir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.j71t-', dir=outdir.parent) as temporary:
        work = Path(temporary)
        calibration = work / 'calibration'
        receipts = prepare_calibration(adt, calibration, spec['device'], APPLE_PRODUCT, BOARD_CONFIG,
                                       [row['name'] for row in spec['generated']])
        count = stage_firmware(manifest, [firmware_dir, calibration], receipts, work / 'firmware')
        if count != len(spec['files']) + len(spec['generated']):
            raise ValueError('Incomplete J71t firmware stage')
        records = {}
        for path in sorted((work / 'firmware').rglob('*')):
            if path.is_file():
                records[path.relative_to(work / 'firmware').as_posix()] = {
                    'bytes': path.stat().st_size, 'sha256': digest(path)}
        receipt = {'schema': 1, 'profile': PROFILE, 'device_id': device_id,
                   'manifest_sha256': digest(manifest), 'adt_sha256': digest(adt), 'files': records}
        (work / 'firmware.json').write_text(json.dumps(receipt, indent=2) + '\n')
        verify(work, manifest, adt)
        check_outdir(outdir)
        os.replace(work, outdir)
    return outdir
