"""Device adapter registry. Shared workflow never interprets hardware identities."""
import importlib
from typing import List, Optional, Tuple

from .contracts import Adapter, SelectedDevice

SUPPORTED = ('j71b',)


def load(profile: Optional[str] = None) -> Adapter:
    profile = profile or SUPPORTED[0]
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


def choose() -> Tuple[Adapter, SelectedDevice]:
    matches: List[Tuple[Adapter, SelectedDevice]] = []
    errors: List[str] = []
    for profile in SUPPORTED:
        adapter = load(profile)
        try:
            matches.append((adapter, adapter.discover()))
        except ValueError as error:
            errors.append(str(error))
    if len(matches) != 1:
        raise ValueError('; '.join(errors) or 'Connect exactly one supported device')
    return matches[0]
