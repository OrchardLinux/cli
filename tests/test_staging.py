"""Exercise real file hashing for pinned/generated firmware and existing outputs."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from orchard_cli.staging import stage


class FirmwareStaging(unittest.TestCase):
    def test_real_checksums_cover_selection_copy_existing_and_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs'
            inputs.mkdir()
            manifest = root / 'manifest.json'
            receipt = root / 'generated.json'
            rows = []
            for name, data in (('pinned.bin',b'pinned'),('generated.bin',b'generated')):
                (inputs / name).write_bytes(data)
                rows.append({'name':name,'destination':'lib/firmware/'+name,
                             'sha256':hashlib.sha256(data).hexdigest()})
            manifest.write_text(json.dumps({'device':'fixture','files':[rows[0]],'generated':[rows[1]]}))
            receipt.write_text(json.dumps({'device':'fixture','name':'generated.bin','bytes':9,
                                           'sha256':rows[1]['sha256']}))
            output = root / 'output'
            self.assertEqual(stage(manifest,[inputs],[receipt],output),2)
            self.assertEqual((output/'lib/firmware/pinned.bin').read_bytes(),b'pinned')
            self.assertEqual((output/'lib/firmware/generated.bin').read_bytes(),b'generated')
            self.assertEqual(stage(manifest,[inputs],[receipt],output),2)
            (output/'lib/firmware/pinned.bin').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'existing output differs'):
                stage(manifest,[inputs],[receipt],output)
            (inputs/'pinned.bin').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'matching local input'):
                stage(manifest,[inputs],[receipt],root/'unwritten')
            self.assertFalse((root/'unwritten').exists())


if __name__ == '__main__':
    unittest.main()
