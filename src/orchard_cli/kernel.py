"""CLI-owned kernel source preparation and cached-component composition.

Hardware is an unchanged input: use its existing full kernel, module, DTB and
staging recipes. Cache policy and module-SDK handling do not live in hardware.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from . import sources
from .kernel_sdk import export as export_sdk


def pin(hardware):
    return dict(line.split('=', 1) for line in (Path(hardware) / 'kernel/upstream.lock').read_text().splitlines()
                if '=' in line)


def work_directory(hardware, device):
    # Preserve existing editable source and compiler output; never move/reset it.
    # These are ignored scratch files, not changes to the hardware repository.
    return Path(hardware) / 'kernel/.work' / device


def prepare_source(hardware, cache, device):
    hardware = Path(hardware)
    selected = pin(hardware)
    work = work_directory(hardware, device)
    source = work / 'source'
    work.mkdir(parents=True, exist_ok=True)
    (work / 'output').mkdir(exist_ok=True)
    if not source.exists():
        sources.checkout(selected['url'], selected['commit'], source, cache=cache)
    if (sources.git(source, 'rev-parse', 'HEAD').decode() != selected['commit'] or
            sources.git(source, 'remote', 'get-url', 'origin').decode() != selected['url']):
        raise ValueError('Local kernel revision/remote differs from upstream.lock; sources were not reset')
    patches = sorted((hardware / 'kernel/patches' / device).glob('*.patch'))
    expected = b''.join(subprocess.check_output(['git', 'hash-object', str(p)]).strip() +
                        b' ' + p.name.encode() + b'\n' for p in patches)
    marker = work / 'applied-patches'
    if marker.exists():
        if marker.read_bytes() != expected:
            raise ValueError('Kernel patch set changed; use a fresh work directory; local edits were not reset')
    else:
        for patch in patches:
            try:
                sources.git(source, 'apply', '--reverse', '--check', patch)
            except subprocess.CalledProcessError:
                sources.git(source, 'apply', '--check', patch)
                sources.git(source, 'apply', patch)
        marker.write_bytes(expected)
    return source, work / 'output'


def build_base(hardware, cache, device, output):
    """One-time fill using hardware's existing full producer, not a new API.

    Retain only the kernel/in-tree modules and an SDK. The producer also builds
    external modules/final DTB on this first fill; those outputs are not in the
    reusable base. Subsequent device-layer edits bypass the full producer.
    """
    from .build import command
    hardware, output = Path(hardware), Path(output)
    source, compiler_output = prepare_source(hardware, cache, device)
    tree = sources.working_tree(source)
    with tempfile.TemporaryDirectory(prefix='base-', dir=work_directory(hardware, device)) as temporary:
        work = Path(temporary)
        command(hardware / 'kernel/build', device, '--resume', '--output', work / 'full')
        command(sys.executable, hardware / 'kernel/stage-modules.py',
                '--build', compiler_output, '--output', work / 'in-tree')
        output.mkdir(parents=True)
        for name in ('Image.gz', 'kernel.release'):
            shutil.copy2(work / 'full' / name, output / name)
        shutil.copytree(work / 'in-tree/lib', output / 'lib')
        export_sdk(compiler_output, output / 'sdk')
        (output / 'inputs.json').write_text(json.dumps({'source_tree': tree}) + '\n')
    if sources.working_tree(source) != tree:
        raise ValueError('Kernel source changed during build; refusing result')


def build_device(hardware, cache, device, base, output):
    """Build external modules and final DTB with a PRIVATE cached-SDK copy."""
    from .build import command
    hardware, base, output = map(Path, (hardware, base, output))
    source, _ = prepare_source(hardware, cache, device)
    tree = sources.working_tree(source)
    if json.loads((base / 'inputs.json').read_text())['source_tree'] != tree:
        raise ValueError('Cached kernel source identity differs; rebuild the base')
    if (base / 'sdk/.config').read_bytes() != (hardware / 'kernel/configs' / device / 'config').read_bytes():
        raise ValueError('Cached kernel configuration differs; rebuild the base')
    release = (base / 'kernel.release').read_text().strip()
    if not release or '/' in release or len(release.split()) != 1:
        raise ValueError('Invalid kernel release')
    if (base / 'sdk/include/config/kernel.release').read_text().strip() != release:
        raise ValueError('Cached module SDK release differs')
    with tempfile.TemporaryDirectory(prefix='modules-', dir=work_directory(hardware, device)) as temporary:
        work = Path(temporary)
        sdk = work / 'output'
        shutil.copytree(base / 'sdk', sdk)
        # The unchanged hardware staging recipe scans the compiler-output tree.
        # Restore in-tree modules at their ORIGINAL relative build paths there,
        # so it can merge external overrides without needing a new hardware API.
        for module in (base / 'lib/modules' / release / 'kernel').rglob('*.ko'):
            if module.is_symlink():
                raise ValueError('Symlink in cached in-tree modules')
            target = sdk / module.relative_to(base / 'lib/modules' / release / 'kernel')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(module, target)
        command(sys.executable, '-m', 'orchard_hardware.buildenv', 'build',
                '--file', hardware / 'kernel/Containerfile', '--context', hardware / 'kernel',
                '--image', 'localhost/orchard-hardware-kernel-toolchain:bookworm')
        external = []
        if (hardware / 'kernel/modules' / device).is_dir():
            command(hardware / 'kernel/build-modules', '--source', source, '--build', sdk,
                    '--modules', hardware / 'kernel/modules' / device,
                    '--localversion', '-orchard-' + device, '--output', work / 'external')
            external = ['--external', work / 'external']
        command(sys.executable, hardware / 'kernel/stage-modules.py', '--build', sdk,
                *external, '--output', work / 'staged')
        command(hardware / 'kernel/devicetree' / device / 'build', '--kernel-build', sdk,
                '--headers', source / 'include', '--output', work / (device + '.dtb'))
        output.mkdir(parents=True)
        for name in ('Image.gz', 'kernel.release'):
            shutil.copy2(base / name, output / name)
        shutil.copy2(work / (device + '.dtb'), output / (device + '.dtb'))
        shutil.copytree(work / 'staged/lib', output / 'lib')
    if sources.working_tree(source) != tree:
        raise ValueError('Kernel source changed during module build; refusing result')
