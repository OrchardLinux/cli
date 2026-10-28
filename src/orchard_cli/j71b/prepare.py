"""Prepare verified J71b firmware from explicit inputs into an explicit output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from orchard_cli.staging import stage as stage_firmware
from orchard_cli.integrity import digest
from .touch import prepare as prepare_touch

from .config import MANIFEST, APPLE_PRODUCT


def verify(directory: Path, manifest: Path) -> dict:
    record = json.loads((directory / 'firmware.json').read_text())
    if (record.get('schema') != 1 or record.get('profile') != 'j71b' or
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
    touch = json.loads((directory / 'touch/touch.json').read_text())
    if (touch.get('sha256') != files['lib/firmware/apple/j71b-fwstart.z2fw']['sha256'] or
            digest(directory / 'touch/j71b-fwstart.z2fw') != touch['sha256']):
        raise ValueError('Touch firmware differs from its receipt')
    return record


def check_outdir(outdir: Path):
    if (outdir.is_symlink() or
            (outdir.exists() and (not outdir.is_dir() or any(outdir.iterdir())))):
        raise ValueError('Outdir must be absent or an empty directory')


def prepare(firmware_dir: Path, outdir: Path) -> Path:
    manifest = MANIFEST
    spec = json.loads(manifest.read_text())
    if spec.get('device') != APPLE_PRODUCT:
        raise ValueError('Wrong J71b firmware manifest')
    if not firmware_dir.is_dir() or firmware_dir.is_symlink():
        raise ValueError('Expected a regular firmware input directory')
    check_outdir(outdir)
    firmware_dir = firmware_dir.resolve()
    outdir = outdir.resolve()
    if outdir == firmware_dir or firmware_dir in outdir.parents:
        raise ValueError('Output must not be inside the firmware inputs')
    source = next(item for item in spec['extracted_inputs'] if item['name'] == 'J71b.mtprops')
    matches = [path for path in firmware_dir.rglob(source['name'])
               if path.is_file() and not path.is_symlink() and digest(path) == source['sha256']]
    if len(matches) != 1:
        raise ValueError('Expected one verified J71b touch profile in firmware directory')
    outdir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.j71b-', dir=outdir.parent) as temporary:
        work = Path(temporary)
        touch_dir = work / 'touch'
        touch_dir.mkdir()
        prepare_touch(matches[0], touch_dir / 'j71b-fwstart.z2fw', touch_dir / 'touch.json')
        count = stage_firmware(manifest, [firmware_dir, touch_dir],
                               [touch_dir / 'touch.json'], work / 'firmware')
        expected_count = len(spec['files']) + len(spec['generated'])
        if count != expected_count:
            raise ValueError('Incomplete J71b firmware stage')
        records = {}
        for path in sorted((work / 'firmware').rglob('*')):
            if path.is_file():
                records[path.relative_to(work / 'firmware').as_posix()] = {
                    'bytes': path.stat().st_size, 'sha256': digest(path)}
        receipt = {'schema': 1, 'profile': 'j71b', 'manifest_sha256': digest(manifest), 'files': records}
        (work / 'firmware.json').write_text(json.dumps(receipt, indent=2) + '\n')
        verify(work, manifest)
        check_outdir(outdir)
        os.replace(work, outdir)
    return outdir
