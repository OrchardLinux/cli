"""RAM building/boot tests. Synthetic components; real packer, mocked USB."""
from contextlib import nullcontext
import gzip
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import orchard_hardware
from orchard_cli import boot, build, ram


class RamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.hardware = Path(orchard_hardware.__file__).resolve().parent
        self.kernel = self.base / 'kernel'
        modules = self.kernel / 'lib/modules/test-release'
        modules.mkdir(parents=True)
        (modules / 'modules.dep').write_text('test.ko:\n')
        (modules / 'test.ko').write_bytes(b'not a real module')
        (self.kernel / 'kernel.release').write_text('test-release\n')
        (self.kernel / 'Image.gz').write_bytes(b'not a real kernel')
        (self.kernel / 'j71b.dtb').write_bytes(b'not a real DTB')
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        elf[18:20] = b'\xb7\x00'
        struct.pack_into('<Q', elf, 32, 64)
        struct.pack_into('<HH', elf, 54, 56, 0)
        self.busybox = self.base / 'busybox'
        self.busybox.write_bytes(elf)
        self.loader, self.pongo = self.base / 'm1n1.bin', self.base / 'pongo.bin'
        self.loader.write_bytes(b'loader')
        self.pongo.write_bytes(b'pongo')
        self.script = self.base / 'test.sh'
        self.script.write_text('#!/bin/sh\necho test\n')
        self.script.chmod(0o755)
        self.init = self.base / 'init'
        self.init.write_text('#!/bin/busybox sh\necho test startup\n')
        self.init.chmod(0o755)
        self.output = self.base / 'out'
        self.components = dict(kernel=self.kernel, busybox=self.busybox,
                               loader=self.loader, pongo=self.pongo)

    def build(self, **kwargs):
        options = dict(init=self.init, files={'usr/bin/test': self.script}, modules=True)
        options.update(kwargs)
        return ram.assemble_ram(self.hardware, self.output, **self.components, **options)

    def test_real_packaging_and_repeat(self):
        self.build()
        self.assertEqual(ram.verify_ram(self.output)['device'], 'j71b')
        archive = gzip.decompress((self.output / 'initramfs.cpio.gz').read_bytes())
        self.assertTrue(archive.startswith(b'070701'))
        self.assertIn(b'usr/bin/test\0', archive)
        self.assertIn(b'lib/modules/test-release/test.ko\0', archive)
        receipt = json.loads((self.output / 'linux.bin.json').read_text())
        self.assertEqual([row['name'] for row in receipt['components']],
                         ['loader', 'bootargs', 'dtb', 'kernel', 'initramfs_header', 'initramfs'])
        self.build()
        ram.verify_ram(self.output)

    def test_custom_init_and_no_modules(self):
        custom = self.base / 'init'
        custom.write_text('#!/bin/busybox sh\necho custom\n')
        self.build(init=custom, modules=False, files={})
        archive = gzip.decompress((self.output / 'initramfs.cpio.gz').read_bytes())
        self.assertIn(custom.read_bytes(), archive)
        self.assertNotIn(b'test.ko\0', archive)
        self.assertNotIn(b'charger-test\0', archive)

    def test_changed_payload_and_source_are_refused(self):
        self.build()
        (self.output / 'linux.bin').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            ram.verify_ram(self.output)
        self.build()
        self.script.write_text('changed input')
        with self.assertRaises(ValueError):
            ram.verify_ram(self.output)

    def test_host_launcher_edit_does_not_invalidate_build(self):
        self.build()
        (self.base / 'run.py').write_text('# host-only change')
        ram.verify_ram(self.output)

    def test_failed_build_invalidates_previous_output(self):
        self.build()
        (self.kernel / 'lib/modules/test-release/modules.dep').unlink()
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse((self.output / 'build.json').exists())

    def test_dynamic_wrong_arch_and_truncated_busybox_rejected(self):
        original = self.busybox.read_bytes()
        for content in (b'bad', original[:6] + b'\0' * 58):
            self.busybox.write_bytes(content)
            with self.assertRaises(ValueError):
                self.build()
        elf = bytearray(original)
        struct.pack_into('<H', elf, 56, 1)
        elf.extend(struct.pack('<I', 3) + b'\0' * 52)
        self.busybox.write_bytes(elf)
        with self.assertRaises(ValueError):
            self.build()

    def test_file_traversal_and_collision_rejected(self):
        for destination in ('../escape', '/escape', 'init', 'bin/busybox'):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.build(files={destination: self.script})

    def test_legacy_payload_survives_host_refactor(self):
        self.build()
        path = self.output / 'build.json'
        receipt = json.loads(path.read_text())
        receipt.pop('kind')
        receipt.pop('sources')
        receipt['example_sources'] = {'removed-host-script': 'old hash'}
        path.write_text(json.dumps(receipt))
        ram.verify_ram(self.output)

    def test_confirmation(self):
        for answer in ('', 'y', 'Y', 'yes', ' YES '):
            with self.subTest(answer=answer), patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value=answer):
                boot.confirm_boot()
        for answer in ('n', 'no', 'boot', 'unknown'):
            with self.subTest(answer=answer), patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value=answer), self.assertRaises(ValueError):
                boot.confirm_boot()
        with patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=EOFError), self.assertRaises(ValueError):
            boot.confirm_boot()
        with patch('sys.stdin.isatty', return_value=False), patch('builtins.input') as prompt:
            with self.assertRaises(ValueError):
                boot.confirm_boot()
            prompt.assert_not_called()

    def test_boot_stops_before_usb_mutation(self):
        self.build()
        for device in ({'mode': 'linux'}, {'mode': 'pongo', 'chip': 0x8010,
                         'board': 0x18, 'ecid': 123, 'location': 'fake'}):
            with patch('orchard_cli.apple.usb.sole_target', return_value=device), \
                    patch('orchard_cli.apple.usb.selected') as selected, \
                    patch('orchard_cli.transports.pongo.upload') as upload, \
                    patch('orchard_cli.state.operation_lock', return_value=nullcontext()), \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value='no'), self.assertRaises(ValueError):
                boot.boot_ram(self.output)
            selected.assert_not_called()
            upload.assert_not_called()

    def test_pongo_boot_uses_no_rpc_or_readiness_claim(self):
        self.build()
        device = dict(mode='pongo', chip=0x8010, board=0x18, ecid=123, location='fake')
        with patch('orchard_cli.apple.usb.sole_target', return_value=device), \
                patch('orchard_cli.apple.usb.selected') as selected, \
                patch('orchard_cli.transports.pongo.upload') as upload, \
                patch('orchard_cli.state.operation_lock', return_value=nullcontext()), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=''), patch('builtins.print') as output:
            boot.boot_ram(self.output)
        self.assertEqual(selected.call_count, 2)
        upload.assert_called_once()
        self.assertIn('NOT been verified', str(output.call_args_list))

    def test_cli_dispatches_directly_without_wrapper_or_outer_device_lock(self):
        from orchard_cli.command import main
        with patch('sys.argv', ['orchard', 'build', str(self.base), '--pongo', 'local.bin', '--modules']), \
                patch.object(ram, 'build_directory') as build, \
                patch('orchard_cli.command.operation_lock') as lock:
            main()
            build.assert_called_once_with(self.base, hardware=None, cache=None, kernel=None,
                                          busybox=None, loader=None, pongo=Path('local.bin'), modules=True)
            lock.assert_not_called()
        with patch('sys.argv', ['orchard', 'run', str(self.base)]), \
                patch.object(boot, 'run_directory') as run:
            main()
            run.assert_called_once_with(self.base)

    def test_directory_content_builds_without_python_recipes(self):
        example = self.base / 'example'
        example.mkdir()
        shutil.copyfile(self.init, example / 'init')
        tool = example / 'rootfs/usr/bin/test'
        tool.parent.mkdir(parents=True)
        shutil.copy2(self.script, tool)
        with patch.object(build, 'prepare_components', return_value=self.components):
            ram.build_directory(example, hardware=self.hardware)
        output = example / 'out'
        ram.verify_ram(output)
        archive = gzip.decompress((output / 'initramfs.cpio.gz').read_bytes())
        self.assertIn(b'usr/bin/test\0', archive)
        self.assertNotIn(b'test.ko\0', archive)  # modules are opt-in
        self.assertFalse((example / 'build.py').exists())
        (tool.parent / 'new-file').write_text('new input')
        with self.assertRaises(ValueError):
            ram.verify_ram(output)
        with patch.object(build, 'prepare_components', return_value=self.components):
            ram.build_directory(example, hardware=self.hardware, modules=True)
        self.assertIn(b'test.ko\0', gzip.decompress((output / 'initramfs.cpio.gz').read_bytes()))

    def test_arbitrary_directory_layout_uses_active_hardware_package(self):
        for relative in ('examples/demo', 'use_cases/nested/charger 001', 'unrelated-project'):
            directory = self.base / relative
            directory.mkdir(parents=True)
            shutil.copyfile(self.init, directory / 'init')
            with self.subTest(relative=relative), \
                    patch.object(build, 'prepare_components', return_value=self.components) as prepare, \
                    patch.object(build, 'cache_root', return_value=self.base / 'shared-cache'):
                ram.build_directory(directory)
                prepare.assert_called_once_with(self.hardware, self.base / 'shared-cache', kernel=None,
                                                busybox=None, loader=None, pongo=None)
                ram.verify_ram(directory / 'out')
                if directory.name == 'charger 001':
                    archive = gzip.decompress((directory / 'out/initramfs.cpio.gz').read_bytes())
                    self.assertIn(b'charger-001\n', archive)

    def test_explicit_hardware_override_takes_precedence_over_active_package(self):
        directory = self.base / 'use_cases/override'
        directory.mkdir(parents=True)
        shutil.copyfile(self.init, directory / 'init')
        with patch.object(orchard_hardware, '__file__', str(self.base / 'unused-hardware/__init__.py')), \
                patch.object(build, 'prepare_components', return_value=self.components) as prepare:
            ram.build_directory(directory, hardware=self.hardware)
        self.assertEqual(prepare.call_args.args[0], self.hardware)
        ram.verify_ram(directory / 'out')

    def test_run_directory_has_no_parent_layout_dependency(self):
        directory = self.base / 'use_cases/deeply/nested/system'
        with patch.object(boot, 'boot_ram') as upload:
            boot.run_directory(directory)
        upload.assert_called_once_with(directory / 'out')

    def test_directory_requires_visible_init_before_building_components(self):
        with patch.object(build, 'prepare_components') as components:
            with self.assertRaises(ValueError):
                ram.build_directory(self.base / 'missing-init', hardware=self.hardware)
            components.assert_not_called()

    def test_cache_lock_refuses_concurrent_build(self):
        cache = self.base / 'cache'
        with build.component_lock(cache):
            with self.assertRaises(ValueError):
                with build.component_lock(cache):
                    self.fail('Lock was not held')


if __name__ == '__main__':
    unittest.main()
