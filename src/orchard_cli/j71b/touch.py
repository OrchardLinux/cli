#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Convert J71b touch firmware to Z2FW with calibration records.

The file is the same for every J71b: apple_z2 fills each calibration record
from the DT properties m1n1 copies from the ADT at boot.

Packet layouts/Z2FW framing informed by Corellium Project Sandcastle and
Pauli1Go's GPL makez2fw (944541e67b4c603df328c77d30093551de802c17).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct

from orchard_cli.apple.osserialize import apple_xml

MAGIC = 0x5746325a
INIT, SEND, WAIT, CONFIG, CALIBRATION = 0, 1, 3, 4, 6
# apple_z2 calibration provider IDs (drivers/input/touchscreen/apple_z2.c).
PROVIDERS = {'multi-touch-calibration': 0, 'multitouch-to-display-offset': 4,
             'firefly-calibration': 5, 'pressure-effect-correction': 6}


def uint(value, name, maximum=0xffffffff):
    if isinstance(value, bool):
        raise ValueError('Boolean instead of integer: ' + name)
    if isinstance(value, str):
        if not value.isdecimal():
            raise ValueError('Nondecimal integer: ' + name)
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError('Integer out of bounds: ' + name)
    return value


def swapped(data):
    if len(data) % 2:
        raise ValueError('Word-swap requires even length')
    result = bytearray(data)
    result[0::2], result[1::2] = data[1::2], data[0::2]
    return bytes(result)


def xe32(value):
    return struct.pack('>HH', value & 0xffff, value >> 16)


def from_xe32(data):
    lo, hi = struct.unpack('>HH', data)
    return lo | hi << 16


def decode_blob(packet):
    if len(packet) < 20 or packet[:4] != b'\x18\xe1\x30\x01':
        raise ValueError('Not a prefixed HBPP14 blob')
    words_minus_one = struct.unpack_from('>H', packet, 4)[0]
    size = (words_minus_one + 1) * 4
    if len(packet) != 16 + size:
        raise ValueError('HBPP14 length mismatch or extra trailing packets')
    if sum(packet[4:10]) != struct.unpack_from('>H', packet, 10)[0]:
        raise ValueError('HBPP14 header checksum mismatch')
    payload = swapped(packet[12:-4])
    if sum(payload) & 0xffffffff != from_xe32(packet[-4:]):
        raise ValueError('HBPP14 payload checksum mismatch')
    address = from_xe32(packet[6:10])
    if address + size > 0x100000000:
        raise ValueError('HBPP14 address range overflow')
    return address, payload


def register_packet(address, mask, value):
    fields = xe32(uint(address, 'register address')) + xe32(uint(mask, 'register mask'))
    fields += xe32(uint(value, 'register value'))
    return b'\x1e\x33' + fields + struct.pack('>H', sum(fields))


def command(code, payload):
    return struct.pack('<II', code, len(payload)) + payload + b'\0' * (-len(payload) % 4)


def decode_container(blob):
    if len(blob) < 8 or struct.unpack_from('<II', blob) != (MAGIC, 1):
        raise ValueError('Invalid Z2FW header')
    pos = 8
    records = []
    while pos < len(blob):
        if pos+8 > len(blob):
            raise ValueError('Truncated Z2FW command')
        code, size = struct.unpack_from('<II', blob, pos)
        pos += 8
        next_pos = (pos+size+3)&~3
        if code not in (INIT, SEND, WAIT, CONFIG, CALIBRATION) or next_pos > len(blob):
            raise ValueError('Unknown or truncated Z2FW command')
        if any(blob[pos+size:next_pos]):
            raise ValueError('Nonzero Z2FW padding')
        records.append((code, blob[pos:pos+size]))
        pos = next_pos
    return records


