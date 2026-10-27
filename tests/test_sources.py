"""File-backed Git acquisition tests. No network or device access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchard_cli import sources


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        self.cache = self.root / 'cache'
        self.git('init', '-q')
        (self.upstream / 'driver.c').write_text('original\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').decode()
        self.url = self.upstream.as_uri()

    def git(self, *args):
        return sources.git(self.upstream, *args)

    def test_two_checkouts_share_objects_without_contacting_upstream_again(self):
        first, second = self.root / 'first', self.root / 'second'
        sources.checkout(self.url, self.commit, first, cache=self.cache)
        # Make the original URL unavailable: the second checkout must be offline.
        self.upstream.rename(self.root / 'upstream-offline')
        sources.checkout(self.url, self.commit, second, cache=self.cache)
        self.assertEqual((first / 'driver.c').read_text(), 'original\n')
        self.assertEqual((second / 'driver.c').read_text(), 'original\n')
        self.assertEqual((first / '.git/objects/info/alternates').read_text(),
                         (second / '.git/objects/info/alternates').read_text())
        (first / 'driver.c').write_text('local edit\n')
        self.assertEqual((second / 'driver.c').read_text(), 'original\n')
        with self.assertRaises(ValueError):
            sources.checkout(self.url, self.commit, first, cache=self.cache)
        self.assertEqual((first / 'driver.c').read_text(), 'local edit\n')

    def test_declared_tree_matches_patched_checkout_and_preserves_user_index(self):
        patchfile = self.root / 'change.patch'
        (self.upstream / 'driver.c').write_text('patched\n')
        patchfile.write_bytes(self.git('diff', '--binary') + b'\n')
        self.git('checkout', '--', 'driver.c')
        tree = sources.declared_tree(self.url, self.commit, [patchfile], cache=self.cache)
        local = self.root / 'local'
        sources.checkout(self.url, self.commit, local, cache=self.cache)
        sources.git(local, 'apply', patchfile)
        index_before = (local / '.git/index').read_bytes()
        self.assertEqual(tree, sources.working_tree(local))
        self.assertEqual(index_before, (local / '.git/index').read_bytes())
        (local / 'new.c').write_text('untracked\n')
        self.assertNotEqual(tree, sources.working_tree(local))

    def test_local_checkout_can_seed_cache_without_remote_access(self):
        missing_url = (self.root / 'missing-origin').as_uri()
        with sources.repository(missing_url, self.commit, cache=self.cache, donor=self.upstream) as bare:
            self.assertEqual(sources.git(bare, 'rev-parse', self.commit).decode(), self.commit)

    def test_cli_prepares_source_for_unmodified_hardware_builder_without_resetting_edits(self):
        from orchard_cli import kernel
        hardware = self.root / 'hardware'
        patches = hardware / 'kernel/patches/j71b'
        patches.mkdir(parents=True)
        (hardware / 'kernel/upstream.lock').write_text(f'url={self.url}\ncommit={self.commit}\n')
        (self.upstream / 'driver.c').write_text('patched\n')
        patchfile = patches / '0001-driver.patch'
        patchfile.write_bytes(self.git('diff', '--binary') + b'\n')
        self.git('checkout', '--', 'driver.c')
        source, output = kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'patched\n')
        self.assertTrue(output.is_dir())
        self.assertTrue((source.parent / 'applied-patches').is_file())
        (source / 'driver.c').write_text('local experiment\n')
        kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'local experiment\n')
        patchfile.write_bytes(patchfile.read_bytes() + b'\n')
        with self.assertRaises(ValueError):
            kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'local experiment\n')

    def test_cache_location_override(self):
        with patch.dict(os.environ, {'ORCHARD_CACHE_DIR': str(self.cache)}):
            self.assertEqual(sources.cache_root(), self.cache)


if __name__ == '__main__':
    unittest.main()
