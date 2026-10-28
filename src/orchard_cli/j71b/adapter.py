"""J71b identity/recipe policy binding the shared Apple boot and transport code."""
from contextlib import AbstractContextManager
from typing import Any, Dict, List, Optional, cast

from orchard_cli.contracts import BootAssets, BootRequest, Connection, SelectedDevice
from orchard_cli.state import BootState, DeviceState, EndpointUnavailable
from orchard_cli.progress import Progress
from orchard_cli.apple import usb, boot as apple_boot, adt_capture
from orchard_hardware.apple.adt import validate as validate_adt
from orchard_cli.apple.adt_tree import parse
from orchard_cli.transports import ssh
from functools import partial
from orchard_cli import firmware
from .config import PROFILE, NAME, PRODUCT, CHIP, BOARD, APPLE_PRODUCT

firmware_inputs = partial(firmware.ensure, PROFILE)
prepare_mixin = partial(firmware.mixin, PROFILE)

REBOOT_MESSAGE = 'A fresh recovery boot is required.\nPut the iPad in DFU mode, then run: orchard install'


def recovery_endpoint() -> Dict[str, Any]:
    return usb.recovery(PRODUCT)


def connect(device: SelectedDevice) -> AbstractContextManager[Connection]:
    return ssh.session(DeviceState(PROFILE, device['ecid']), recovery_endpoint,
                       expected_boot=device.get('boot_id'))


def connect_saved(device: SelectedDevice) -> AbstractContextManager[Connection]:
    if device['mode'] != 'linux' or not device.get('boot_id'):
        raise ValueError('Diagnostic access requires the selected running Linux boot')
    return ssh.saved_session(DeviceState(PROFILE, device['ecid']), recovery_endpoint,
                             expected_boot=device['boot_id'])


def forget_access(scope: BootState) -> None:
    if scope.device.profile != PROFILE:
        raise ValueError('Credential scope differs from adapter profile')
    return ssh.forget_access(scope)


def read_adt() -> bytes:
    data = adt_capture.capture(recovery_endpoint)
    if parse(data).properties.get('model', b'').rstrip(b'\0') != APPLE_PRODUCT.encode():
        raise ValueError('Captured ADT is not for ' + APPLE_PRODUCT)
    return data


def capture_adt(device: Optional[SelectedDevice] = None) -> bytes:
    # DFU/Pongo and Linux location IDs differ: bind the fresh ADT to the ECID.
    data = read_adt()
    if device is not None and validate_adt(data) != device['ecid']:
        raise ValueError('Fresh ADT identity differs from the confirmed device')
    return data


def list_devices() -> List[Dict[str, Any]]:
    return usb.devices()


def discover() -> SelectedDevice:
    try:
        device = usb.sole_target()
    except EndpointUnavailable as error:
        raise ValueError(str(error)) from error
    if device['mode'] == 'linux':
        endpoint = recovery_endpoint()
        BootState(DeviceState(PROFILE, endpoint['ecid']), endpoint['boot_id'])
        return cast(SelectedDevice, {**endpoint, 'resumed': True})
    if (device['chip'], device['board']) != (CHIP, BOARD) or not device['ecid']:
        raise ValueError('Unsupported device; only iPad 6 (Wi-Fi), J71b is supported')
    DeviceState(PROFILE, device['ecid'])
    return cast(SelectedDevice, {**device, 'resumed': False})


def boot(device: SelectedDevice, assets: BootAssets,
         manifest: BootRequest, progress: Progress) -> None:
    return apple_boot.boot(device, assets, manifest, progress,
                           profile=PROFILE, chip=CHIP, board=BOARD, product=PRODUCT)
