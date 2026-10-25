"""Verified private firmware cache and data-only mixin packaging, for any profile."""
import hashlib
import io
import json
import shutil
import tarfile

from .downloads import download
from .integrity import digest


def missing_inputs(locations, expected):
    return [name for name, path in locations.items()
            if path.is_symlink() or not path.is_file() or digest(path) != expected[name]]


def ensure_archive(url, size, checksum, archive, clock, progress, *, label):
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists() or archive.stat().st_size != size:
        if shutil.disk_usage(archive.parent).free < 2 * size:
            raise ValueError('Insufficient space for firmware download and extraction')
        progress.phase(f'Downloading {label} (excluded from install time)')
        with clock.exclude('download'):
            download(url, size, archive)
    if archive.is_symlink() or digest(archive) != checksum:
        raise ValueError(f'{label} digest differs; retained for inspection')
    return archive


def store_input(path, data, checksum):
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError('Extracted firmware differs: ' + path.name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('Linked firmware cache entry refused')
    path.write_bytes(data)
    path.chmod(0o600)


def pack_mixin(prepared, metadata, outdir):
    """Package the caller's identity metadata without interpreting its hardware format."""
    if prepared.is_symlink() or (prepared / 'firmware').is_symlink():
        raise ValueError('Linked private firmware root refused')
    entries = {}
    for name in metadata['files']:
        if (not isinstance(name, str) or not name.startswith('lib/firmware/') or
                '\\' in name or any(part in ('', '.', '..') for part in name.split('/'))):
            raise ValueError('Private mixin may contain only normalized firmware data paths')
        path = prepared / 'firmware'
        for part in name.split('/'):
            path = path / part
            if path.is_symlink():
                raise ValueError('Linked private firmware refused')
        entries[name] = path.read_bytes()
    entries['mixin.json'] = json.dumps(metadata, sort_keys=True).encode()
    archive = outdir / 'mixin.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        for name, data in sorted(entries.items()):
            if name != 'mixin.json' and not name.startswith('lib/firmware/'):
                raise ValueError('Private mixin may contain only firmware data')
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o600
            bundle.addfile(member, io.BytesIO(data))
    archive.chmod(0o600)
    record = {'schema': 1, 'kind': 'private-mixin', 'bytes': archive.stat().st_size,
              'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (outdir / 'mixin.json').write_text(json.dumps(record, indent=2) + '\n')
    (outdir / 'mixin.json').chmod(0o600)
    return archive, record
