"""Cache keys record their inputs; misses say what changed; entries share files and can be listed, removed and pruned."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest

from orchard_cli import build, components

import hardware_layout


class ComponentCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.parent = self.cache / 'components-v2'

    def publish(self, inputs, data=b'kernel', modules=b'module', name='kernel-j71b'):
        def produce(stage):
            (stage / 'Image.gz').write_bytes(data)
            (stage / 'lib').mkdir()
            (stage / 'lib/wifi.ko').write_bytes(modules)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            path = build.cached_component(self.parent, name, build.keyed(lambda: inputs), produce)
        return path, out.getvalue()

    def quietly(self, function, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            function(*args)
        return out.getvalue()

    def test_miss_names_the_inputs_that_changed(self):
        first, _ = self.publish({'base': 'a', 'dtb': 't8010-j71b', 'recipes': {'build-modules': '755:1'}})
        self.assertTrue((first / build.INPUTS_RECORD).is_file())
        _, output = self.publish({'base': 'b', 'dtb': 't8010-j71b', 'recipes': {'build-modules': '755:2'}})
        self.assertIn('differs from newest cached ' + first.name[:12] + ' in: base, recipes.build-modules', output)
        again, output = self.publish({'base': 'a', 'dtb': 't8010-j71b', 'recipes': {'build-modules': '755:1'}})
        self.assertEqual(again, first)
        self.assertIn('Reusing', output)

    def test_device_key_ignores_descriptor_text_outside_the_kernel_selection(self):
        hardware = self.cache / 'hardware'
        hardware_layout.write(hardware)
        descriptor = hardware / 'devices/j71b/device.toml'
        before = build.kernel_key(hardware, 'j71b', 'base')
        # Feature states and caveats change freely; only the DTB selection counts.
        descriptor.write_text(descriptor.read_text() + '\n[features]\nwifi = "working"\n')
        self.assertEqual(before, build.kernel_key(hardware, 'j71b', 'base'))
        descriptor.write_text(descriptor.read_text().replace('t8010-j71b', 't8010-j72b'))
        self.assertNotEqual(before, build.kernel_key(hardware, 'j71b', 'base'))

    def test_list_and_remove_entries(self):
        first, _ = self.publish({'base': 'a', 'dtb': 'x'})
        second, _ = self.publish({'base': 'b', 'dtb': 'x'}, b'other')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            components.show(self.cache)
        self.assertIn('2 entries', out.getvalue())
        with self.assertRaisesRegex(ValueError, 'No cache entry'):
            components.remove(['kernel-j72b'], self.cache)
        with contextlib.redirect_stdout(io.StringIO()):
            components.remove(['kernel-j71b/' + first.name[:12]], self.cache)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_entries_share_identical_files(self):
        first, _ = self.publish({'base': 'a'})
        second, _ = self.publish({'base': 'b'}, b'other kernel')
        self.assertTrue(os.path.samefile(first / 'lib/wifi.ko', second / 'lib/wifi.ko'))
        self.assertFalse(os.path.samefile(first / 'Image.gz', second / 'Image.gz'))
        # Reuse still verifies every file of the entry.
        self.assertIn('Reusing', self.publish({'base': 'a'})[1])

    def test_files_with_another_mode_are_not_shared(self):
        first, _ = self.publish({'base': 'a'})
        def produce(stage):
            (stage / 'Image.gz').write_bytes(b'kernel')
            (stage / 'Image.gz').chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()):
            second = build.cached_component(self.parent, 'kernel-j71b', build.keyed(lambda: {'base': 'b'}), produce)
        self.assertFalse(os.path.samefile(first / 'Image.gz', second / 'Image.gz'))
        self.assertEqual((second / 'Image.gz').stat().st_mode & 0o777, 0o755)

    def test_damaged_shared_copy_is_replaced_not_spread(self):
        first, _ = self.publish({'base': 'a'})
        (first / 'lib/wifi.ko').write_bytes(b'damaged')
        second, _ = self.publish({'base': 'b'}, b'other kernel')
        self.assertEqual((second / 'lib/wifi.ko').read_bytes(), b'module')
        with self.assertRaisesRegex(ValueError, 'Cached component changed'):
            self.publish({'base': 'a'})

    def test_prune_keeps_the_most_recently_used_entries(self):
        entries = [self.publish({'base': value}, value.encode())[0] for value in 'abc']
        for entry, when in zip(entries, (1000, 2000, 3000)):
            os.utime(entry, (when, when))
        self.publish({'base': 'a'})  # reuse makes the oldest entry the most recent
        leftover = self.parent / 'kernel-j71b/build-interrupted'
        leftover.mkdir()
        output = self.quietly(components.prune, 2, self.cache)
        self.assertTrue(entries[0].exists())
        self.assertFalse(entries[1].exists())
        self.assertTrue(entries[2].exists())
        self.assertFalse(leftover.exists())
        self.assertIn('Freed', output)
        shared = {p.name.split('-')[0] for p in (self.parent / build.SHARED_FILES).iterdir()}
        expected = {build.digest(p) for entry in (entries[0], entries[2]) for p in entry.rglob('*')
                    if p.is_file() and p.name != 'component.json'}
        self.assertEqual(shared, expected)

    def test_prune_shares_files_of_entries_published_before_sharing(self):
        first, _ = self.publish({'base': 'a'})
        second, _ = self.publish({'base': 'b'}, b'other kernel')
        private = second / 'lib/wifi.ko'
        private.unlink()
        private.write_bytes(b'module')
        self.assertFalse(os.path.samefile(first / 'lib/wifi.ko', private))
        self.quietly(components.prune, 5, self.cache)
        self.assertTrue(os.path.samefile(first / 'lib/wifi.ko', private))

    def test_listing_counts_shared_files_once_on_disk(self):
        self.publish({'base': 'a'}, modules=b'm' * 4096)
        self.publish({'base': 'b'}, b'other kernel', modules=b'm' * 4096)
        output = self.quietly(components.show, self.cache)
        self.assertIn('2 entries, 8.', output)
        self.assertIn('(4.', output)


if __name__ == '__main__':
    unittest.main()
