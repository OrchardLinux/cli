"""Cross-checkout reuse with real local Git and mocked compiler recipes."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from orchard_cli import build, sources, kernel


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
                'kernel/configs/j71b/config': 'config',
                'kernel/devicetree/j71b/overlay': 'dtb',
                'kernel/modules/j71b/power/driver.c': 'external driver',
                'kernel/upstream.lock': f'url={self.upstream.as_uri()}\ncommit={self.commit}\n',
            }.items():
                path = hardware / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        self.overrides = {}
        for name in ('busybox', 'pongo', 'loader'):
            path = self.root / name
            path.write_text('explicit local component')
            self.overrides[name] = path
        self.calls = []

    def produce(self, hardware, cache, device, output, base=None):
        self.assertEqual(cache, self.cache)
        self.assertEqual(device, 'j71b')
        source = hardware / 'kernel/.work/j71b/source'
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
        else:
            self.calls.append('external-modules-and-dtb')
            self.assertTrue((base / 'sdk/Module.symvers').is_file())
            for name in ('Image.gz', 'kernel.release'):
                shutil.copyfile(base / name, output / name)
            shutil.copytree(base / 'lib', output / 'lib')
            (output / 'lib/modules/test-release/external.ko').write_text(
                (hardware / 'kernel/modules/j71b/power/driver.c').read_text())
            (output / 'j71b.dtb').write_text((hardware / 'kernel/devicetree/j71b/overlay').read_text())

    def prepare(self, hardware):
        with patch.object(kernel, 'build_base', side_effect=self.produce), \
                patch.object(kernel, 'build_device', side_effect=lambda hw, cache, device, base, out:
                             self.produce(hw, cache, device, out, base)):
            return build.prepare_components(hardware, self.cache, **self.overrides)

    def test_fresh_second_checkout_neither_downloads_checks_out_nor_compiles_linux(self):
        first = self.prepare(self.one)
        self.assertEqual(self.calls, ['kernel-base', 'external-modules-and-dtb'])
        # No upstream access possible on the second build.
        self.upstream.rename(self.root / 'upstream-offline')
        second = self.prepare(self.two)
        self.assertEqual(first['kernel'], second['kernel'])
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.two / 'kernel/.work').exists())

    def test_external_driver_change_reuses_kernel_base_and_sdk(self):
        first = self.prepare(self.one)
        (self.two / 'kernel/modules/j71b/power/driver.c').write_text('changed external driver')
        second = self.prepare(self.two)
        self.assertEqual(self.calls, ['kernel-base', 'external-modules-and-dtb', 'external-modules-and-dtb'])
        self.assertNotEqual(first['kernel'], second['kernel'])
        self.assertEqual(build.digest(first['kernel'] / 'Image.gz'), build.digest(second['kernel'] / 'Image.gz'))
        self.assertEqual((second['kernel'] / 'lib/modules/test-release/external.ko').read_text(),
                         'changed external driver')

    def test_readme_other_device_and_firmware_changes_do_not_rebuild_kernel(self):
        first = self.prepare(self.one)
        for relative in ('README.md', 'devices/j71b/firmware.json', 'kernel/configs/other/config',
                         'kernel/modules/other/driver.c'):
            path = self.two / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('unrelated change')
        second = self.prepare(self.two)
        self.assertEqual(first['kernel'], second['kernel'])
        self.assertEqual(len(self.calls), 2)

    def test_config_and_local_in_tree_edits_rebuild_the_base_without_reset(self):
        self.prepare(self.one)
        (self.two / 'kernel/configs/j71b/config').write_text('changed config')
        self.prepare(self.two)
        self.assertEqual(self.calls.count('kernel-base'), 2)
        source = self.one / 'kernel/.work/j71b/source'
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

    def test_dtb_edit_only_rebuilds_device_layer(self):
        self.prepare(self.one)
        (self.two / 'kernel/devicetree/j71b/overlay').write_text('changed overlay')
        self.prepare(self.two)
        self.assertEqual(self.calls.count('kernel-base'), 1)
        self.assertEqual(self.calls.count('external-modules-and-dtb'), 2)


if __name__ == '__main__':
    unittest.main()
