"""Read-only iOS and iBEC discovery alongside Orchard's identified USB gadget."""
import plistlib
import re
import subprocess
from typing import Any, Dict, List

from orchard_cli.apple import usb as apple_usb
from .config import BOARD, CHIP, PRODUCT, settings, tool


def product_matches(value: Any) -> bool:
    # macOS's USB Product Name replaces the descriptor's hyphen with an underscore.
    return value in (PRODUCT, PRODUCT.replace('-', '_'))


def query(*args: str) -> str:
    result = subprocess.run(list(args), capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError('Selected P105 USB query failed')
    return result.stdout.decode().strip()


def ios(udid: str) -> Dict[str, Any]:
    if not re.fullmatch('[0-9a-fA-F]{40}', udid):
        raise ValueError('Invalid iOS device identifier')
    data = plistlib.loads(query(tool('ideviceinfo'), '-u', udid, '-x').encode())
    if (data.get('ProductType') != 'iPad2,5' or str(data.get('HardwareModel', '')).lower() != 'p105ap' or
            str(data.get('UniqueDeviceID', '')).lower() != udid.lower()):
        raise ValueError('Selected device is not iPad2,5 / P105AP')
    ecid = data.get('UniqueChipID')
    if type(ecid) is not int or not 0 < ecid < 2**64:
        raise ValueError('Invalid iOS chip identity')
    return {'mode': 'ios', 'location': udid.lower(), 'udid': udid.lower(), 'ecid': ecid,
            'chip': CHIP, 'board': BOARD, 'product': 'iPad2,5',
            'ios_version': data.get('ProductVersion'), 'ios_build': data.get('BuildVersion')}


def verify_ios(device: Dict[str, Any]) -> None:
    current = ios(device['udid'])
    if current['ecid'] != device['ecid']:
        raise ValueError('iOS device identity changed')
    if (current['ios_version'], current['ios_build']) != ('9.3.5', '13G36'):
        raise ValueError('This P105 boot path requires iOS 9.3.5 build 13G36')


def ibec(ecid: int) -> Dict[str, Any]:
    text = query(tool('irecovery'), '-i', str(ecid), '-q')
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep and key in ('ECID', 'PRODUCT', 'MODE', 'CPID', 'BDID'):
            if key in fields:
                raise ValueError('Ambiguous recovery identity')
            fields[key] = value.strip()
    if (fields.get('PRODUCT'), fields.get('MODE', '').lower()) != ('iPad2,5', 'recovery'):
        raise ValueError('Expected the selected P105 in iBEC Recovery')
    if int(fields.get('ECID', '0'), 0) != ecid:
        raise ValueError('iBEC device identity changed')
    return {'mode': 'ibec', 'location': str(ecid), 'ecid': ecid, 'chip': CHIP, 'board': BOARD}


def recovery() -> Dict[str, Any]:
    matches = [row for row in apple_usb.devices() if row.get('mode') == 'linux' and product_matches(row.get('product'))]
    if len(matches) != 1:
        raise ValueError('Connect exactly one Orchard P105 recovery')
    row = matches[0]
    if not row.get('ecid') or not row.get('boot_id') or not row.get('serial_port'):
        raise ValueError('P105 recovery lacks its device and boot identity')
    return row


def observations() -> List[Dict[str, Any]]:
    observed = apple_usb.devices()
    result = [row for row in observed if product_matches(row.get('product'))]
    identifiers = [row['udid'] for row in observed if row.get('udid')]
    for identifier in identifiers:
        try:
            result.append(ios(identifier))
        except ValueError:
            continue
    if not result:
        try:
            result.append(ibec(settings()['target']['ecid']))
        except (ValueError, OSError):
            pass
    return result
