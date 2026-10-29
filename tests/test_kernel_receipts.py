"""The cached kernel and its device output retain hardware's receipt contract."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import orchard_hardware
from orchard_cli import build, kernel, kernel_sdk

import hardware_layout


class KernelReceipts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.hardware = self.root / 'hardware'
        hardware_layout.write(self.hardware, device='n61', platform='apple-4k', dtb='t7000-n61')
        writer = Path(orchard_hardware.__file__).resolve().parent / 'kernel/write-receipt.py'
        if not writer.is_file():
            self.skipTest('Installed hardware has no kernel receipt writer')
        shutil.copyfile(writer, self.hardware / 'kernel/write-receipt.py')
        self.writer = self.hardware / 'kernel/write-receipt.py'
        spec = importlib.util.spec_from_file_location('hardware_kernel_receipt', writer)
        self.receipt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.receipt)
        for name in ('__init__.py', 'buildenv.py', 'kernel/Containerfile', 'kernel/install-toolchain',
                     'kernel/build', 'kernel/build-modules', 'kernel/stage-modules.py', 'kernel/sources.toml'):
            path = self.hardware / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        (self.hardware / 'kernel/upstream.lock').write_text('url=file:///fixture\ncommit=' + 'a' * 40 + '\n')
        (self.hardware / 'devices/n61/device.toml').write_text(
            '[kernel]\nplatform = "apple-4k"\ndtb = "t7000-n61"\n[firmware]\nrecipe = "firmware.json"\n')
        (self.hardware / 'devices/n61/firmware.json').write_text('firmware recipe')
        other = self.hardware / 'devices/j81/device.toml'
        other.parent.mkdir(parents=True)
        other.write_text('[kernel]\nplatform = "apple-4k"\ndtb = "t7001-j81"\n')
        for name in ('apple-4k', 'n61'):
            path = self.hardware / 'kernel/modules' / name / 'module/Makefile'
            path.parent.mkdir(parents=True)
            path.write_text('module')
        self.work = kernel.work_directory(self.hardware, 'n61')
        self.work.mkdir(parents=True)
        (self.work / 'applied-patches').write_text('')
        (self.work / 'prepared-tree').write_text('c' * 40 + '\n')
        self.source = self.root / 'source'
        self.source.mkdir()
        self.compiler = self.root / 'compiler'
        for name in kernel_sdk.REQUIRED:
            path = self.compiler / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('sdk fixture')
        for name in kernel_sdk.DIRECTORIES:
            (self.compiler / name).mkdir(parents=True, exist_ok=True)
        config = self.hardware / 'kernel/configs/apple-4k/config'
        config.write_text('CONFIG_ARM64_4K_PAGES=y\n')
        (self.compiler / '.config').write_bytes(config.read_bytes())
        self.release = '7.3.0-rc1-orchard-apple-4k'
        (self.compiler / 'include/config/kernel.release').write_text(self.release + '\n')
        self.calls = []

    def write_modules(self, target, external=()):
        modules = target / 'lib/modules' / self.release
        (modules / 'kernel/drivers').mkdir(parents=True)
        (modules / 'kernel/drivers/core.ko').write_bytes(b'core module')
        for root in external:
            for path in root.rglob('*.ko'):
                dest = modules / 'extra' / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        (modules / 'modules.dep').write_text('kernel/drivers/core.ko:\n')

    def command(self, *argv, **kwargs):
        self.calls.append(argv)
        if Path(argv[0]) == self.hardware / 'kernel/build':
            full = Path(argv[argv.index('--output') + 1])
            (full / 'dtbs').mkdir(parents=True)
            (full / 'Image.gz').write_bytes(b'kernel image')
            (full / 'kernel.release').write_text(self.release + '\n')
            (full / 'n61.dtb').write_bytes(b'n61 dtb')
            (full / 'dtbs/t7000-n61.dtb').write_bytes(b'n61 dtb')
            (full / 'dtbs/t7001-j81.dtb').write_bytes(b'other dtb')
            self.write_modules(full)
            snapshot = self.root / 'producer-recipe.json'
            snapshot.write_text(json.dumps(self.receipt.recipe_snapshot(
                self.hardware, 'n61', 'apple-4k', self.work / 'applied-patches')))
            self.receipt.write_receipt(full, 'n61', 'apple-4k', 't7000-n61', 'a' * 40,
                'b' * 40, 'c' * 40, self.work / 'applied-patches',
                self.hardware / 'kernel/configs/apple-4k/config', self.hardware, snapshot)
        elif Path(argv[0]) == self.hardware / 'kernel/build-modules':
            destination = Path(argv[argv.index('--output') + 1])
            destination.mkdir()
            name = Path(argv[argv.index('--modules') + 1]).name
            (destination / (name + '.ko')).write_bytes((name + ' module').encode())
        elif len(argv) > 1 and Path(argv[1]) == self.hardware / 'kernel/stage-modules.py':
            destination = Path(argv[argv.index('--output') + 1])
            external = [Path(argv[index + 1]) for index, arg in enumerate(argv) if arg == '--external']
            self.write_modules(destination, external)
        elif len(argv) > 1 and Path(argv[1]) == self.writer:
            subprocess.run(list(map(str, argv)), check=True, **kwargs)
        else:
            self.assertIn('orchard_hardware.buildenv', argv)

    def produce(self):
        base = self.root / 'base'
        with patch.object(kernel, 'prepare_source', return_value=(self.source, self.compiler)), \
                patch.object(kernel.sources, 'working_tree', return_value='b' * 40), \
                patch.object(build, 'command', side_effect=self.command):
            kernel.build_base(self.hardware, self.root / 'cache', 'n61', base)
        return base

    def device(self, base, output, device='n61'):
        with patch.object(kernel, 'prepare_source', return_value=(self.source, self.compiler)), \
                patch.object(kernel.sources, 'working_tree', return_value='b' * 40), \
                patch.object(build, 'command', side_effect=self.command):
            kernel.build_device(self.hardware, self.root / 'cache', device, base, output)

    def test_device_receipt_covers_cached_image_dtbs_and_rebuilt_modules(self):
        base = self.produce()
        output = self.root / 'result'
        self.device(base, output)
        receipt = json.loads((output / 'receipt.json').read_text())
        self.assertEqual((receipt['device'], receipt['platform'], receipt['dtb']),
                         ('n61', 'apple-4k', 't7000-n61'))
        self.assertEqual((receipt['source']['tree'], receipt['source']['prepared_tree']),
                         ('b' * 40, 'c' * 40))
        self.assertIn('devices/n61/firmware.json', receipt['hardware_recipe']['files'])
        self.assertEqual(receipt['files']['n61.dtb'], receipt['files']['dtbs/t7000-n61.dtb'])
        for name in ('Image.gz', 'n61.dtb', 'dtbs/t7000-n61.dtb', 'dtbs/t7001-j81.dtb',
                     'lib/modules/' + self.release + '/extra/apple-4k.ko',
                     'lib/modules/' + self.release + '/extra/n61.ko'):
            path = output / name
            self.assertEqual(receipt['files'][name], {'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertEqual(len([call for call in self.calls if Path(call[0]) == self.hardware / 'kernel/build']), 1)

    def test_second_device_reuses_base_with_its_own_dtb_and_module_receipt(self):
        base = self.produce()
        phone, other = self.root / 'phone', self.root / 'other'
        self.device(base, phone)
        self.device(base, other, device='j81')
        receipt = json.loads((other / 'receipt.json').read_text())
        self.assertEqual((receipt['device'], receipt['platform'], receipt['dtb']),
                         ('j81', 'apple-4k', 't7001-j81'))
        self.assertEqual((other / 'j81.dtb').read_bytes(), b'other dtb')
        self.assertEqual((phone / 'Image.gz').read_bytes(), (other / 'Image.gz').read_bytes())
        self.assertIn('lib/modules/' + self.release + '/extra/apple-4k.ko', receipt['files'])
        self.assertNotIn('lib/modules/' + self.release + '/extra/n61.ko', receipt['files'])
        self.assertEqual(len([call for call in self.calls if Path(call[0]) == self.hardware / 'kernel/build']), 1)

    def test_corrupted_sdk_dtb_and_source_marker_are_rejected(self):
        base = self.produce()
        for relative in ('sdk/Module.symvers', 'lib/modules/' + self.release + '/kernel/drivers/core.ko',
                         'dtbs/t7000-n61.dtb'):
            path = base / relative
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(path=relative), self.assertRaises(ValueError):
                self.device(base, self.root / 'rejected')
            self.assertFalse((self.root / 'rejected').exists())
            path.write_bytes(original)
        (self.work / 'prepared-tree').write_text('d' * 40 + '\n')
        with self.assertRaisesRegex(ValueError, 'producer source or platform differs'):
            self.device(base, self.root / 'rejected')


if __name__ == '__main__':
    unittest.main()
