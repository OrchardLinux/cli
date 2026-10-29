"""Build provenance distinguishes clean checkouts, local edits and installed files."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from orchard_cli import provenance


class SourceProvenance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / 'package'
        self.package.mkdir()
        (self.package / '__init__.py').write_text('first\n')

    def test_installed_files_do_not_claim_a_git_revision(self):
        result = provenance.snapshot(self.package)
        self.assertEqual(result['kind'], 'installed')
        self.assertNotIn('git', result)

    def test_checkout_records_head_and_flags_edits(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', 'package'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', 'fixture'], check=True)
        clean = provenance.snapshot(self.package)
        self.assertEqual(clean['kind'], 'checkout')
        self.assertTrue(clean['git']['clean'])
        self.assertEqual(len(clean['git']['head']), 40)
        (self.package / '__init__.py').write_text('changed\n')
        dirty = provenance.snapshot(self.package)
        self.assertFalse(dirty['git']['clean'])
        self.assertEqual(dirty['git']['head'], clean['git']['head'])
        self.assertNotEqual(dirty['sha256'], clean['sha256'])

    def test_linked_source_files_are_refused(self):
        (self.package / 'linked').symlink_to(self.package / '__init__.py')
        with self.assertRaisesRegex(ValueError, 'Linked'):
            provenance.snapshot(self.package)


if __name__ == '__main__':
    unittest.main()
