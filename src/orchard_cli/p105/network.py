"""Own only the selected USB link's address; use an existing owner NFS export."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Dict

from orchard_cli.state import BootState, private_directory
from . import usb
from .boot import save

HOST_MAC = '02:10:5a:05:00:01'
HOST = '10.55.0.1'


def interfaces(selected: str = '') -> Dict[str, str]:
    if sys.platform == 'darwin':
        text = subprocess.check_output(['/sbin/ifconfig', '-a'], text=True, timeout=10)
        result = {}
        for block in re.split(r'(?=^[A-Za-z0-9][A-Za-z0-9._-]*:)', text, flags=re.M):
            name = block.split(':', 1)[0]
            if (selected and name == selected) or (not selected and re.search(r'\bether '+HOST_MAC+r'\b', block, re.I)):
                result[name] = block
        return result
    if sys.platform == 'linux':
        rows = json.loads(subprocess.check_output(['ip', '-j', 'address'], text=True, timeout=10))
        return {r['ifname']: json.dumps(r) for r in rows if (selected and r['ifname'] == selected) or
                (not selected and r.get('address', '').lower() == HOST_MAC)}
    raise ValueError('P105 USB networking supports macOS and Linux hosts')


def privileged(argv: list) -> None:
    if os.geteuid() == 0:
        subprocess.run(argv, check=True, timeout=20)
    elif sys.platform == 'darwin':
        script = 'do shell script '+json.dumps(shlex.join(argv))+' with administrator privileges'
        subprocess.run(['/usr/bin/osascript', '-e', script], check=True, timeout=120)
    else:
        subprocess.run(['sudo', '--', *argv], check=True, timeout=120)


def address_present(text: str) -> bool:
    return bool(re.search(r'(?<![0-9.])10\.55\.0\.1(?![0-9.])', text))


def ensure(scope: BootState) -> Path:
    deadline = time.monotonic()+15
    while True:
        endpoint = usb.recovery()
        if not scope.matches(endpoint):
            raise ValueError('USB identity changed before network setup')
        selected = endpoint.get('network_interface', '')
        if selected and endpoint.get('network_mac') != HOST_MAC:
            raise ValueError('Selected P105 USB network address differs from the gadget recipe')
        if selected and not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,31}', selected):
            raise ValueError('Unexpected selected USB interface name')
        rows = interfaces(selected) if selected else {}
        if len(rows) == 1:
            break
        if len(rows) > 1 or time.monotonic() >= deadline:
            raise ValueError('Expected exactly one P105 USB network interface')
        time.sleep(0.25)
    name, text = next(iter(rows.items()))
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,31}', name):
        raise ValueError('Unexpected USB interface name')
    path = private_directory(scope.root)/'network.json'
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get('phase') == 'address-outcome-unknown':
            raise ValueError('USB address outcome is unknown; inspect its saved record before another setup')
        if previous.get('changed'):
            if previous['interface'] != name or not address_present(text):
                raise ValueError('Owned USB network state changed; inspect its saved record')
            return path
    record: Dict[str, Any] = {'profile': 'p105', 'ecid': scope.device.ecid, 'boot_id': scope.boot_id,
                             'interface': name, 'mac': HOST_MAC, 'changed': False}
    if not address_present(text):
        record['phase'] = 'address-outcome-unknown'
        save(path, record)
        command = (['/sbin/ifconfig', name, 'inet', HOST, 'netmask', '255.255.255.0', 'alias']
                   if sys.platform == 'darwin' else ['ip', 'address', 'add', HOST+'/24', 'dev', name])
        privileged(command)
        current = usb.recovery()
        if (not scope.matches(current) or current.get('network_interface') != name or
                not address_present(interfaces(name).get(name, ''))):
            raise ValueError('Selected USB address setup could not be verified')
        record['changed'] = True
    record['phase'] = 'ready'
    save(path, record)
    return path


def nfs_root(value: str) -> str:
    if not re.fullmatch(r'10\.55\.0\.1:/[A-Za-z0-9._/-]+', value) or '/..' in value or value.endswith('/'):
        raise ValueError('Expected the host USB export as 10.55.0.1:/absolute/export/path')
    return value


def check_export(value: str) -> None:
    path = nfs_root(value).split(':', 1)[1]
    result = subprocess.run(['showmount', '-e', HOST], capture_output=True, text=True, timeout=15)
    if result.returncode or not any(line.split() and line.split()[0] == path for line in result.stdout.splitlines()):
        raise ValueError('The requested NFS root is absent from the existing host export')


def cleanup(path: Path) -> Dict[str, Any]:
    from .config import regular
    record = json.loads(regular(path, private=True).read_text())
    if record.get('profile') != 'p105' or record.get('mac') != HOST_MAC or record.get('phase') != 'ready':
        raise ValueError('Expected a completed P105 network ownership record')
    name = record['interface']
    if record['changed']:
        if any(usb.product_matches(row.get('product')) for row in usb.apple_usb.devices()):
            raise ValueError('Shut down the P105 Linux session before removing its USB address')
        rows = interfaces()
        if name not in rows and not interfaces(name):
            save(path, {**record, 'phase': 'cleaned', 'changed': False})
            return {'cleaned': True, 'usb_link': 'retired', 'nfs_service': 'existing owner service retained'}
        if name not in rows or not address_present(rows[name]):
            raise ValueError('Owned network address changed; cleanup stopped')
        command = (['/sbin/ifconfig', name, 'inet', HOST, '-alias'] if sys.platform == 'darwin' else
                   ['ip', 'address', 'delete', HOST+'/24', 'dev', name])
        privileged(command)
    save(path, {**record, 'phase': 'cleaned', 'changed': False})
    return {'cleaned': True, 'nfs_service': 'existing owner service retained'}
