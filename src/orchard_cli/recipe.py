"""Firmware recipes (schema 2): declarative steps from a fixed vocabulary.

Hardware describes inputs and steps (docs/firmware-recipes.md in hardware);
this module implements the steps. It does not know where inputs come from:
the host reads them from the IPSW, the in-app installer from iPadOS.
"""
import hashlib
import json
import plistlib

from orchard_cli.apple import ipsw, smartio, z2fw
from orchard_cli.apple.osserialize import apple_xml


def _copy(step, data, expected):
    return data


def _bluetool_hcd(step, data, expected):
    return ipsw.hcd_from_binary(data, expected)


def _smartio(step, data, expected):
    return smartio.extract(data)[0]


def _im4p_payload(step, data, expected):
    return im4p_payload(data)


def _z2fw(step, data, expected):
    profiles = plistlib.loads(data)
    profile = profiles.get(step['profile'])
    if not isinstance(profile, dict):
        raise ValueError('Touch profile not found: ' + str(step['profile']))
    output, _ = z2fw.convert(profile, apple_xml(profile['Firmware Config']),
                             packet=step['packet'], load_address=step['load_address'],
                             hbpp_version=step['hbpp_version'])
    return output


# step@version -> (implementation, parameters besides 'step' and 'input')
STEPS = {
    'copy@1': (_copy, set()),
    'bluetool-hcd@1': (_bluetool_hcd, set()),
    'smartio-from-kernelcache@1': (_smartio, set()),
    'im4p-payload@1': (_im4p_payload, set()),
    'z2fw-from-mtprops@1': (_z2fw, {'profile', 'packet', 'load_address', 'hbpp_version'}),
}


class UnsupportedRecipe(ValueError):
    """The recipe needs a step this Orchard version does not implement."""


def check(recipe):
    """Validate the recipe and confirm every step is implemented, before any work."""
    from orchard_cli.manifests import firmware_schema
    import jsonschema
    jsonschema.validate(recipe, firmware_schema(), cls=jsonschema.Draft202012Validator)
    for output in recipe['outputs']:
        for step in output['steps']:
            if step['step'] not in STEPS:
                raise UnsupportedRecipe(f"This firmware recipe needs step {step['step']}; "
                                        'update Orchard to a version that implements it')
            _, parameters = STEPS[step['step']]
            if set(step) - {'step', 'input'} != parameters:
                raise ValueError(f"Wrong parameters for {step['step']}: {sorted(set(step) - {'step', 'input'})}")
            if step['input'] not in recipe['inputs']:
                raise ValueError('Unknown recipe input: ' + step['input'])
    destinations = [output['destination'] for output in recipe['outputs']]
    if len(set(destinations)) != len(destinations):
        raise ValueError('Duplicate firmware destination')
    return recipe


def needed_inputs(recipe, outputs=None):
    outputs = recipe['outputs'] if outputs is None else outputs
    return sorted({step['input'] for output in outputs for step in output['steps']})


def run(recipe, read_input, outputs=None):
    """Produce outputs as {destination: bytes}; read_input(name) returns bytes.

    Every pinned input and every output is checked against its hash.
    """
    cache = {}

    def source(name):
        if name not in cache:
            data = read_input(name)
            pinned = recipe['inputs'][name].get('sha256')
            if pinned and hashlib.sha256(data).hexdigest() != pinned:
                raise ValueError('Firmware input differs from the recipe: ' + name)
            cache[name] = data
        return cache[name]

    result = {}
    for output in recipe['outputs'] if outputs is None else outputs:
        data = None
        for step in output['steps']:
            implementation, _ = STEPS[step['step']]
            data = implementation(step, source(step['input']) if data is None else data, output['sha256'])
        if hashlib.sha256(data).hexdigest() != output['sha256']:
            raise ValueError('Firmware output differs from the recipe: ' + output['destination'])
        result[output['destination']] = data
    return result


def im4p_payload(blob):
    """Payload of an IM4P container: SEQUENCE { "IM4P", type, description, OCTET STRING, ... }."""
    def item(pos):
        if pos + 2 > len(blob):
            raise ValueError('Truncated IM4P')
        tag, length = blob[pos], blob[pos + 1]
        pos += 2
        if length & 0x80:
            count = length & 0x7f
            if not 1 <= count <= 4 or pos + count > len(blob):
                raise ValueError('Invalid IM4P length')
            length = int.from_bytes(blob[pos:pos + count], 'big')
            pos += count
        if pos + length > len(blob):
            raise ValueError('Truncated IM4P')
        return tag, blob[pos:pos + length], pos + length

    tag, body, _ = item(0)
    if tag != 0x30:
        raise ValueError('IM4P is not a DER sequence')
    blob = body
    fields, pos = [], 0
    while pos < len(blob) and len(fields) < 4:
        tag, value, pos = item(pos)
        fields.append((tag, value))
    if len(fields) < 4 or fields[0] != (0x16, b'IM4P') or fields[3][0] != 0x04:
        raise ValueError('Not an IM4P container')
    payload = fields[3][1]
    if payload[:4] in (b'bvx2', b'bvx1', b'bvx-', b'bvxn'):
        raise ValueError('Compressed IM4P payloads are not supported by im4p-payload@1')
    return payload
