"""Shared checkm8/Pongo boot sequence; adapters supply device identity constraints."""
import os
import signal
import subprocess
import shutil
import time
import tempfile
import threading

from orchard_cli.state import DeviceState, private_directory
from orchard_cli.transports import pongo
from . import usb


def wait_mode(device, mode, *, chip, board, product, seconds=30):
    deadline = time.monotonic() + seconds
    observed = []
    probe_error = ''
    while True:
        try:
            observed = usb.devices()
            active = [d for d in observed if d['mode'] in ('dfu', 'pongo', 'linux')]
            if len(active) > 1:
                raise ValueError('Connect exactly one target; no boot retry was performed')
            if mode == 'linux':
                matches = [d for d in observed if d['mode'] == 'linux' and
                           d.get('product') == product and d.get('serial_port')]
            else:
                matches = [d for d in observed if d['location'] == device['location']]
            if len(matches) == 1 and matches[0]['mode'] == mode:
                if mode == 'pongo':
                    usb.selected(device['location'], device['ecid'], chip=chip, board=board)
                elif mode == 'linux':
                    if matches[0].get('ecid') != device['ecid'] or not matches[0].get('boot_id'):
                        raise ValueError('Recovery device identity differs or lacks boot identity; use the current recovery')
                return matches[0]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # Only USB enumeration is retried, within the existing deadline.
            # Boot/upload live outside this loop and are never replayed.
            detail = error.stderr or b''
            if isinstance(detail, bytes):
                detail = detail.decode(errors='replace')
            probe_error = f' Last USB enumeration error: {error}. {detail[-1000:]}'
        if time.monotonic() >= deadline:
            states = ', '.join(f'{d["mode"]} ({d.get("product") or "unnamed"})' for d in observed) or 'no Apple USB devices'
            raise RuntimeError(f'{mode} readiness timed out; observed: {states}.{probe_error} '
                               'Inspect USB state; no boot retry was performed.')
        time.sleep(0.25)


def _exit_on_sigterm(signum, frame):
    raise SystemExit(128 + signum)


def run_process_group(argv, *, timeout, **kwargs):
    """subprocess.run(check=True) that also stops the command's children.

    palera1n posix_spawns checkra1n and only waits for it. Killing palera1n
    alone leaves checkra1n running, and it exploits the next DFU device it
    sees, so give up on the whole process group instead.
    """
    # A plain SIGTERM (pkill, logout) would end Orchard without unwinding;
    # turn it into SystemExit while the group runs so it is cleaned up too.
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        with subprocess.Popen(argv, start_new_session=True, **kwargs) as process:
            try:
                returncode = process.wait(timeout=timeout)
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                raise
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    if returncode:
        raise subprocess.CalledProcessError(returncode, argv)


def boot(device, assets, manifest, progress, *, profile, chip, board, product):
    if device['mode'] == 'linux':
        progress.phase('Using running recovery; no reboot or upload (timing starts at resume)')
        return
    if device['mode'] == 'dfu':
        executable = shutil.which('palera1n')
        if not executable:
            raise ValueError('palera1n is required on the host to boot Pongo')
        usb.selected(device['location'], device['ecid'], chip=chip, board=board, mode='dfu')
        progress.phase('Booting Pongo')
        progress.phase('Wait for the Apple logo to appear. If it does not disappear after a few seconds, '
                       'disconnect and reconnect the cable at the iPad once.')
        logs = private_directory(DeviceState(profile, device['ecid']).root / 'logs')
        with tempfile.NamedTemporaryFile(prefix='palera1n-', suffix='.log', dir=logs,
                                         delete=False) as log:
            try:
                run_process_group([executable, '-p', '-k', str(assets['pongo'])],
                                  stdout=log, stderr=subprocess.STDOUT, timeout=120)
            except (OSError, subprocess.SubprocessError) as error:
                raise RuntimeError('Pongo bootstrap failed or outcome unknown; inspect USB before any retry. '
                                   f'Log: {log.name}') from error
        wait_mode(device, 'pongo', chip=chip, board=board, product=product)
    progress.phase('Loading recovery')
    usb.selected(device['location'], device['ecid'], chip=chip, board=board)
    progress.reset_transfer()
    pongo.upload(assets['recovery'], device['location'], manifest['assets']['recovery']['sha256'], progress.transfer)
    progress.phase('Waiting for recovery USB')
    wait_mode(device, 'linux', chip=chip, board=board, product=product, seconds=120)
