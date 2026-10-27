#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Select J71t's N1 touch boot packet from the public multitouch profile.

The Linux N1 driver uploads iOS's preconstructed C1F15,1 packet unmodified
(no Z2FW wrapper) and the unit's MtCl calibration separately, so this only
validates the packet framing and returns it verbatim.
"""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib

PERSONALITY = 'C1F15,1'
PREFIX = b'\x18\xe1\x30\x01'


def halfword32(data: bytes) -> int:
    return int.from_bytes(bytes((data[1], data[0], data[3], data[2])), 'little')


def validate(packet: bytes) -> None:
    if len(packet) < 20 or packet[:4] != PREFIX:
        raise ValueError('Not a prefixed N1 boot packet')
    if int.from_bytes(packet[4:6], 'big') * 4 + 16 != len(packet):
        raise ValueError('N1 packet length mismatch')
    if halfword32(packet[6:10]):
        raise ValueError('Unexpected N1 packet load address')
    if sum(packet[4:10]) != int.from_bytes(packet[10:12], 'big'):
        raise ValueError('N1 packet header checksum mismatch')
    if sum(packet[12:-4]) & 0xffffffff != halfword32(packet[-4:]):
        raise ValueError('N1 packet payload checksum mismatch')


def firmware(profile: bytes) -> bytes:
    profiles = plistlib.loads(profile)
    if not isinstance(profiles, dict) or set(profiles) != {PERSONALITY}:
        raise ValueError('Unexpected J71t multitouch profile set')
    packet = profiles[PERSONALITY].get('Constructed Firmware')
    if not isinstance(packet, bytes):
        raise ValueError('Expected data-form constructed firmware')
    validate(packet)
    return packet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    packet = firmware(args.profile.read_bytes())
    with args.output.open('xb') as stream:
        stream.write(packet)
    print(json.dumps({'bytes': len(packet), 'sha256': hashlib.sha256(packet).hexdigest()}, indent=2))


if __name__ == '__main__':
    main()
