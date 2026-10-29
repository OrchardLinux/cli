"""BlueTool's embedded Bluetooth firmware; board recipes and selections are callers' policy."""
import hashlib


def hcd_from_binary(data, expected):
    """Select only the SHA-pinned HCD command stream embedded in BlueTool."""
    offset = 0
    while True:
        signature = data.find(b'BRCMcfgS', offset)
        if signature < 0:
            raise ValueError('Pinned Bluetooth firmware not found in BlueTool')
        start = signature - 7
        offset = signature + 8
        if start < 0 or data[start:start + 2] != b'\x4c\xfc':
            continue
        end = start
        while end + 3 <= len(data):
            opcode = int.from_bytes(data[end:end + 2], 'little')
            length = data[end + 2]
            if opcode not in (0xfc4c, 0xfc4e) or end + 3 + length > len(data):
                break
            if end != start and data[end + 7:end + 15] == b'BRCMcfgS':
                break
            end += 3 + length
            if opcode == 0xfc4e:
                break
        candidate = data[start:end]
        if hashlib.sha256(candidate).hexdigest() == expected:
            return candidate
