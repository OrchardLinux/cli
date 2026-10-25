#!/usr/bin/env python3
"""Stage verified local firmware files using a device manifest."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil

from .integrity import digest as file_digest


def stage(manifest_path: Path, inputs: list[Path], receipt_paths: list[Path], output: Path):
    manifest = json.loads(manifest_path.read_text())
    files = manifest['files']
    generated = manifest.get('generated', [])
    receipts = {}
    for path in receipt_paths:
        receipt = json.loads(path.read_text())
        name = receipt.get('name')
        digest = receipt.get('sha256')
        size = receipt.get('bytes')
        if (not isinstance(name, str) or name in receipts or
                receipt.get('device') != manifest['device'] or
                not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
                isinstance(size, bool) or not isinstance(size, int) or size <= 0):
            raise ValueError(f'invalid or duplicate generated-file receipt: {path}')
        receipts[name] = receipt
    if set(receipts) != {item['name'] for item in generated}:
        raise ValueError('supply exactly one local receipt for each generated firmware file')
    selected = []
    destinations = set()
    names = set()
    for item in files + generated:
        name = item['name']
        digest = receipts[name]['sha256'] if item in generated else item['sha256']
        size = receipts[name]['bytes'] if item in generated else None
        destination = PurePosixPath(item['destination'])
        if (not name or name in ('.', '..') or '/' in name or name in names or
                destination.is_absolute() or '..' in destination.parts or
                not destination.parts or str(destination) in destinations or
                not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('unsafe or duplicate firmware manifest entry')
        destinations.add(str(destination))
        names.add(name)
        candidates = (p for directory in inputs
                      for p in ([directory] if directory.is_file() else directory.rglob(name))
                      if p.is_file() and not p.is_symlink() and p.name == name)
        matches = [p for p in candidates if (size is None or p.stat().st_size == size)
                   and file_digest(p) == digest]
        if len(matches) != 1:
            raise ValueError(f'expected one matching local input for {name}; found {len(matches)}')
        selected.append((matches[0], output.joinpath(*destination.parts), digest))
    for _, dest, digest in selected:
        if dest.exists() and file_digest(dest) != digest:
            raise ValueError(f'existing output differs: {dest}')
    for source, dest, digest in selected:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(source, dest)
            dest.chmod(0o600)
            if file_digest(dest) != digest:
                raise ValueError(f'copied firmware differs from verified input: {dest}')
    return len(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--input', type=Path, action='append', required=True,
                        help='extracted firmware or local device-data directory; repeat as needed')
    parser.add_argument('--receipt', type=Path, action='append', default=[],
                        help='local receipt from a device-specific generator; repeat as needed')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        count = stage(args.manifest, args.input, args.receipt, args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(f'Staged {count} verified local firmware files in {args.output}')


if __name__ == '__main__':
    main()
