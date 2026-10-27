"""Shared component build orchestration and caching.

Invokes hardware-owned compiler recipes. No RAM-root composition, installer
policy or device access. The hardware/CLI ownership boundary remains provisional.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from . import sources, kernel as kernels


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def command(*argv, env=None):
    print('+', ' '.join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), check=True, env=env)


def recipe_key(hardware, paths, *, ignore=('.work', '.git', '__pycache__')):
    """Hash recipes, not build output or experiment files."""
    result = hashlib.sha256()
    for relative in paths:
        root = hardware / relative
        files = [root] if root.is_file() else []
        if root.is_dir():
            for directory, dirs, names in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d not in ignore)
                files.extend(Path(directory) / name for name in sorted(names))
        for path in files:
            result.update(str(path.relative_to(hardware)).encode() + b'\0')
            result.update(str(path.stat().st_mode & 0o777).encode() + b'\0')
            result.update(digest(path).encode())
    return result.hexdigest()


def cache_root():
    return sources.cache_root()


def kernel_base_key(hardware, device, cache):
    """Same key before and after source checkout; external modules are separate."""
    pin = dict(line.split('=', 1) for line in (hardware / 'kernel/upstream.lock').read_text().splitlines()
               if '=' in line)
    source = hardware / 'kernel/.work' / device / 'source'
    patches = sorted((hardware / 'kernel/patches' / device).glob('*.patch'))
    declared = sources.declared_tree(pin['url'], pin['commit'], patches, cache=cache, donor=source)
    tree = sources.working_tree(source) if (source / '.git').exists() else declared
    if (source / '.git').exists() and sources.git(source, 'rev-parse', 'HEAD').decode() != pin['commit']:
        raise ValueError('Local kernel HEAD differs from upstream.lock; will not reset it')
    recipes = recipe_key(hardware, [
        'kernel/upstream.lock', 'kernel/configs/' + device, 'kernel/patches/' + device,
        'kernel/build', 'kernel/Containerfile', 'kernel/install-toolchain',
        'kernel/stage-modules.py', 'buildenv.py',
    ])
    # CLI owns source preparation/SDK composition. Include those implementations
    # in the base identity rather than requiring new hardware package contents.
    recipes += digest(Path(kernels.__file__)) + digest(Path(__file__).with_name('kernel_sdk.py'))
    mode = os.environ.get('ORCHARD_BUILD_NATIVE', '0') + ':' + os.environ.get('ORCHARD_BUILD_ENV_ID', '')
    return hashlib.sha256((recipes + tree + mode).encode()).hexdigest()


def kernel_key(hardware, device, base_key):
    """Device layer: external drivers and final DTB, reusing the compiled base."""
    recipes = recipe_key(hardware, ['kernel/modules/' + device, 'kernel/devicetree/' + device,
                                    'kernel/build-modules', 'kernel/stage-modules.py'])
    return hashlib.sha256((base_key + recipes).encode()).hexdigest()


@contextmanager
def component_lock(cache):
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'Another build is using {cache}; wait for it to finish') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cached_component(cache, name, key, produce):
    """Immutable component directories, checked before reuse and publication.
    Call under component_lock when building into the same cache.
    """
    parent = cache / name
    parent.mkdir(parents=True, exist_ok=True)
    initial_key = key()
    target = parent / initial_key
    if target.exists():
        records = json.loads((target / 'component.json').read_text())
        actual = {str(p.relative_to(target)) for p in target.rglob('*')
                  if p.is_file() and p != target / 'component.json'}
        if actual != set(records):
            raise ValueError(f'Cached component inventory changed: {target}; remove it and rebuild')
        for relative, expected in records.items():
            path = target / relative
            if path.is_symlink() or digest(path) != expected:
                raise ValueError(f'Cached component changed: {path}; remove that component and rebuild')
        print(f'Reusing {name}: {target}', flush=True)
        return target
    print(f'Building {name} (cache miss): {target}', flush=True)
    with tempfile.TemporaryDirectory(prefix='build-', dir=parent) as temporary:
        stage = Path(temporary) / 'component'
        stage.mkdir()
        produce(stage)
        records = {str(p.relative_to(stage)): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage / 'component.json').write_text(json.dumps(records, indent=2) + '\n')
        if key() != initial_key:
            raise ValueError(f'{name} inputs changed during build; result was not cached')
        if target.exists():
            raise ValueError(f'Component appeared during build: {target}; inspect before rebuilding')
        stage.rename(target)
    return target


def _build_context(hardware, cache, device):
    if hardware is None:
        import orchard_hardware
        hardware = Path(orchard_hardware.__file__).resolve().parent
    hardware = Path(hardware).resolve()
    cache = Path(cache).resolve() if cache is not None else cache_root()
    if not (hardware / 'kernel/build').is_file():
        raise ValueError(f'Not a hardware checkout: {hardware}')
    if not device or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in device):
        raise ValueError('Invalid device profile')
    return hardware, cache


def _prepare_kernel(hardware, cache, device):
    """Resolve one coherent kernel/DTB/module bundle under the component lock."""
    components = cache / 'components-v2'
    base_key = lambda: kernel_base_key(hardware, device, cache)
    def produce_base(stage):
        kernels.build_base(hardware, cache, device, stage / 'kernel')
    base = cached_component(components, 'kernel-base-' + device, base_key, produce_base) / 'kernel'
    def produce_kernel(stage):
        kernels.build_device(hardware, cache, device, base, stage / 'kernel')
    directory = cached_component(components, 'kernel-' + device,
        lambda: kernel_key(hardware, device, base.parent.name), produce_kernel)
    return directory / 'kernel'


def prepare_kernel(hardware=None, cache=None, *, device='j71b'):
    """Build/reuse a coherent kernel, DTB and modules, without boot/tool producers.

    Defaults to the active hardware package and normal shared Orchard cache.
    Assembly policy and explicit-component overrides belong to the caller.
    """
    hardware, cache = _build_context(hardware, cache, device)
    with component_lock(cache):
        return _prepare_kernel(hardware, cache, device)


def prepare_components(hardware=None, cache=None, *, device='j71b', kernel=None,
                       busybox=None, loader=None, pongo=None):
    """Build/reuse components shared by experiments and larger images.

    Explicit component paths bypass their producers. The returned paths are not
    installer-specific. Callers supplying a different hardware tree must also
    arrange for its Python build dependencies to be importable.
    """
    hardware, cache = _build_context(hardware, cache, device)
    components = cache / 'components-v2'
    with component_lock(cache):
        if pongo is None:
            def produce_pongo(stage):
                if sys.platform != 'darwin':
                    raise ValueError('Building Pongo needs macOS/Xcode; use --pongo FILE on Linux')
                work = hardware / '.work/pongo' / recipe_key(hardware, ['boot/quiet'])
                argv = [sys.executable, hardware / 'boot/quiet/build-pongo.py',
                        '--work-dir', work, '--output', stage / 'pongo.bin']
                if (work / 'source').exists():
                    argv.append('--resume')
                command(*argv)
            directory = cached_component(components, 'pongo',
                lambda: recipe_key(hardware, ['boot/quiet']), produce_pongo)
            pongo = directory / 'pongo.bin'
        if kernel is None:
            kernel = _prepare_kernel(hardware, cache, device)
        if busybox is None:
            def produce_tools(stage):
                work = hardware / '.work/tools'
                work = work / recipe_key(hardware, ['boot/initramfs/j71b/recovery', 'buildenv.py'])
                if not (work / 'bin/busybox').exists():
                    command(hardware / 'boot/initramfs/j71b/recovery/build-tools',
                            env={**os.environ, 'OMA_RECOVERY_TOOLS_WORK': str(work)})
                shutil.copyfile(work / 'bin/busybox', stage / 'busybox')
            directory = cached_component(components, 'busybox',
                lambda: recipe_key(hardware, ['boot/initramfs/j71b/recovery', 'buildenv.py']), produce_tools)
            busybox = directory / 'busybox'
        if loader is None:
            def produce_loader(stage):
                argv = [hardware / 'boot/m1n1/build', device, '--output', stage / 'm1n1.bin']
                if (hardware / 'boot/m1n1/.work' / device).exists():
                    argv.append('--resume')
                command(*argv)
            directory = cached_component(components, 'm1n1-' + device,
                lambda: recipe_key(hardware, ['boot/m1n1', 'buildenv.py']), produce_loader)
            loader = directory / 'm1n1.bin'
    return {name: Path(path).resolve() for name, path in
            dict(kernel=kernel, busybox=busybox, loader=loader, pongo=pongo).items()}


