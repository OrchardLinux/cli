"""Device confirmation and tethered boot of locally assembled RAM payloads.

Uses the existing USB/Pongo transport; no installer or device-side RPC required.
"""
from pathlib import Path
import shutil
import subprocess
import sys

from .ram import verify_ram


def confirm_boot():
    if not sys.stdin.isatty():
        raise ValueError('Boot confirmation requires an interactive terminal')
    try:
        answer = input('Boot this device? [Y/n] ').strip().lower()
    except EOFError:
        raise ValueError('Boot cancelled: input closed') from None
    if answer not in ('', 'y', 'yes'):
        raise ValueError('Boot cancelled')


def boot_ram(output):
    """Boot once via existing USB/Pongo transport. No recovery API required."""
    from orchard_hardware import device as hardware_device
    from .apple import usb
    from .apple.boot import wait_mode
    from .state import operation_lock
    from .transports import pongo

    output = Path(output).resolve()
    receipt = verify_ram(output)
    description = hardware_device(receipt['device'])
    chip, board = (description['match']['dfu'][name] for name in ('chip', 'board'))
    with operation_lock():
        device = usb.sole_target()
        if device['mode'] not in ('dfu', 'pongo') or (device.get('chip'), device.get('board')) != (chip, board):
            raise ValueError('Requires the matching device in DFU/Pongo; will not reboot running Linux')
        ecid = device.get('ecid')
        if not ecid:
            raise ValueError('Missing device ECID')
        executable = shutil.which('palera1n')
        if device['mode'] == 'dfu' and not executable:
            raise ValueError('Install palera1n before booting')
        print(f'{description["device"]["name"]}, ECID {ecid:x}, USB {device["location"]}, mode {device["mode"]}.')
        print("Experimental RAM boot; the payload's /init determines what runs.")
        confirm_boot()
        verify_ram(output)
        usb.selected(device['location'], ecid, chip=chip, board=board, mode=device['mode'])
        if device['mode'] == 'dfu':
            print('Starting Pongo once. Inspect any uncertain failure before retrying.')
            with (output / 'palera1n.log').open('wb') as log:
                subprocess.run([executable, '-p', '-k', str(output / 'pongo.bin')],
                               check=True, timeout=120, stdout=log, stderr=subprocess.STDOUT)
            wait_mode(device, 'pongo', chip=chip, board=board, product='', seconds=60)
        usb.selected(device['location'], ecid, chip=chip, board=board)
        pongo.upload(output / 'linux.bin', device['location'], receipt['files']['linux.bin'])
    print('Upload/boot request sent; Linux readiness has NOT been verified.')
    print('Connect to its USB serial port: screen /dev/cu.usbmodemYOUR_PORT 115200 (macOS)')
    print('or screen /dev/ttyACM0 115200 (Linux; verify the actual port).')


def run_directory(directory):
    """Friendly error boundary around boot_ram; never retries a boot request."""
    try:
        boot_ram(Path(directory).expanduser().resolve() / 'out')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        raise SystemExit(f'{error}\nStopped. Inspect device state before any new boot attempt.')
