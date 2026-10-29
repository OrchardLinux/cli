#!/usr/bin/env python3
"""Run install_device.py against a loop-device disk in a Linux VM, as root.

Not part of the unit tests (needs root and loop devices). From macOS:
    podman machine ssh "sudo python3 $PWD/tests/linux/install_vm.py $PWD STATIC_ARM64_BUSYBOX"
Covers a free range, replacing a root, a stale plan and the old one-partition layout.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import uuid

CLI = Path(sys.argv[1])
BUSYBOX = Path(sys.argv[2])
sys.path.insert(0, str(CLI / 'src'))
from orchard_cli import install_layout as L          # noqa: E402
from orchard_cli.bootdisk import Disk, read_boot     # noqa: E402

DEVICE = CLI / 'src/orchard_cli/install_device.py'
APFS = '7c3457ef-0000-11aa-aa11-00306543ecac'
VERSION = '7.3.0-rc1-orchard-apple-16k'
GIB = 1 << 30


def sh(*argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, **kwargs).stdout


def events(output):
    return [json.loads(line) for line in output.decode().splitlines() if line.startswith('{')]


def device(disk, *args, stdin=b''):
    result = subprocess.run([sys.executable, str(DEVICE), *args], input=stdin, capture_output=True,
                            env={**os.environ, 'ORCHARD_INSTALL_DISK': disk})
    found = events(result.stdout)
    if result.returncode:
        raise SystemExit(f'device {args[0]} failed: {found[-1] if found else result.stderr.decode()}')
    return found


def root_image():
    kernel_install = f'''#!/bin/sh
export PATH=/bin
ver=$2; kernel=$3; token=$(cat /etc/kernel/entry-token); dir=/boot/$token/$ver
mkdir -p $dir /boot/loader/entries
cp $kernel $dir/linux; echo initrd > $dir/initrd; cp /usr/lib/modules/$ver/dtb/t8010-j71b.dtb $dir/
printf 'title Test\\nversion %s\\nsort-key test\\nlinux /%s/%s/linux\\ninitrd /%s/%s/initrd\\ndevicetree /%s/%s/t8010-j71b.dtb\\noptions %s\\n' \\
  $ver $token $ver $token $ver $token $ver "$(cat /etc/kernel/cmdline)" > /boot/loader/entries/$token-$ver.conf
'''
    files = {'bin/busybox': (BUSYBOX.read_bytes(), 0o755),
             'usr/bin/kernel-install': (kernel_install.encode(), 0o755),
             f'usr/lib/modules/{VERSION}/vmlinuz': (b'ARM64 kernel image' * 100, 0o644),
             f'usr/lib/modules/{VERSION}/dtb/t8010-j71b.dtb': (b'\xd0\x0d\xfe\xed' + bytes(60), 0o644),
             'etc/os-release': (b'ID=test\n', 0o644)}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        for applet in ('sh', 'mkdir', 'cp', 'cat', 'printf', 'echo'):
            info = tarfile.TarInfo('bin/' + applet)
            info.type, info.linkname = tarfile.SYMTYPE, 'busybox'
            tar.addfile(info)
    data = buffer.getvalue()
    return data, {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                  'expanded_bytes': 4 << 20, 'compression': 'gzip'}


def apfs_digest(disk):
    return hashlib.sha256(sh('dd', f'if={disk}p1', 'bs=1M', 'count=8', 'status=none')).hexdigest()


def install(disk, choose, image, data, label):
    (layout,) = [e for e in device(disk, 'inspect') if e['event'] == 'layout']
    option = choose(L.options(layout))
    plan = L.plan(layout, option, image, {'id': 'test', 'version': VERSION, 'options': 'quiet'})
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(plan, f)
    found = device(disk, 'run', f.name, stdin=data)
    print(label, [e.get('step') or e['event'] for e in found if e['event'] in ('step', 'finished')])
    return plan


def main():
    with tempfile.TemporaryDirectory(dir='/var/tmp') as work:
        backing = Path(work) / 'disk.img'
        with backing.open('wb') as f:
            f.truncate(16 * GIB)
        disk = sh('losetup', '--sector-size', '4096', '--show', '-fP', str(backing)).decode().strip()
        sh('blockdev', '--setrw', disk)   # a previous run's read-only fence stays on the loop node
        try:
            sh('sfdisk', disk, input=f'label: gpt\nstart=256, size={6 * GIB // 4096}, type={APFS}\n'.encode())
            sh('blockdev', '--rereadpt', disk)
            sh('dd', 'if=/dev/urandom', f'of={disk}p1', 'bs=1M', 'count=8', 'status=none')
            before = apfs_digest(disk)
            data, image = root_image()
            L.MIN_ROOT_BYTES = 2 * GIB                     # small test disk; rules unchanged

            first = install(disk, lambda options: [o for o in options if o.kind == 'free'][0],
                            image, data, 'case a (free space):')
            second = install(disk, lambda options: [o for o in options if o.kind == 'replace'][0],
                             image, data, 'case c (replace root):')
            assert apfs_digest(disk) == before, 'APFS bytes changed'
            with open(disk, 'rb') as dev:
                def read(lba):
                    dev.seek(lba * 4096)
                    return dev.read(4096)
                _, _, entries = read_boot(Disk(read, 4096), 't8010-j71b.dtb')
            assert [e.problems for e in entries] == [[]], [e.problems for e in entries]
            assert entries[0].root == second['root']['partuuid'] != first['root']['partuuid']
            print('APFS untouched; boot entries:', [(e.id, e.root[:8]) for e in entries], '- contract valid')

            # A plan from an older inspect must be refused before any write.
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
                json.dump(first, f)
            result = subprocess.run([sys.executable, str(DEVICE), 'run', f.name], input=data, capture_output=True,
                                    env={**os.environ, 'ORCHARD_INSTALL_DISK': disk})
            refused = events(result.stdout)[-1]
            assert result.returncode and refused['code'] == 'layout-changed', refused
            print('stale plan refused:', refused['code'])

            # Case b: the old layout, one ext4 Linux-data partition over all free space.
            start = 256 + 6 * GIB // 4096
            sh('blockdev', '--setrw', disk)
            sh('sfdisk', disk, input=(f'label: gpt\nstart=256, size={6 * GIB // 4096}, type={APFS}\n'
                                      f'start={start}, type=0fc63daf-8483-4772-8e79-3d69d8477de4\n').encode())
            sh('blockdev', '--rereadpt', disk)
            sh('dd', 'if=/dev/urandom', f'of={disk}p1', 'bs=1M', 'count=8', 'status=none')
            sh('mke2fs', '-q', '-t', 'ext4', '-L', 'OrchardLinux', f'{disk}p2')
            before = apfs_digest(disk)
            install(disk, lambda options: [o for o in options if o.kind == 'replace'][0],
                    image, data, 'case b (replace old layout):')
            assert apfs_digest(disk) == before, 'APFS bytes changed'
            with open(disk, 'rb') as dev:
                def read(lba):
                    dev.seek(lba * 4096)
                    return dev.read(4096)
                parts, _, entries = read_boot(Disk(read, 4096), 't8010-j71b.dtb')
            types = sorted(str(p.type_guid)[:8] for p in parts)
            assert [e.problems for e in entries] == [[]] and types == ['7c3457ef', 'b921b045', 'bc13c2ff'], types
            print('case b: APFS untouched, old partition replaced by boot + root, entry valid')
        finally:
            subprocess.run(['losetup', '-d', disk])


if __name__ == '__main__':
    main()
