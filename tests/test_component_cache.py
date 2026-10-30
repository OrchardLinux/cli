"""Cache keys record their inputs; misses say what changed; entries can be listed and removed."""
import contextlib
import io
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

    def publish(self, inputs, data=b'kernel'):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            path = build.cached_component(self.parent, 'kernel-j71b', build.keyed(lambda: inputs),
                                          lambda stage: (stage / 'Image.gz').write_bytes(data))
        return path, out.getvalue()

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


if __name__ == '__main__':
    unittest.main()
