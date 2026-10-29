"""Cross-checkout reuse with real local Git and mocked compiler recipes."""
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from orchard_cli import build, sources, kernel

import hardware_layout


class SharedBuildCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / 'shared-cache'
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        import subprocess
        subprocess.run(['git', 'init', '-q', str(self.upstream)], check=True)
        (self.upstream / 'driver.c').write_text('upstream driver\n')
        sources.git(self.upstream, 'add', '.')
        sources.git(self.upstream, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        self.commit = sources.git(self.upstream, 'rev-parse', 'HEAD').decode()
        self.one, self.two = self.root / 'one/hardware', self.root / 'two/hardware'
        for hardware in (self.one, self.two):
            for name, text in {
                'kernel/build': 'producer', 'kernel/build-modules': 'external producer',
                'kernel/stage-modules.py': 'stage',
                'kernel/modules/j71b/power/driver.c': 'external driver',
                'kernel/upstream.lock': f'url={self.upstream.as_uri()}\ncommit={self.commit}\n',
            }.items():
                path = hardware / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            hardware_layout.write(hardware)
        self.overrides = {}
        for name in ('busybox', 'pongo', 'loader'):
            path = self.root / name
            path.write_text('explicit local component')
            self.overrides[name] = path
        self.calls = []

    def produce(self, hardware, cache, device, output, base=None):
        self.assertEqual(cache, self.cache)
        self.assertEqual(device, 'j71b')
        source = hardware / 'kernel/.work/apple/source'
        if not source.exists():
            sources.checkout(self.upstream.as_uri(), self.commit, source, cache=self.cache)
        output.mkdir(parents=True)
        if base is None:
            self.calls.append('kernel-base')
            (output / 'Image.gz').write_text(sources.working_tree(source))
            (output / 'kernel.release').write_text('test-release\n')
            module_tree = output / 'lib/modules/test-release'
            module_tree.mkdir(parents=True)
            (module_tree / 'modules.dep').write_text('upstream.ko:\n')
            (module_tree / 'upstream.ko').write_text('upstream module')
            (output / 'sdk').mkdir()
            (output / 'sdk/Module.symvers').write_text('matching symbols')
            (output / 'dtbs').mkdir()
            for dtb in ('t8010-j71b', 't8010-j72b'):
                (output / 'dtbs' / (dtb + '.dtb')).write_text(dtb)
        else:
            self.calls.append('external-modules-and-dtb')
            self.assertTrue((base / 'sdk/Module.symvers').is_file())
            for name in ('Image.gz', 'kernel.release'):
                shutil.copyfile(base / name, output / name)
            shutil.copytree(base / 'lib', output / 'lib')
            (output / 'lib/modules/test-release/external.ko').write_text(
                (hardware / 'kernel/modules/j71b/power/driver.c').read_text())
            dtb = kernel.layout(hardware, device)[1]
            shutil.copyfile(base / 'dtbs' / (dtb + '.dtb'), output / 'j71b.dtb')

    def prepare(self, hardware):
        with patch.object(kernel, 'build_base', side_effect=self.produce), \
                patch.object(kernel, 'build_device', side_effect=lambda hw, cache, device, base, out:
                             self.produce(hw, cache, device, out, base)):
            return build.prepare_components(hardware, self.cache, device='j71b', **self.overrides)

    def test_recovery_tools_cache_tracks_tool_inputs_not_runtime(self):
        for relative in build.TOOL_INPUTS:
            path = self.one / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('original tool input')
        runtime = self.one / 'boot/initramfs/recovery/init'
        runtime.write_text('original runtime')
        calls = []

        def produce_tools(*args, env):
            calls.append(args)
            tools = Path(env['OMA_RECOVERY_TOOLS_WORK']) / 'bin'
            tools.mkdir(parents=True)
            (tools / 'busybox').write_text('built tool')

        with patch.object(build, 'command', side_effect=produce_tools):
            first = build.prepare_tools(self.one, self.cache, device='j71b')
            runtime.write_text('changed runtime')
            self.assertEqual(build.prepare_tools(self.one, self.cache, device='j71b'), first)
            self.assertEqual(len(calls), 1)
            (self.one / build.TOOL_INPUTS[0]).write_text('changed tool input')
            second = build.prepare_tools(self.one, self.cache, device='j71b')
        self.assertNotEqual(first, second)
        self.assertEqual(len(calls), 2)
        self.assertEqual((second / 'busybox').read_text(), 'built tool')

    def test_fresh_second_checkout_neither_downloads_checks_out_nor_compiles_linux(self):
        first = self.prepare(self.one)
        self.assertEqual(self.calls, ['kernel-base', 'external-modules-and-dtb'])
        # No upstream access possible on the second build.
        self.upstream.rename(self.root / 'upstream-offline')
        second = self.prepare(self.two)
        self.assertEqual(first['kernel'], second['kernel'])
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.two / 'kernel/.work').exists())

    def test_kernel_only_caller_reuses_ram_build_without_checkout_or_other_producers(self):
        first = self.prepare(self.one)
        self.upstream.rename(self.root / 'upstream-offline')
        with patch.object(kernel, 'build_base', side_effect=AssertionError('base rebuilt')), \
                patch.object(kernel, 'build_device', side_effect=AssertionError('modules rebuilt')), \
                patch.object(sources, 'checkout', side_effect=AssertionError('source checked out')), \
                patch.object(build, 'command', side_effect=AssertionError('unrelated producer invoked')):
            second = build.prepare_kernel(self.two, self.cache, device='j71b')
        self.assertEqual(first['kernel'], second)
        self.assertFalse((self.two / 'kernel/.work').exists())

    def test_ram_build_reuses_kernel_only_build(self):
        with patch.object(kernel, 'build_base', side_effect=self.produce), \
                patch.object(kernel, 'build_device', side_effect=lambda hw, cache, device, base, out:
                             self.produce(hw, cache, device, out, base)):
            first = build.prepare_kernel(self.one, self.cache, device='j71b')
        second = self.prepare(self.two)
        self.assertEqual(first, second['kernel'])
        self.assertEqual(self.calls, ['kernel-base', 'external-modules-and-dtb'])
        self.assertFalse((self.two / 'kernel/.work').exists())

    def test_kernel_defaults_to_active_hardware_and_shared_cache_environment(self):
        import orchard_hardware
        first = self.prepare(self.one)
        with patch.object(orchard_hardware, '__file__', str(self.two / '__init__.py')), \
                patch.dict(os.environ, {'ORCHARD_CACHE_DIR': str(self.cache)}), \
                patch.object(kernel, 'build_base', side_effect=AssertionError('base rebuilt')), \
                patch.object(kernel, 'build_device', side_effect=AssertionError('modules rebuilt')):
            second = build.prepare_kernel(device='j71b')
        self.assertEqual(first['kernel'], second)
        self.assertFalse((self.two / 'kernel/.work').exists())

    def test_cache_misses_and_hits_are_visible_without_changing_identity(self):
        with redirect_stdout(io.StringIO()) as first_log:
            first = self.prepare(self.one)
        self.assertIn('Building kernel-base-apple (cache miss):', first_log.getvalue())
        self.assertIn('Building kernel-j71b (cache miss):', first_log.getvalue())
        with redirect_stdout(io.StringIO()) as second_log:
            second = build.prepare_kernel(self.two, self.cache, device='j71b')
        self.assertEqual(first['kernel'], second)
        self.assertIn('Reusing kernel-base-apple:', second_log.getvalue())
        self.assertIn('Reusing kernel-j71b:', second_log.getvalue())
        self.assertNotIn('Building ', second_log.getvalue())
        self.assertEqual(self.calls, ['kernel-base', 'external-modules-and-dtb'])

    def test_reused_cache_is_still_hash_verified(self):
        first = self.prepare(self.one)
        base = next((self.cache / 'components-v2/kernel-base-apple').iterdir()) / 'kernel'
        (base / 'Image.gz').write_bytes(b'corrupted')
        shutil.rmtree(first['kernel'].parent)
        with patch.object(kernel, 'build_base', side_effect=AssertionError('silently rebuilt')), \
                self.assertRaisesRegex(ValueError, 'Cached component changed'):
            build.prepare_kernel(self.two, self.cache, device='j71b')

    def test_missing_recipe_fails_before_any_checkout_or_producer(self):
        with patch.object(sources, 'checkout', side_effect=AssertionError('checked out source')), \
                patch.object(build, 'command', side_effect=AssertionError('started producer')), \
                self.assertRaisesRegex(ValueError, 'Missing hardware recipe input'):
            build.prepare_components(self.one, self.cache, device='new-board')
        self.assertFalse(self.cache.exists())

    def test_external_driver_change_reuses_kernel_base_and_sdk(self):
        first = self.prepare(self.one)
        (self.two / 'kernel/modules/j71b/power/driver.c').write_text('changed external driver')
        second = self.prepare(self.two)
        self.assertEqual(self.calls, ['kernel-base', 'external-modules-and-dtb', 'external-modules-and-dtb'])
        self.assertNotEqual(first['kernel'], second['kernel'])
        self.assertEqual(build.digest(first['kernel'] / 'Image.gz'), build.digest(second['kernel'] / 'Image.gz'))
        self.assertEqual((second['kernel'] / 'lib/modules/test-release/external.ko').read_text(),
                         'changed external driver')

    def test_device_firmware_changes_refresh_receipt_without_rebuilding_kernel_base(self):
        first = self.prepare(self.one)
        for relative in ('README.md', 'kernel/configs/other/config', 'kernel/modules/other/driver.c'):
            path = self.two / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('unrelated change')
        second = self.prepare(self.two)
        self.assertEqual(first['kernel'], second['kernel'])
        self.assertEqual(len(self.calls), 2)
        (self.two / 'devices/j71b/firmware.json').write_text('changed firmware recipe')
        third = self.prepare(self.two)
        self.assertNotEqual(first['kernel'], third['kernel'])
        self.assertEqual(self.calls.count('kernel-base'), 1)
        self.assertEqual(self.calls.count('external-modules-and-dtb'), 2)

    def test_config_and_local_in_tree_edits_rebuild_the_base_without_reset(self):
        self.prepare(self.one)
        (self.two / 'kernel/configs/apple/config').write_text('changed config')
        self.prepare(self.two)
        self.assertEqual(self.calls.count('kernel-base'), 2)
        source = self.one / 'kernel/.work/apple/source'
        (source / 'driver.c').write_text('local experimental kernel driver\n')
        self.prepare(self.one)
        self.assertEqual(self.calls.count('kernel-base'), 3)
        self.assertEqual((source / 'driver.c').read_text(), 'local experimental kernel driver\n')

    def test_inputs_changed_during_build_are_not_published(self):
        values = ['before']
        def produce(stage):
            (stage / 'artifact').write_text('built from old input')
            values[0] = 'after'
        with self.assertRaises(ValueError):
            build.cached_component(self.cache, 'race', lambda: values[0], produce)
        self.assertFalse((self.cache / 'race/before').exists())
        self.assertFalse((self.cache / 'race/after').exists())

    def test_dtb_selection_only_rebuilds_device_layer(self):
        self.prepare(self.one)
        hardware_layout.write(self.two, dtb='t8010-j72b')
        second = self.prepare(self.two)
        self.assertEqual((second['kernel'] / 'j71b.dtb').read_text(), 't8010-j72b')
        self.assertEqual(self.calls.count('kernel-base'), 1)
        self.assertEqual(self.calls.count('external-modules-and-dtb'), 2)

    def test_page_size_platforms_keep_separate_kernels_and_module_sdks(self):
        hardware_layout.write(self.one, device='j71b', platform='apple-16k', dtb='t8010-j71b')
        hardware_layout.write(self.one, device='n61', platform='apple-4k', dtb='t7000-n61')
        (self.one / 'kernel/configs/apple-16k/config').write_text('CONFIG_ARM64_16K_PAGES=y\n')
        (self.one / 'kernel/configs/apple-4k/config').write_text('CONFIG_ARM64_4K_PAGES=y\n')
        calls = []

        def produce_base(hardware, cache, device, output):
            platform = kernel.layout(hardware, device)[0]
            calls.append(('base', platform))
            output.mkdir(parents=True)
            (output / 'Image.gz').write_text(platform)
            (output / 'kernel.release').write_text('test-' + platform)
            (output / 'sdk').mkdir()
            (output / 'sdk/Module.symvers').write_text(platform)
            (output / 'dtbs').mkdir()
            dtb = kernel.layout(hardware, device)[1]
            (output / 'dtbs' / (dtb + '.dtb')).write_text(dtb + ':' + platform)

        def produce_device(hardware, cache, device, base, output):
            platform = kernel.layout(hardware, device)[0]
            self.assertEqual((base / 'sdk/Module.symvers').read_text(), platform)
            calls.append(('device', device))
            shutil.copytree(base, output)
            dtb = kernel.layout(hardware, device)[1]
            shutil.copyfile(base / 'dtbs' / (dtb + '.dtb'), output / (device + '.dtb'))

        with patch.object(kernel, 'build_base', side_effect=produce_base), \
                patch.object(kernel, 'build_device', side_effect=produce_device):
            large_pages = build.prepare_kernel(self.one, self.cache, device='j71b')
            small_pages = build.prepare_kernel(self.one, self.cache, device='n61')
            reused = build.prepare_kernel(self.one, self.cache, device='n61')

        self.assertNotEqual(large_pages, small_pages)
        self.assertEqual(reused, small_pages)
        self.assertEqual((large_pages / 'kernel.release').read_text(), 'test-apple-16k')
        self.assertEqual((small_pages / 'kernel.release').read_text(), 'test-apple-4k')
        self.assertEqual((large_pages / 'j71b.dtb').read_text(), 't8010-j71b:apple-16k')
        self.assertEqual((small_pages / 'n61.dtb').read_text(), 't7000-n61:apple-4k')
        self.assertEqual(calls, [('base', 'apple-16k'), ('device', 'j71b'),
                                 ('base', 'apple-4k'), ('device', 'n61')])

    def test_loader_recipe_and_device_select_isolated_resumable_workspaces(self):
        builder = self.one / 'boot/m1n1/build'
        builder.parent.mkdir(parents=True)
        builder.write_text('#!' + sys.executable + '\n' + '''
import json
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
device = args[0]
work = Path(args[args.index('--work-dir') + 1])
output = Path(args[args.index('--output') + 1])
resume = '--resume' in args
assert (work / 'source').exists() if resume else not work.exists()
(work / 'source').mkdir(parents=True, exist_ok=resume)
marker = work / 'source/preserve.txt'
if not marker.exists():
    marker.write_text(device + ':' + work.name)
    subprocess.run(['git', 'init', '-q', str(marker.parent)], check=True)
    subprocess.run(['git', '-C', str(marker.parent), 'add', 'preserve.txt'], check=True)
    subprocess.run(['git', '-C', str(marker.parent), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                    'commit', '-qm', 'loader fixture'], check=True)
if (work / 'change-during-build').exists():
    marker.write_text('changed during build')
log = Path(__file__).resolve().parents[2] / '.work/loader-calls.jsonl'
with log.open('a') as stream:
    stream.write(json.dumps({'device': device, 'work': str(work), 'resume': resume}) + '\\n')
output.write_text(marker.read_text())
''')
        builder.chmod(0o755)
        patch_file = builder.parent / 'patches/0001.patch'
        patch_file.parent.mkdir()
        patch_file.write_text('original loader recipe\n')
        legacy = builder.parent / '.work/j71b/source/legacy.txt'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('preserve earlier workspace\n')
        overrides = {key: value for key, value in self.overrides.items() if key != 'loader'}
        overrides['kernel'] = self.root / 'explicit-kernel'

        def prepare(device='j71b'):
            return build.prepare_components(self.one, self.cache, device=device, **overrides)

        def calls():
            return [json.loads(line) for line in (self.one / '.work/loader-calls.jsonl').read_text().splitlines()]

        original_key, original_work = build._loader_recipe(self.one, 'j71b')
        first = prepare()
        self.assertEqual(calls(), [{'device': 'j71b', 'work': str(original_work), 'resume': False}])
        self.assertEqual(prepare()['loader'], first['loader'])
        self.assertEqual(len(calls()), 1)

        source = original_work / 'source/preserve.txt'
        original = source.read_text()
        (original_work / 'compiler-output.o').write_text('preserved object')
        source.write_text('editable loader source')
        edited = prepare()['loader']
        self.assertNotEqual(edited, first['loader'])
        self.assertEqual(edited.read_text(), 'editable loader source')
        self.assertEqual(calls()[-1], {'device': 'j71b', 'work': str(original_work), 'resume': True})
        self.assertEqual(prepare()['loader'], edited)
        self.assertEqual((original_work / 'compiler-output.o').read_text(), 'preserved object')
        source.write_text(original)
        self.assertEqual(prepare()['loader'], first['loader'])

        baseline = original_work / 'orchard-cli-source-tree'
        recorded = baseline.read_text()
        baseline.unlink()
        source.write_text('unrecorded workspace edit')
        unknown = prepare()['loader']
        self.assertNotEqual(unknown, first['loader'])
        self.assertEqual(unknown.read_text(), 'unrecorded workspace edit')
        self.assertFalse(baseline.exists())
        source.write_text(original)
        baseline.write_text(recorded)

        source.write_text('edit before a build')
        (original_work / 'change-during-build').touch()
        with self.assertRaisesRegex(ValueError, 'source changed during build'):
            prepare()
        self.assertEqual(source.read_text(), 'changed during build')
        (original_work / 'change-during-build').unlink()
        source.write_text(original)

        shutil.rmtree(first['loader'].parent)
        prepare()
        self.assertEqual(calls()[-1], {'device': 'j71b', 'work': str(original_work), 'resume': True})
        preserved = (original_work / 'source/preserve.txt').read_bytes()
        patch_file.write_text('changed loader recipe\n')
        changed_key, _ = build._loader_recipe(self.one, 'j71b')
        changed = prepare()
        self.assertNotEqual(changed['loader'], first['loader'])
        self.assertEqual(calls()[-1], {'device': 'j71b',
            'work': str(self.one / '.work/m1n1/j71b' / changed_key), 'resume': False})
        prepare('n61')
        self.assertEqual(calls()[-1], {'device': 'n61',
            'work': str(self.one / '.work/m1n1/n61' / changed_key), 'resume': False})
        with patch.dict(os.environ, {'ORCHARD_BUILD_NATIVE':'1', 'ORCHARD_BUILD_ENV_ID':'toolchain-b'}):
            alternate_key, alternate_work = build._loader_recipe(self.one, 'j71b')
            alternate = prepare()['loader']
            self.assertNotEqual(alternate_key, changed_key)
            self.assertNotEqual(alternate, changed['loader'])
            self.assertEqual(calls()[-1], {'device':'j71b', 'work':str(alternate_work), 'resume':False})
        self.assertEqual((original_work / 'source/preserve.txt').read_bytes(), preserved)
        self.assertEqual(legacy.read_text(), 'preserve earlier workspace\n')

    def test_loader_rejects_dirty_submodules_without_changing_them(self):
        sub = self.root / 'submodule'
        source = self.root / 'loader-repo'
        for directory in (sub, source):
            subprocess.run(['git', 'init', '-q', str(directory)], check=True)
        (sub / 'driver.c').write_text('original\n')
        subprocess.run(['git', '-C', str(sub), 'add', 'driver.c'], check=True)
        subprocess.run(['git', '-C', str(sub), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', 'fixture'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'protocol.file.allow=always',
                        'submodule', 'add', '-q', str(sub), 'dependency'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', 'loader'], check=True)
        build._loader_source_tree(source)
        local_edit = source / 'dependency/driver.c'
        local_edit.write_text('local experiment\n')
        with self.assertRaisesRegex(ValueError, 'submodule has local changes'):
            build._loader_source_tree(source)
        self.assertEqual(local_edit.read_text(), 'local experiment\n')

    def test_loader_workspace_is_serialized_across_distinct_component_caches(self):
        builder = self.one / 'boot/m1n1/build'
        builder.parent.mkdir(parents=True)
        builder.write_text('#!' + sys.executable + '\n' + '''
import fcntl
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
work = Path(args[args.index('--work-dir') + 1])
output = Path(args[args.index('--output') + 1])
guard = Path(__file__).resolve().parents[2] / '.work/race-guard'
guard.parent.mkdir(parents=True, exist_ok=True)
with guard.open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (guard.parent / 'build-started').touch()
    time.sleep(0.8)
    source = work / 'source'
    if not source.exists():
        source.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        (source / 'code').write_text('loader')
        subprocess.run(['git', '-C', str(source), 'add', 'code'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', 'loader'], check=True)
    output.write_text((source / 'code').read_text())
''')
        builder.chmod(0o755)
        patch_file = builder.parent / 'patches/0001.patch'
        patch_file.parent.mkdir()
        patch_file.write_text('loader recipe\n')
        program = '''
from pathlib import Path
import sys
from orchard_cli import build
root, hardware, cache = map(Path, sys.argv[1:])
build.prepare_components(hardware, cache, device='j71b', kernel=root / 'kernel',
                         busybox=root / 'busybox', pongo=root / 'pongo')
'''
        first = subprocess.Popen([sys.executable, '-c', program, str(self.root), str(self.one),
                                  str(self.root / 'cache-one')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        marker = self.one / '.work/build-started'
        deadline = time.monotonic() + 10
        while not marker.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), 'first loader build did not start')
        second = subprocess.Popen([sys.executable, '-c', program, str(self.root), str(self.one),
                                   str(self.root / 'cache-two')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: second.poll() is None and second.kill())
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, (stdout + stderr).decode())


if __name__ == '__main__':
    unittest.main()
