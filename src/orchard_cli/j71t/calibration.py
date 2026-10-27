#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Copy J71t calibration and identity records from the device's boot ADT.

iBoot resolves these syscfg-backed properties before m1n1 starts, so the fresh
recovery ADT carries the unit's own bytes. They are copied unmodified into
private firmware files and are never logged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from orchard_cli.apple.adt_tree import Node, parse

GENERATOR = 'j71t-adt'
# Linux firmware name, ADT node, property, exact size (None: bounded, versioned
# record validated by its kernel consumer).
RECORDS: Tuple[Tuple[str, str, str, Optional[int]], ...] = (
    ('j71t-n1-calibration.bin', '/device-tree/arm-io/spi3/multi-touch', 'multi-touch-calibration', 1024),
    ('j71t-LSCI.bin', '/device-tree/arm-io/i2c2/als1', 'alsCalibration', None),
    ('j71t-CBAT.bin', '/device-tree/charger', 'usb-input-limit-calibration', 24),
    ('j71t-BMac.bin', '/device-tree/arm-io/uart3/bluetooth', 'local-mac-address', 6),
)
UNRESOLVED = (b'syscfg/', b'macaddr/', b'zeroes/')


def identify(tree: Node, model: str, board_config: str) -> None:
    if tree.properties.get('model', b'').split(b'\0', 1)[0] != model.encode():
        raise ValueError('ADT does not describe ' + model)
    compatible = tree.properties.get('compatible', b'').split(b'\0')
    if board_config.encode() not in compatible:
        raise ValueError('ADT is not for board ' + board_config)


def checked(name: str, value: Optional[bytes], size: Optional[int]) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ValueError('Missing ADT record: ' + name)
    if value.startswith(UNRESOLVED):
        raise ValueError('Unresolved ADT record: ' + name)
    if (size is not None and len(value) != size) or (size is None and not 8 <= len(value) <= 1024):
        raise ValueError('Unexpected ADT record size: ' + name)
    if value == b'\0' * len(value) or value == b'\xff' * len(value):
        raise ValueError('Blank ADT record: ' + name)
    return value


def records(blob: bytes, model: str, board_config: str) -> Dict[str, bytes]:
    tree = parse(blob)
    identify(tree, model, board_config)
    paths = {path: [node for walked, node in tree.walk() if walked == path]
             for _, path, _, _ in RECORDS}
    result = {}
    for name, path, prop, size in RECORDS:
        if len(paths[path]) != 1:
            raise ValueError('No unique ADT node for ' + name)
        result[name] = checked(name, paths[path][0].properties.get(prop), size)
    address = result['j71t-BMac.bin']
    if address[0] & 1:
        raise ValueError('Bluetooth address is not unicast')
    chosen = [node for walked, node in tree.walk() if walked == '/device-tree/chosen']
    published = chosen[0].properties.get('mac-address-bluetooth0') if len(chosen) == 1 else None
    if published is not None and published != address:
        raise ValueError('Bluetooth address records disagree')
    return result


def prepare(adt_path: Path, outdir: Path, device: str, model: str, board_config: str,
            expected: List[str]) -> List[Path]:
    """Write each record and a staging receipt; return the receipt paths."""
    if sorted(expected) != sorted(name for name, _, _, _ in RECORDS):
        raise ValueError('Firmware recipe and ADT generator records differ')
    if outdir.is_symlink() or (outdir.exists() and any(outdir.iterdir())):
        raise ValueError('Calibration output must be absent or an empty directory')
    blob = adt_path.read_bytes()
    values = records(blob, model, board_config)
    outdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipts = []
    for name, data in sorted(values.items()):
        fd = os.open(outdir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        receipt = outdir / (name + '.json')
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'device': device, 'name': name, 'generator': GENERATOR, 'bytes': len(data),
                       'sha256': hashlib.sha256(data).hexdigest(),
                       'adt_sha256': hashlib.sha256(blob).hexdigest()}, stream, indent=2)
            stream.write('\n')
        receipts.append(receipt)
    return receipts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    receipts = prepare(args.adt, args.output, 'iPad6,11', 'iPad6,11', 'J71tAP',
                       [name for name, _, _, _ in RECORDS])
    # Sizes only: receipts stay private beside the generated files.
    print(json.dumps({'output': str(args.output), 'files': len(receipts)}, indent=2))


if __name__ == '__main__':
    main()