def convert(profile, config):
    if profile.get('PreconstructedBootloadPacketType') != 'K1HBPP14' or config.get('Protocol') != 'Z2':
        raise ValueError('Unexpected packet type or protocol')
    payload = profile.get('Constructed Firmware')
    if not isinstance(payload, bytes):
        raise ValueError('Expected data-form constructed firmware')
    firmware_address, firmware_data = decode_blob(payload)
    if firmware_address != 0x100000:
        raise ValueError('Unexpected J71b firmware load address')
    calibration = config.get('Calibration Sequence')
    if not isinstance(calibration, list) or len(calibration) != 4:
        raise ValueError('Expected exactly four calibration entries')
    names = [item.get('Provider') for item in calibration]
    if set(names) != set(PROVIDERS) or len(set(names)) != len(names):
        raise ValueError('Missing, duplicate, or unsupported calibration provider')
    if not isinstance(config.get('Power Off On Reset'), bool):
        raise ValueError('Power Off On Reset must be a boolean')
    spi = config['SPI Config']
    timeout = config['Boot Timeout']
    # Preserve source scalar values. CS Delay=10000 and Clock Period Ms=41
    # are raw metadata, not independently established Linux units.
    words = [(1<<9)-1 | (1<<11), uint(config['Min DMA Transfer Size'], 'DMA minimum'),
             uint(config['Z2 Config']['Inter Packet Delay'], 'Z2 delay'),
             uint(spi['CS Delay'], 'raw CS delay'), uint(spi['Clock Phase'], 'CPHA', 1),
             uint(spi['Clock Polarity'], 'CPOL', 1), uint(spi['Word Delay'], 'word delay'),
             uint(spi['Clock Period Ms'], 'raw clock period'),
             int(config['Power Off On Reset'] is True),
             uint(timeout['Normal Boot Ms'], 'normal timeout', 30000), 0x104]
    first_boot = uint(timeout['First Boot Ms'], 'first timeout', 30000)
    if not first_boot:
        raise ValueError('First-boot timeout must be nonzero')
    records = [(CONFIG, struct.pack('<11I', *words)),
               (INIT, b'\x1a\xa1\x18\xe1'), (SEND, payload)]
    providers = []
    addresses = []
    for item in calibration:
        name = item['Provider']
        maximum = uint(item['Max Size'], 'calibration maximum', 0x40000)
        if not maximum:
            raise ValueError('Empty calibration range: ' + name)
        address = uint(item['Address'], 'calibration address')
        end = address + ((maximum+3)&~3)
        if end > 0x100000000 or any(address < b and a < end for a, b in addresses):
            raise ValueError('Overlapping or overflowing calibration ranges')
        addresses.append((address, end))
        records.append((CALIBRATION, struct.pack('<III', address, maximum, PROVIDERS[name])))
        providers.append({'provider': name, 'maximum': maximum, 'address': address})
    boot = config.get('Boot Sequence')
    if not isinstance(boot, list) or len(boot) != 4:
        raise ValueError('Unexpected boot sequence shape')
    for item in boot:
        if not isinstance(item, dict) or not {'Address', 'Value'} <= item.keys() or set(item)-{'Address','Value','Mask'}:
            raise ValueError('Unsupported boot operation')
        records.append((SEND, register_packet(item['Address'], item.get('Mask', 0xffffffff), item['Value'])))
    records.append((WAIT, struct.pack('<I', first_boot)))
    output = struct.pack('<II', MAGIC, 1) + b''.join(command(c, p) for c, p in records)
    if decode_container(output) != records:
        raise ValueError('Container roundtrip mismatch')
    return output, {'device': 'iPad7,5', 'profile': 'K1F19,4', 'hbpp_version': '0x104',
                    'firmware_packet_preserved_verbatim': True, 'firmware_packet_bytes': len(payload),
                    'firmware_payload_bytes': len(firmware_data), 'firmware_address': firmware_address,
                    'calibration': providers, 'register_operations': len(boot),
                    'command_sequence': [c for c, p in records]}


def prepare(firmware: Path, output_path: Path, report_path: Path):
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    if (output_path.name != 'j71b-fwstart.z2fw' or
            output_path == report_path or output_path.exists() or report_path.exists()):
        raise ValueError('Choose unused output and report paths for j71b-fwstart.z2fw')
    blob = firmware.read_bytes()
    profiles = plistlib.loads(blob)
    if set(profiles) != {'K1F19,4'}:
        raise ValueError('Unexpected profile set')
    profile = profiles['K1F19,4']
    output, report = convert(profile, apple_xml(profile['Firmware Config']))
    os.umask(0o077)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(output)
    report.update(name=output_path.name, sha256=hashlib.sha256(output).hexdigest(),
                  bytes=len(output), source_sha256=hashlib.sha256(blob).hexdigest())
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    receipt = prepare(args.firmware, args.output, args.report)
    print(json.dumps({'output': str(args.output), 'bytes': receipt['bytes'],
                      'sha256': receipt['sha256'], 'receipt': str(args.report)}, indent=2))


if __name__ == '__main__':
    main()
