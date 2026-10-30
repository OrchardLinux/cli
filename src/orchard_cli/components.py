"""Inspect and prune the shared build component cache (components-v2)."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

from .build import INPUTS_RECORD, cache_root, component_lock


def _size(path):
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            file = Path(directory) / name
            if not file.is_symlink():
                total += file.stat().st_size
    return total


def _human(size):
    for unit in ('B', 'K', 'M', 'G'):
        if size < 1024 or unit == 'G':
            return f'{size:.0f}{unit}' if unit == 'B' else f'{size:.1f}{unit}'
        size /= 1024


def entries(cache=None):
    """(component, key, path) for published entries and leftover build directories."""
    components = Path(cache or cache_root()) / 'components-v2'
    if not components.is_dir():
        return []
    return [(parent.name, entry.name, entry)
            for parent in sorted(components.iterdir()) if parent.is_dir()
            for entry in sorted(parent.iterdir(), key=lambda p: p.stat().st_mtime) if entry.is_dir()]


def _summary(path):
    record = path / INPUTS_RECORD
    if not record.is_file():
        return 'inputs not recorded'
    inputs = json.loads(record.read_text())
    if 'tree' in inputs:
        return f"tree {inputs['tree'][:12]}"
    if 'base' in inputs:
        return f"base {inputs['base'][:12]} dtb {inputs.get('dtb')}"
    return ''


def show(cache=None):
    rows = entries(cache)
    if not rows:
        print('Component cache is empty')
        return
    total = 0
    for component, key, path in rows:
        size = _size(path)
        total += size
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
        state = 'unfinished build' if key.startswith('build-') else _summary(path)
        print(f'{component:22} {key[:12]:12} {_human(size):>7}  {when}  {state}')
    print(f'{len(rows)} entries, {_human(total)} in {Path(cache or cache_root()) / "components-v2"}')


def remove(selectors, cache=None):
    """Delete entries named COMPONENT/KEY-PREFIX or COMPONENT (all of its entries)."""
    root = Path(cache or cache_root())
    rows = entries(root)
    chosen = []
    for selector in selectors:
        component, _, prefix = selector.partition('/')
        matches = [r for r in rows if r[0] == component and r[1].startswith(prefix)]
        if not matches:
            raise ValueError(f'No cache entry matches {selector}')
        if prefix and len(matches) > 1:
            raise ValueError(f'{selector} is ambiguous; give more of the key')
        chosen += [m for m in matches if m not in chosen]
    # The build lock keeps removal away from a running build and its sources.
    with component_lock(root):
        for component, key, path in chosen:
            shutil.rmtree(path)
            print(f'Removed {component}/{key}')
