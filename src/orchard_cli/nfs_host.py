"""Manage the selected experimental NFS gadget's host-side IPv4 alias."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import uuid

from orchard_hardware.boot.initramfs.nfs.stage import identity


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Expected a canonical UUID') from None
    return value


def root_identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular NFS root identity')
    raw = path.read_bytes()
    record, _ = identity(raw)
    if record['profile'] != 'n61' or record['architecture'] != 'aarch64':
        raise ValueError('Expected the N61 AArch64 NFS root identity')
    return record, hashlib.sha256(raw).hexdigest()


def _usb_devices(raw):
    if not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024:
        raise ValueError('Invalid USB registry data')
    pending = plistlib.loads(raw)
    if not isinstance(pending, list):
        raise ValueError('Invalid USB registry data')
    pending, found, visited = list(pending), [], 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited > 100000 or not isinstance(node, dict):
            raise ValueError('Invalid USB registry tree')
        children = node.get('IORegistryEntryChildren', [])
        if not isinstance(children, list):
            raise ValueError('Invalid USB registry children')
        pending.extend(children)
        if node.get('IOObjectClass') == 'IOUSBHostDevice' and node.get('idVendor') == 0x05ac:
            found.append(node)
    return found


def select_macos(raw, location, root_id, *, expected_boot=None):
    """Bind one experimental gadget to its selected USB location and root."""
    if type(location) is not int or not 0 < location < 2**32:
        raise ValueError('Invalid selected USB location')
    _uuid(root_id)
    if expected_boot is not None:
        _uuid(expected_boot)
    devices = _usb_devices(raw)
    active = [node for node in devices if node.get('idProduct') in (0x1227, 0x4141, 0x4142, 0x4144)]
    if len(active) != 1 or active[0].get('locationID') != location:
        raise ValueError('Expected exactly one boot device at the selected USB location')
    node = active[0]
    if node.get('idProduct') != 0x4144 or node.get('USB Product Name') != 'Orchard n61 NFS':
        raise ValueError('The selected USB device is not the N61 NFS gadget')
    serial = node.get('USB Serial Number') or ''
    match = re.fullmatch(r'PROFILE:n61 ROOT:([0-9a-f-]{36}) BOOT:([0-9a-f-]{36})', serial)
    if not match or _uuid(match[1]) != root_id:
        raise ValueError('The selected NFS root identity differs')
    boot = _uuid(match[2])
    if expected_boot is not None and boot != expected_boot:
        raise ValueError('The selected NFS boot identity differs')
    pending = list(node.get('IORegistryEntryChildren', []))
    ports, interfaces, macs = [], [], []
    while pending:
        child = pending.pop()
        if child.get('IOObjectClass') == 'IOUSBHostDevice':
            continue
        pending.extend(child.get('IORegistryEntryChildren', []))
        if child.get('IOObjectClass') == 'IOSerialBSDClient':
            ports.append(child.get('IOCalloutDevice'))
        if child.get('IOObjectClass') in ('IOEthernetInterface', 'IOSkywalkLegacyEthernetInterface'):
            interfaces.append(child.get('BSD Name'))
        if 'IOMACAddress' in child:
            macs.append(child['IOMACAddress'])
    if (len(ports) != 1 or not isinstance(ports[0], str) or
            not re.fullmatch(r'/dev/cu\.[^/\x00\r\n]+', ports[0])):
        raise ValueError('Expected exactly one NFS USB serial endpoint')
    if (len(interfaces) != 1 or not isinstance(interfaces[0], str) or
            not re.fullmatch(r'en[0-9]+', interfaces[0])):
        raise ValueError('Expected exactly one NFS USB network interface')
    expected_mac = bytes.fromhex('02000a3d0001')
    if not macs or any(value != expected_mac for value in macs):
        raise ValueError('The selected NFS network hardware address differs')
    return {'location': location, 'profile': 'n61', 'root_id': root_id, 'boot_id': boot,
            'serial_port': ports[0], 'interface': interfaces[0], 'mac': '02:00:0a:3d:00:01'}


def interfaces(text):
    result, current = {}, None
    for line in text.splitlines():
        header = re.match(r'^([A-Za-z][A-Za-z0-9._-]{0,31}):', line)
        if header:
            current = result.setdefault(header[1], {'mac': None, 'addresses': []})
            continue
        if current is None:
            continue
        mac = re.match(r'\s+ether ([0-9a-fA-F:]{17})(?:\s|$)', line)
        address = re.match(r'\s+inet ([0-9.]+) netmask (0x[0-9a-fA-F]+|[0-9.]+)(?:\s|$)', line)
        if mac:
            if current['mac'] is not None:
                raise ValueError('Duplicate interface hardware address')
            current['mac'] = mac[1].lower()
        if address:
            mask = str(ipaddress.IPv4Address(int(address[2], 16))) if address[2].startswith('0x') else address[2]
            current['addresses'].append(ipaddress.IPv4Interface(address[1] + '/' + mask))
    return result


def routes(text):
    result, netif = [], None
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == 'Destination' and 'Netif' in fields:
            netif = fields.index('Netif')
            continue
        if not fields or fields[0] == 'default' or not re.match(r'^[0-9]', fields[0]):
            continue
        if netif is None or len(fields) <= netif:
            raise ValueError('Unrecognized IPv4 route table')
        address, slash, prefix = fields[0].partition('/')
        octets = address.split('.')
        if not 1 <= len(octets) <= 4:
            raise ValueError('Unrecognized IPv4 route destination')
        width = int(prefix) if slash else len(octets) * 8
        address = '.'.join(octets + ['0'] * (4 - len(octets)))
        result.append((ipaddress.IPv4Network(address + '/' + str(width), strict=False), fields[netif]))
    if netif is None:
        raise ValueError('The IPv4 route header is absent')
    return result


def plan(endpoint, configuration, route_table, owner=None):
    device = endpoint['interface']
    target = ipaddress.IPv4Interface('10.61.0.1/30')
    available = interfaces(configuration)
    if device not in available or available[device]['mac'] not in (endpoint['mac'], '02:00:00:00:00:00'):
        raise ValueError('The selected USB interface differs')
    matches = [key for key, value in available.items() if value['mac'] == endpoint['mac']]
    if matches not in ([], [device]):
        raise ValueError('The USB network hardware address is ambiguous')
    present = False
    for key, value in available.items():
        for address in value['addresses']:
            if address.network.overlaps(target.network):
                if key != device or address != target:
                    raise ValueError('An existing IPv4 address conflicts with the N61 network')
                present = True
    if owner is not None:
        for key in ('location', 'root_id', 'boot_id', 'interface', 'mac'):
            if owner.get(key) != endpoint[key]:
                raise ValueError('The saved host link belongs to another session')
        if (owner.get('kind'), owner.get('state'), owner.get('address')) != (
                'orchard.n61-nfs-host-link', 'owned', str(target)):
            raise ValueError('The saved host link is not a confirmed ownership receipt')
    if present and owner is None:
        raise ValueError('The N61 address exists without an ownership receipt')
    for network, key in routes(route_table):
        if network.prefixlen == 0 or not network.overlaps(target.network):
            continue
        if present and owner is not None and (
                (key == device and network.subnet_of(target.network)) or
                (key == 'lo0' and network == ipaddress.IPv4Network('10.61.0.1/32'))):
            continue
        raise ValueError('An existing IPv4 route conflicts with the N61 network')
    return {'action': 'already-owned' if present else 'add-alias', 'endpoint': endpoint,
            'address': str(target), 'command': ['/usr/bin/sudo', '-n', '/sbin/ifconfig', device,
                                               'inet', '10.61.0.1', 'netmask', '255.255.255.252', 'alias']}


def inspect(root_path, location, *, expected_boot=None, state=None):
    if sys.platform != 'darwin':
        raise ValueError('N61 NFS host link currently supports macOS')
    root, root_sha = root_identity(root_path)
    raw = subprocess.run(['/usr/sbin/ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice'],
                         check=True, capture_output=True, timeout=10).stdout
    endpoint = select_macos(raw, location, root['root_id'], expected_boot=expected_boot)
    if not stat.S_ISCHR(Path(endpoint['serial_port']).stat().st_mode):
        raise ValueError('The selected NFS serial endpoint is not a character device')
    config = subprocess.run(['/sbin/ifconfig', '-a'], check=True, capture_output=True, text=True, timeout=10).stdout
    table = subprocess.run(['/usr/sbin/netstat', '-rn', '-f', 'inet'],
                           check=True, capture_output=True, text=True, timeout=10).stdout
    owner = None
    if state is not None and (Path(state).exists() or Path(state).is_symlink()):
        path = Path(state)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError('Expected a private regular host link receipt')
        owner = json.loads(path.read_text())
        if owner.get('root_identity_sha256') != root_sha:
            raise ValueError('The saved root identity differs')
    return {**plan(endpoint, config, table, owner), 'root_identity_sha256': root_sha}


def _write_new(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream, sort_keys=True, indent=2)
        stream.write('\n')


def apply(root_path, location, boot_id, state):
    """Add one alias after two checks; preserve an uncertain intent on failure."""
    _uuid(boot_id)
    path = Path(state)
    first = inspect(root_path, location, expected_boot=boot_id, state=path)
    if first['action'] == 'already-owned':
        return first
    if path.exists() or path.is_symlink():
        raise ValueError('Choose a new private host link receipt')
    subprocess.run(['/usr/bin/sudo', '-v'], check=True)
    if inspect(root_path, location, expected_boot=boot_id, state=path) != first:
        raise ValueError('The USB link changed during authorization')
    record = {'schema': 1, 'kind': 'orchard.n61-nfs-host-link', 'state': 'adding',
              'address': first['address'], 'root_identity_sha256': first['root_identity_sha256'],
              **first['endpoint']}
    _write_new(path, record)
    try:
        if inspect(root_path, location, expected_boot=boot_id) != first:
            raise ValueError('The USB link changed before address setup')
        subprocess.run(first['command'], check=True, timeout=15)
        record['state'] = 'owned'
        # Verify with the candidate ownership record before committing it.
        config = subprocess.run(['/sbin/ifconfig', '-a'], check=True, capture_output=True, text=True, timeout=10).stdout
        table = subprocess.run(['/usr/sbin/netstat', '-rn', '-f', 'inet'], check=True,
                               capture_output=True, text=True, timeout=10).stdout
        raw = subprocess.run(['/usr/sbin/ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice'],
                             check=True, capture_output=True, timeout=10).stdout
        endpoint = select_macos(raw, location, record['root_id'], expected_boot=boot_id)
        root, root_sha = root_identity(root_path)
        if (root['root_id'] != record['root_id'] or root_sha != record['root_identity_sha256'] or
                endpoint != first['endpoint'] or plan(endpoint, config, table, record)['action'] != 'already-owned'):
            raise ValueError('The host address could not be verified')
        temporary = path.with_name('.' + path.name + '.' + str(uuid.uuid4()))
        _write_new(temporary, record)
        os.replace(temporary, path)
    except BaseException:
        # The original adding receipt requires inspection before another attempt.
        raise
    return {**first, 'action': 'added', 'ownership_receipt': str(path)}


def _has_nfs_gadget(raw):
    return any(node.get('idProduct') == 0x4144 for node in _usb_devices(raw))


def _replace(path, record):
    temporary = path.with_name('.' + path.name + '.' + str(uuid.uuid4()))
    _write_new(temporary, record)
    os.replace(temporary, path)


def cleanup(root_path, state):
    """Retire only an owned alias after the experimental gadget disconnects."""
    if sys.platform != 'darwin':
        raise ValueError('N61 NFS host link currently supports macOS')
    path = Path(state)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Expected a private regular host link receipt')
    record = json.loads(path.read_text())
    root, root_sha = root_identity(root_path)
    if (record.get('schema'), record.get('kind'), record.get('state'), record.get('profile'),
            record.get('root_id'), record.get('root_identity_sha256'), record.get('address'), record.get('mac')) != (
            1, 'orchard.n61-nfs-host-link', 'owned', 'n61', root['root_id'], root_sha,
            '10.61.0.1/30', '02:00:0a:3d:00:01'):
        raise ValueError('The host link receipt is not an owned N61 session')
    _uuid(record.get('boot_id'))
    interface = record.get('interface')
    if not isinstance(interface, str) or not re.fullmatch(r'en[0-9]+', interface):
        raise ValueError('Invalid saved network interface')

    def inspect_retired():
        raw = subprocess.run(['/usr/sbin/ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice'],
                             check=True, capture_output=True, timeout=10).stdout
        if _has_nfs_gadget(raw):
            raise ValueError('Disconnect the NFS gadget before retiring its host address')
        config = subprocess.run(['/sbin/ifconfig', '-a'], check=True, capture_output=True, text=True, timeout=10).stdout
        row = interfaces(config).get(interface)
        if row is None or ipaddress.IPv4Interface('10.61.0.1/30') not in row['addresses']:
            return False
        if row['mac'] != record['mac']:
            raise ValueError('The saved interface no longer has its verified hardware address')
        return True

    if inspect_retired():
        subprocess.run(['/usr/bin/sudo', '-v'], check=True)
        if not inspect_retired():
            raise ValueError('The host link changed during authorization')
        removing = {**record, 'state': 'removing'}
        _replace(path, removing)
        subprocess.run(['/usr/bin/sudo', '-n', '/sbin/ifconfig', interface, 'inet', '10.61.0.1', '-alias'],
                       check=True, timeout=15)
        if inspect_retired():
            raise ValueError('The owned host address is still present')
    _replace(path, {**record, 'state': 'retired'})
    return {'action': 'retired', 'ownership_receipt': str(path)}
