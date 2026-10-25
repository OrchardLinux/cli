"""Apple IPSW tooling and extraction; board recipes and selections are callers' policy."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile

from orchard_cli.cache import cache_root
from orchard_cli.integrity import digest
from orchard_cli.http import open_url

TOOL = {'url': 'https://github.com/blacktop/ipsw/releases/download/v3.1.739/ipsw_3.1.739_macOS_arm64.tar.gz',
        'sha256': 'c09e8eba02c332f5b50d15f94383e59dd35646d70f6ea9e78d185e9afda6abc2'}


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


def tool(clock, progress):
    supplied = os.environ.get('ORCHARD_IPSW_TOOL') or shutil.which('ipsw')
    if supplied:
        return supplied
    directory = cache_root() / 'tools/ipsw'
    executable = directory / 'ipsw'
    receipt = directory / 'verified.json'
    if executable.is_file() and receipt.is_file() and digest(executable) == json.loads(receipt.read_text())['sha256']:
        return str(executable)
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('Firmware extraction requires the ipsw CLI in PATH (or ORCHARD_IPSW_TOOL) on this host')
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'tool.tar.gz.part'
    progress.phase('Downloading pinned firmware extraction tool')
    with clock.exclude('download'), open_url(TOOL['url'], timeout=60) as response, archive.open('wb') as output:
        shutil.copyfileobj(response, output)
    if digest(archive) != TOOL['sha256']:
        raise ValueError('Firmware extraction tool digest differs')
    with tarfile.open(archive) as bundle:
        matches = [m for m in bundle if Path(m.name).name == 'ipsw' and m.isfile()]
        if len(matches) != 1:
            raise ValueError('Unexpected firmware tool archive')
        executable.write_bytes(bundle.extractfile(matches[0]).read())
    executable.chmod(0o755)
    receipt.write_text(json.dumps({'sha256': digest(executable), 'archive_sha256': TOOL['sha256']}))
    archive.unlink()
    return str(executable)


def extract_files(executable, archive, patterns, output):
    for pattern in patterns:
        subprocess.run([executable, 'extract', '--files', '--pattern', pattern,
                        '--output', str(output), str(archive)], check=True)


def extract_kernel(executable, archive, device, output):
    subprocess.run([executable, 'extract', '--kernel', '--device', device,
                    '--output', str(output), str(archive)], check=True)
    candidates = [p for p in output.rglob('kernelcache*') if p.is_file()]
    if len(candidates) != 1:
        raise ValueError('Expected one extracted device kernelcache')
    kernel = candidates[0].read_bytes()
    if kernel[:4] != b'\xcf\xfa\xed\xfe':
        decoded = output / 'kernel.macho'
        subprocess.run([executable, 'kernel', 'dec', str(candidates[0]), '--output', str(decoded)], check=True)
        kernel = decoded.read_bytes()
    return kernel
