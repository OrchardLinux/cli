"""The preconstructed Z2 boot packet of a multitouch profile, unmodified.

Some profiles (iPad 5: C1F15,1) carry one complete, prefixed boot packet as
"Constructed Firmware" with PreconstructedBootloadPacketType "Z2" instead of
HBPP load items. The driver uploads it as is; this only checks its framing.
"""
import plistlib

PREFIX = b'\x18\xe1\x30\x01'


def halfword32(data: bytes) -> int:
    return int.from_bytes(bytes((data[1], data[0], data[3], data[2])), 'little')


def validate(packet: bytes) -> None:
    if len(packet) < 20 or packet[:4] != PREFIX:
        raise ValueError('Not a prefixed Z2 boot packet')
    if int.from_bytes(packet[4:6], 'big') * 4 + 16 != len(packet):
        raise ValueError('Z2 boot packet length mismatch')
    if halfword32(packet[6:10]):
        raise ValueError('Unexpected Z2 boot packet load address')
    if sum(packet[4:10]) != int.from_bytes(packet[10:12], 'big'):
        raise ValueError('Z2 boot packet header checksum mismatch')
    if sum(packet[12:-4]) & 0xffffffff != halfword32(packet[-4:]):
        raise ValueError('Z2 boot packet payload checksum mismatch')


def extract(mtprops: bytes, profile: str) -> bytes:
    entry = plistlib.loads(mtprops).get(profile)
    if not isinstance(entry, dict) or entry.get('PreconstructedBootloadPacketType') != 'Z2':
        raise ValueError('No preconstructed Z2 packet in profile ' + profile)
    packet = entry.get('Constructed Firmware')
    if not isinstance(packet, bytes):
        raise ValueError('Profile has no constructed firmware: ' + profile)
    validate(packet)
    return packet
