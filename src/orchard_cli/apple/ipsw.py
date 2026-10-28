"""Apple IPSW tooling and extraction; board recipes and selections are callers' policy."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

from orchard_cli.cache import cache_root
from orchard_cli.integrity import digest

TOOL = {'url': 'https://github.com/blacktop/ipsw/releases/download/v3.1.739/ipsw_3.1.739_macOS_arm64.tar.gz',
        'sha256': 'c09e8eba02c332f5b50d15f94383e59dd35646d70f6ea9e78d185e9afda6abc2',
        'bytes': 28498196}


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
    # Keep the pure firmware selector usable on-device without download tooling.
    from orchard_cli.releases import materialize
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
    archive = materialize(TOOL, None, progress, clock, label='Firmware tool')
    with tarfile.open(archive) as bundle:
        matches = [m for m in bundle if Path(m.name).name == 'ipsw' and m.isfile()]
        if len(matches) != 1:
            raise ValueError('Unexpected firmware tool archive')
        executable.write_bytes(bundle.extractfile(matches[0]).read())
    executable.chmod(0o755)
    receipt.write_text(json.dumps({'sha256': digest(executable), 'archive_sha256': TOOL['sha256']}))
    return str(executable)


def run_tool(argv):
    directory = cache_root() / 'private/diagnostics'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(prefix='firmware-', suffix='.log', dir=directory, delete=False) as log:
        try:
            subprocess.run(argv, check=True, stdout=log, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as error:
            raise RuntimeError('Firmware extraction failed. Diagnostic log: ' + log.name) from error


def extract_files(executable, archive, patterns, output):
    for pattern in patterns:
        run_tool([executable, 'extract', '--files', '--pattern', pattern,
                  '--output', str(output), str(archive)])


def extract_kernel(executable, archive, device, output):
    run_tool([executable, 'extract', '--kernel', '--device', device,
              '--output', str(output), str(archive)])
    candidates = [p for p in output.rglob('kernelcache*') if p.is_file()]
    if len(candidates) != 1:
        raise ValueError('Expected one extracted device kernelcache')
    kernel = candidates[0].read_bytes()
    if kernel[:4] != b'\xcf\xfa\xed\xfe':
        decoded = output / 'kernel.macho'
        run_tool([executable, 'kernel', 'dec', str(candidates[0]), '--output', str(decoded)])
        kernel = decoded.read_bytes()
    return kernel
