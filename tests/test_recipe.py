"""Firmware recipe steps: vocabulary check, hash verification and containers."""
import hashlib
import unittest

from orchard_cli import recipe


def sha(data):
    return hashlib.sha256(data).hexdigest()


def der(tag, value):
    length = len(value)
    if length < 0x80:
        return bytes([tag, length]) + value
    size = (length.bit_length() + 7) // 8
    return bytes([tag, 0x80 | size]) + length.to_bytes(size, 'big') + value


def sample(steps, data=b'firmware'):
    return {
        'schema': 2, 'device': 'iPad7,5',
        'ipsw': {'version': '17.7.11', 'build': '21H461', 'bytes': 1, 'sha256': '0' * 64,
                 'url': 'https://updates.cdn-apple.com/x.ipsw'},
        'inputs': {'blob': {'ipsw': {'rootfs': 'usr/share/firmware/blob'}, 'sha256': sha(data)}},
        'outputs': [{'destination': 'lib/firmware/apple/blob', 'sha256': sha(data), 'steps': steps}],
    }


class RecipeTests(unittest.TestCase):
    def test_unknown_step_asks_for_an_update_before_any_work(self):
        with self.assertRaisesRegex(recipe.UnsupportedRecipe, 'update Orchard'):
            recipe.check(sample([{'step': 'future-format@1', 'input': 'blob'}]))

    def test_wrong_parameters_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Wrong parameters'):
            recipe.check(sample([{'step': 'copy@1', 'input': 'blob', 'profile': 'x'}]))

    def test_inputs_and_outputs_are_hash_checked(self):
        document = recipe.check(sample([{'step': 'copy@1', 'input': 'blob'}]))
        self.assertEqual(recipe.run(document, lambda name: b'firmware'),
                         {'lib/firmware/apple/blob': b'firmware'})
        with self.assertRaisesRegex(ValueError, 'input differs'):
            recipe.run(document, lambda name: b'tampered')
        document['inputs']['blob'].pop('sha256')
        document['outputs'][0]['sha256'] = '1' * 64
        with self.assertRaisesRegex(ValueError, 'output differs'):
            recipe.run(document, lambda name: b'firmware')

    def test_needed_inputs_are_only_those_of_the_requested_outputs(self):
        document = recipe.check(sample([{'step': 'copy@1', 'input': 'blob'}]))
        self.assertEqual(recipe.needed_inputs(document), ['blob'])
        self.assertEqual(recipe.needed_inputs(document, []), [])

    def test_later_steps_take_the_previous_result(self):
        container = der(0x30, der(0x16, b'IM4P') + der(0x16, b'mtfw') + der(0x16, b'touch')
                        + der(0x04, b'firmware'))
        document = sample([{'step': 'im4p-payload@1', 'input': 'blob'}, {'step': 'copy@1'}], container)
        document['outputs'][0]['sha256'] = sha(b'firmware')
        recipe.check(document)
        self.assertEqual(recipe.run(document, lambda name: container),
                         {'lib/firmware/apple/blob': b'firmware'})
        document['outputs'][0]['steps'][1]['input'] = 'blob'
        with self.assertRaisesRegex(ValueError, 'Only the first step'):
            recipe.supported(document)

    def test_im4p_payload(self):
        payload = b'\x7fELF' + bytes(300)
        container = der(0x30, der(0x16, b'IM4P') + der(0x16, b'mtfw') + der(0x16, b'touch')
                        + der(0x04, payload))
        self.assertEqual(recipe.im4p_payload(container), payload)
        compressed = der(0x30, der(0x16, b'IM4P') + der(0x16, b'mtfw') + der(0x16, b'touch')
                         + der(0x04, b'bvx2' + bytes(8)))
        with self.assertRaisesRegex(ValueError, 'Compressed'):
            recipe.im4p_payload(compressed)
        with self.assertRaisesRegex(ValueError, 'Not an IM4P'):
            recipe.im4p_payload(der(0x30, der(0x16, b'IMG4') + der(0x16, b'a') + der(0x16, b'b') + der(0x04, b'c')))


if __name__ == '__main__':
    unittest.main()
