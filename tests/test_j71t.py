"""J71t adapter checks with synthetic ADT/firmware fixtures; no device or Apple data."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchard_hardware import device as hardware_device
from orchard_cli import devices
from orchard_cli.apple import usb
from orchard_cli.apple.d5500 import extract as video_decoder_extract
from orchard_cli.apple.smartio import S8000, extract as smartio_extract
from orchard_cli.contracts import Adapter
from orchard_cli.staging import stage
from orchard_cli.j71b import adapter as j71b
from orchard_cli.j71t import adapter, calibration, inputs, mixin, prepare, touch

BOOT = '11111111-1111-4111-8111-111111111111'
ECID = 0x1234


def node(name, properties=(), children=()):
    entries = [('name', name.encode() + b'\0'), *properties]
    result = struct.pack('<II', len(entries), len(children))
    for key, value in entries:
        result += key.encode().ljust(32, b'\0') + struct.pack('<I', len(value)) + value + b'\0' * (-len(value) % 4)
    return result + b''.join(children)


RECORDS = {'multi-touch-calibration': bytes(range(256)) * 4, 'alsCalibration': bytes(range(1, 73)),
           'usb-input-limit-calibration': bytes(range(2, 26)), 'local-mac-address': b'\x02\x11\x22\x33\x44\x55'}


def adt(model=b'iPad6,11', board=b'J71tAP', chosen_mac=None, **changes):
    values = {**RECORDS, **changes}
    chosen = [('unique-chip-id', struct.pack('<Q', ECID))]
    if chosen_mac is not None:
        chosen.append(('mac-address-bluetooth0', chosen_mac))
    arm = node('arm-io', children=[
        node('spi3', children=[node('multi-touch', [('multi-touch-calibration', values['multi-touch-calibration'])])]),
        node('i2c2', children=[node('als1', [('alsCalibration', values['alsCalibration'])])]),
        node('uart3', children=[node('bluetooth', [('local-mac-address', values['local-mac-address'])])])])
    return node('device-tree', [('model', model + b'\0'), ('compatible', board + b'\0' + model + b'\0AppleARM\0')],
                [node('chosen', chosen), arm,
                 node('charger', [('usb-input-limit-calibration', values['usb-input-limit-calibration'])])])


def encode32(value):
    data = value.to_bytes(4, 'little')
    return bytes((data[1], data[0], data[3], data[2]))


def packet(payload=bytes(range(64))):
    header = struct.pack('>H', len(payload) // 4) + encode32(0)
    return (touch.PREFIX + header + struct.pack('>H', sum(header)) + payload + encode32(sum(payload)))


class Registry(unittest.TestCase):
    def test_registered_module_satisfies_the_contract_without_discovery(self):
        with patch.object(adapter, 'discover', side_effect=AssertionError('Unexpected USB access')):
            self.assertIs(devices.load('j71t'), adapter)
            self.assertIsInstance(adapter, Adapter)
        record = hardware_device('j71t')
        self.assertEqual((adapter.CHIP, adapter.BOARD, adapter.PRODUCT, adapter.APPLE_PRODUCT),
                         (0x8003, 0x10, 'J71t Recovery', 'iPad6,11'))
        self.assertEqual(adapter.NAME, record['device']['name'])

    def choose(self, observed):
        with patch.object(usb, 'devices', return_value=observed):
            return devices.choose()

    def test_each_model_selects_only_its_adapter(self):
        dfu = {'mode': 'dfu', 'location': 'usb', 'ecid': ECID, 'product': None, 'serial_port': None}
        for chip, board, expected in ((0x8010, 0x18, j71b), (0x8003, 0x10, adapter)):
            with self.subTest(chip=chip):
                loaded, selected = self.choose([{**dfu, 'chip': chip, 'board': board}])
                self.assertIs(loaded, expected)
                self.assertIs(selected['resumed'], False)
        linux = {'mode': 'linux', 'location': 'usb', 'ecid': ECID, 'boot_id': BOOT,
                 'serial_port': '/dev/recovery', 'chip': None, 'board': None}
        for product, expected in (('J71b Recovery', j71b), ('J71t Recovery', adapter)):
            with self.subTest(product=product):
                loaded, selected = self.choose([{**linux, 'product': product}])
                self.assertIs(loaded, expected)
                self.assertIs(selected['resumed'], True)

    def test_unsupported_and_ambiguous_targets_report_one_error(self):
        j71s = {'mode': 'dfu', 'location': 'usb', 'ecid': ECID, 'chip': 0x8000, 'board': 0x10}
        with self.assertRaisesRegex(ValueError, '^Unsupported device; supported: '
                                    'iPad 6 \\(Wi-Fi\\), J71b; iPad 5 \\(Wi-Fi\\), J71t$'):
            self.choose([j71s])
        with self.assertRaisesRegex(ValueError, '^Unsupported device; supported:'):
            self.choose([{'mode': 'linux', 'location': 'usb', 'product': 'Other Recovery'}])
        with self.assertRaisesRegex(ValueError, '^Selected USB target is not connected$'):
            self.choose([])
        with self.assertRaises(ValueError) as raised:
            self.choose([j71s, {**j71s, 'location': 'other'}])
        self.assertTrue(str(raised.exception).startswith('Connect exactly one target'))
        self.assertEqual(str(raised.exception).count('Connect exactly one target'), 1)

    def test_profile_selection_from_adt_and_explicit_mixin_profile(self):
        self.assertEqual(devices.profile_for_adt(adt()), 'j71t')


class Calibration(unittest.TestCase):
    def test_records_are_copied_verbatim_and_staged_with_receipts(self):
        names = [row['name'] for row in json.loads(prepare.MANIFEST.read_text())['generated']]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'adt').write_bytes(adt(chosen_mac=RECORDS['local-mac-address']))
            receipts = calibration.prepare(root / 'adt', root / 'out', 'iPad6,11', 'iPad6,11', 'J71tAP', names)
            self.assertEqual(len(receipts), 4)
            expected = {'j71t-n1-calibration.bin': 'multi-touch-calibration', 'j71t-LSCI.bin': 'alsCalibration',
                        'j71t-CBAT.bin': 'usb-input-limit-calibration', 'j71t-BMac.bin': 'local-mac-address'}
            for name, prop in expected.items():
                self.assertEqual((root / 'out' / name).read_bytes(), RECORDS[prop])
                self.assertEqual((root / 'out' / name).stat().st_mode & 0o777, 0o600)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'device': 'iPad6,11', 'files': [], 'generated': [
                {'name': name, 'destination': 'lib/firmware/apple/' + name, 'generator': 'j71t-adt'}
                for name in names]}))
            self.assertEqual(stage(manifest, [root / 'out'], receipts, root / 'staged'), 4)
            with self.assertRaisesRegex(ValueError, 'empty directory'):
                calibration.prepare(root / 'adt', root / 'out', 'iPad6,11', 'iPad6,11', 'J71tAP', names)
            with self.assertRaisesRegex(ValueError, 'recipe and ADT generator'):
                calibration.prepare(root / 'adt', root / 'other', 'iPad6,11', 'iPad6,11', 'J71tAP', names[1:])

    def test_wrong_board_and_unusable_records_are_refused(self):
        cases = [(adt(board=b'J71sAP'), 'not for board'), (adt(model=b'iPad7,5'), 'does not describe'),
                 (adt(**{'multi-touch-calibration': b'syscfg/MtCl/0x400,zeroes/0x400'}), 'Unresolved'),
                 (adt(**{'multi-touch-calibration': b'\0' * 1024}), 'Blank'),
                 (adt(**{'multi-touch-calibration': b'\xff' * 1024}), 'Blank'),
                 (adt(**{'usb-input-limit-calibration': b'\x01' * 23}), 'size'),
                 (adt(**{'alsCalibration': b'\x01' * 4}), 'size'),
                 (adt(**{'local-mac-address': b'\x01\x11\x22\x33\x44\x55'}), 'unicast'),
                 (adt(chosen_mac=b'\x02\x11\x22\x33\x44\x66'), 'disagree')]
        for blob, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                calibration.records(blob, 'iPad6,11', 'J71tAP')

    def test_capture_requires_the_j71t_board(self):
        with patch.object(adapter.adt_capture, 'capture', return_value=adt(board=b'J71sAP')):
            with self.assertRaisesRegex(ValueError, 'Captured ADT is not for iPad6,11 \\(J71tAP\\)'):
                adapter.read_adt()
        with patch.object(adapter.adt_capture, 'capture', return_value=adt()):
            self.assertEqual(adapter.capture_adt({'ecid': ECID}), adt())
            with self.assertRaisesRegex(ValueError, 'identity differs'):
                adapter.capture_adt({'ecid': ECID + 1})


class Touch(unittest.TestCase):
    def test_boot_packet_is_returned_verbatim_after_framing_checks(self):
        data = packet()
        profile = plistlib.dumps({'C1F15,1': {'Constructed Firmware': data, 'Constructed Firmware Version': 'x'}})
        self.assertEqual(touch.firmware(profile), data)
        broken = [data[:3] + b'\0' + data[4:], data[:-1], data[:6] + encode32(1) + data[10:],
                  data[:10] + b'\0\0' + data[12:], data[:20] + bytes([data[20] ^ 1]) + data[21:]]
        for item in broken:
            with self.subTest(item=item[:12]), self.assertRaises(ValueError):
                touch.validate(item)
        for document in ({'C1F15,1': {}}, {'C1F15,1': {'Constructed Firmware': data}, 'other': {}}):
            with self.assertRaises(ValueError):
                touch.firmware(plistlib.dumps(document))


class Inputs(unittest.TestCase):
    def test_derived_inputs_use_their_board_sources(self):
        mtprops = plistlib.dumps({'C1F15,1': {'Constructed Firmware': packet()}})
        derived = {'BCM4355C0-FearA-USI.hcd': b'hcd', 's8000-smartio.bin': b'smartio', 'j71t-n1.bin': packet(),
                   'vxd-deva.bin': b'deva', 'feara.trx': b'wifi'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {'device': 'iPad6,11', 'ipsw_url': 'https://example.invalid/ipsw', 'ipsw_bytes': 1,
                    'ipsw_sha256': '0' * 64, 'extract_patterns': ['C-4355__s-C0/', 'J71t[.]mtprops$'],
                    'extracted_inputs': [{'name': 'J71t.mtprops', 'sha256': hashlib.sha256(mtprops).hexdigest()}],
                    'files': [{'name': name, 'sha256': hashlib.sha256(data).hexdigest(),
                               'destination': 'lib/firmware/' + name} for name, data in derived.items()]}
            (root / 'recipe.json').write_text(json.dumps(spec))

            def extract(_tool, _archive, patterns, output):
                self.assertIn('usr/sbin/BlueTool$', patterns)
                (output / 'wifi').mkdir()
                (output / 'wifi/feara.trx').write_bytes(b'wifi')
                (output / 'J71t.mtprops').write_bytes(mtprops)
                (output / 'BlueTool').write_bytes(b'tool')
            with patch.object(inputs, 'MANIFEST', root / 'recipe.json'), \
                 patch.object(inputs, 'cache_root', return_value=root / 'cache'), \
                 patch.object(inputs, 'ensure_archive', return_value=root / 'archive'), \
                 patch.object(inputs.ipsw, 'tool', return_value='ipsw'), \
                 patch.object(inputs.ipsw, 'extract_files', side_effect=extract), \
                 patch.object(inputs.ipsw, 'hcd_from_binary', return_value=b'hcd') as hcd, \
                 patch.object(inputs.ipsw, 'extract_kernel', return_value=b'kernel') as kernel, \
                 patch.object(inputs, 'smartio', return_value=(b'smartio', {})) as smartio, \
                 patch.object(inputs, 'video_decoder', return_value=(b'deva', {})) as video_decoder:
                firmware = inputs.ensure(Mock(), Mock())
                hcd.assert_called_once_with(b'tool', hashlib.sha256(b'hcd').hexdigest())
                kernel.assert_called_once()
                smartio.assert_called_once_with(b'kernel', S8000)
                video_decoder.assert_called_once_with(b'kernel')
                self.assertEqual((firmware / 'extra/vxd-deva.bin').read_bytes(), b'deva')
                self.assertEqual((firmware / 'extra/j71t-n1.bin').read_bytes(), packet())
                self.assertEqual((firmware / 'extracted/feara.trx').read_bytes(), b'wifi')
                self.assertEqual(inputs.ensure(Mock(), Mock(), allow_download=False), firmware)

    def test_mixin_contains_exactly_the_recipe_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pinned = {'feara.trx': b'wifi', 'j71t-n1.bin': packet()}
            real = json.loads(prepare.MANIFEST.read_text())
            spec = {'device': 'iPad6,11', 'files': [
                {'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'destination': 'lib/firmware/x/' + name}
                for name, data in pinned.items()], 'generated': real['generated']}
            (root / 'recipe.json').write_text(json.dumps(spec))
            (root / 'firmware/extracted').mkdir(parents=True)
            for name, data in pinned.items():
                (root / 'firmware/extracted' / name).write_bytes(data)
            (root / 'adt').write_bytes(adt())
            with patch.object(prepare, 'MANIFEST', root / 'recipe.json'), \
                 patch.object(mixin, 'MANIFEST', root / 'recipe.json'):
                archive, receipt = adapter.prepare_mixin(root / 'adt', root / 'firmware', root / 'mixin')
            self.assertEqual(receipt['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            with tarfile.open(archive) as bundle:
                names = sorted(bundle.getnames())
                metadata = json.loads(bundle.extractfile('mixin.json').read())
            destinations = sorted([row['destination'] for row in spec['files'] + spec['generated']])
            self.assertEqual(names, sorted(destinations + ['mixin.json']))
            self.assertEqual(metadata['profile'], 'j71t')
            self.assertEqual(metadata['adt_sha256'], hashlib.sha256(adt()).hexdigest())


@unittest.skipUnless(os.environ.get('ORCHARD_TEST_LOCAL_J71T'),
                     'set ORCHARD_TEST_LOCAL_J71T to a directory with local 20H350 inputs')
class LocalInputs(unittest.TestCase):
    """Optional: reproduce pinned recipe hashes from a developer's own extracted 20H350 files."""

    def find(self, name):
        matches = [p for p in Path(os.environ['ORCHARD_TEST_LOCAL_J71T']).rglob(name) if p.is_file()]
        if len(matches) != 1:
            self.skipTest('missing local input ' + name)
        return matches[0].read_bytes()

    def test_pinned_derived_inputs_reproduce(self):
        pinned = {row['name']: row['sha256'] for row in json.loads(prepare.MANIFEST.read_text())['files']}
        checks = {'j71t-n1.bin': lambda: touch.firmware(self.find('J71t.mtprops')),
                  's8000-smartio.bin': lambda: smartio_extract(self.find('kernelcache*'), S8000)[0],
                  'vxd-deva.bin': lambda: video_decoder_extract(self.find('kernelcache*'))[0],
                  'BCM4355C0-FearA-USI.hcd': lambda: inputs.ipsw.hcd_from_binary(
                      self.find('BlueTool'), pinned['BCM4355C0-FearA-USI.hcd'])}
        for name, produce in checks.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256(produce()).hexdigest(), pinned[name])


if __name__ == '__main__':
    unittest.main()
