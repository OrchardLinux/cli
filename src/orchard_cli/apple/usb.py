"""Apple USB discovery and exact device/port selection, without touching devices."""
import plistlib
import re
import subprocess
import sys
import uuid

from .linux_usb import devices as linux_devices
from orchard_cli.state import EndpointUnavailable

MODES = {0x1227: 'dfu', 0x4141: 'pongo', 0x4142: 'linux'}


class DeviceError(ValueError):
    pass


def devices():
    if sys.platform == 'linux':
        return linux_devices()
    if sys.platform != 'darwin':
        raise DeviceError('Unsupported USB host platform')
    raw = subprocess.run(['/usr/sbin/ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice'],
                         check=True, capture_output=True, timeout=10).stdout
    pending = list(plistlib.loads(raw) if raw.strip() else [])
    found = []
    while pending:
        node = pending.pop()
        pending.extend(node.get('IORegistryEntryChildren', []))
        if node.get('IOObjectClass') != 'IOUSBHostDevice' or node.get('idVendor') != 0x05ac:
            continue
        location = node.get('locationID')
        if not isinstance(location, int):
            continue
        serial = node.get('USB Serial Number') or ''
        boot = re.search(r'\bBOOT:([0-9a-fA-F-]{36})\b', serial)
        fields = [re.search(r'\b' + label + r':([0-9a-fA-F]+)\b', serial)
                  for label in ('ECID', 'CPID', 'BDID')]
        descendants = list(node.get('IORegistryEntryChildren', []))
        ports = []
        while descendants:
            child = descendants.pop()
            descendants.extend(child.get('IORegistryEntryChildren', []))
            if child.get('IOObjectClass') == 'IOSerialBSDClient':
                port = child.get('IOCalloutDevice')
                if isinstance(port, str) and port.startswith('/dev/cu.'):
                    ports.append(port)
        if len(ports) > 1:
            raise DeviceError('Ambiguous USB serial ports on the selected device')
        found.append({'location': location, 'mode': MODES.get(node.get('idProduct'), 'other'),
                      'ecid': int(fields[0].group(1), 16) if fields[0] else None,
                      'boot_id': str(uuid.UUID(boot.group(1))) if boot else None,
                      'chip': int(fields[1].group(1), 16) if fields[1] else None,
                      'board': int(fields[2].group(1), 16) if fields[2] else None,
                      'product': node.get('USB Product Name'),
                      'serial_port': ports[0] if ports else None})
    return found


def sole_target():
    targets = [item for item in devices() if item['mode'] in ('dfu', 'pongo', 'linux')]
    if not targets:
        raise EndpointUnavailable('Selected USB target is not connected')
    if len(targets) != 1:
        raise ValueError('Connect exactly one target in DFU, Pongo or Linux; device selection is not supported')
    return targets[0]


def recovery(product):
    device = sole_target()
    if device['mode'] != 'linux' or device.get('product') != product or not device.get('serial_port'):
        raise ValueError('Expected one recovery USB serial endpoint')
    if not device.get('ecid') or not device.get('boot_id'):
        raise ValueError('Recovery lacks device/boot identity; load the current recovery from DFU')
    return device


def selected(location, ecid, *, chip, board, mode='pongo'):
    device = sole_target()
    if device['location'] != location:
        raise DeviceError('Device moved from the selected USB port')
    if device['mode'] != mode:
        hint = ''
        if (mode, device['mode']) == ('dfu', 'pongo'):
            hint = ('. Another palera1n/checkra1n may have exploited it; stop any running, '
                    'force-restart the device and enter DFU again')
        raise DeviceError(f'Expected {mode} at the selected port; found {device["mode"]}{hint}')
    if mode in ('dfu', 'pongo') and (device['ecid'], device['chip'], device['board']) != (ecid, chip, board):
        raise DeviceError('Wrong device identity at the selected port')
    return device
