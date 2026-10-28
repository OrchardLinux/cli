"""Explicit owner inputs. Importing the adapter never reads this configuration."""
import json
import os
from pathlib import Path
import stat
from typing import Any, Dict

from orchard_cli.cache import cache_root
from orchard_cli.state import DeviceState

PROFILE = 'p105'
NAME = 'iPad mini 1 (Wi-Fi)'
PRODUCT = 'Orchard-P105'
CHIP, BOARD = 0x8942, 0x0a


def regular(path: Path, *, private: bool = False) -> Path:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (private and (info.st_mode & 0o077 or info.st_uid != os.getuid())):
        raise ValueError('Expected an owner-only regular input' if private else 'Expected a regular input')
    return path


def settings() -> Dict[str, Any]:
    path = Path(os.environ.get('ORCHARD_P105_CONFIG', cache_root() / 'private/p105.json')).expanduser().absolute()
    regular(path, private=True)
    value = json.loads(path.read_text())
    if value.get('schema') != 1 or not isinstance(value.get('target'), dict):
        raise ValueError('Expected P105 owner configuration schema 1')
    DeviceState(PROFILE, value['target']['ecid'])
    import re
    if not re.fullmatch('[0-9a-fA-F]{40}', value['target'].get('udid', '')):
        raise ValueError('Invalid configured P105 UDID')
    return value


def tool(name: str) -> str:
    path = Path(settings()['tools']) / name
    if not regular(path).stat().st_mode & 0o111:
        raise ValueError('P105 host tool is not executable: ' + name)
    return str(path)
