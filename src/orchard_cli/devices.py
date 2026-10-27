"""Device adapter registry. Shared workflow never interprets hardware identities."""
import importlib
from typing import Any, Dict, List, Tuple

from orchard_hardware import device as hardware_device
from .apple.adt_tree import parse
from .contracts import Adapter, SelectedDevice, UnsupportedDevice

SUPPORTED = ('j71b',)


def load(profile: str) -> Adapter:
    if profile not in SUPPORTED:
        raise ValueError('Unsupported device profile')
    adapter = importlib.import_module('orchard_cli.' + profile + '.adapter')
    if not isinstance(adapter, Adapter):
        raise ValueError('Incomplete adapter contract for ' + profile)
    if adapter.PROFILE != profile:
        raise ValueError('Adapter profile does not match registered profile: ' + profile)
    if not isinstance(adapter.NAME, str) or not adapter.NAME or not isinstance(adapter.REBOOT_MESSAGE, str):
        raise ValueError('Invalid adapter presentation metadata for ' + profile)
    return adapter


def list_devices() -> List[Dict[str, Any]]:
    """Raw observations from every registered adapter, without selecting one."""
    found: List[Dict[str, Any]] = []
    for profile in SUPPORTED:
        for observation in load(profile).list_devices():
            if observation not in found:
                found.append(observation)
    return found


def choose() -> Tuple[Adapter, SelectedDevice]:
    matches: List[Tuple[Adapter, SelectedDevice]] = []
    errors: List[str] = []
    names: List[str] = []
    for profile in SUPPORTED:
        adapter = load(profile)
        names.append(adapter.NAME + ', ' + profile[0].upper() + profile[1:])
        try:
            matches.append((adapter, adapter.discover()))
        except UnsupportedDevice:
            continue
        except ValueError as error:
            # Shared USB checks fail identically for every adapter; report once.
            if str(error) not in errors:
                errors.append(str(error))
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError('The connected device matches several adapters; refusing to choose')
    raise ValueError('; '.join(errors) or 'Unsupported device; supported: ' + '; '.join(names))


def profile_for_adt(blob: bytes) -> str:
    """Select the registered profile whose public Apple product names this ADT's model."""
    model = parse(blob).properties.get('model', b'').split(b'\0', 1)[0].decode('ascii', errors='replace')
    profiles = [profile for profile in SUPPORTED
                if model in hardware_device(profile)['device']['apple_products']]
    if len(profiles) != 1:
        raise ValueError('Cannot select a device profile from this ADT; specify --profile')
    return profiles[0]
