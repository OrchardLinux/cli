"""Inspect and prune the shared build component cache (components-v2)."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

from .build import INPUTS_RECORD, cache_root, component_lock, remove_unshared, share_files


def _size(path):
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            file = Path(directory) / name
            if not file.is_symlink():
                total += file.stat().st_size
    return total


def _on_disk(path):
    """Bytes under PATH, counting hard-linked files once."""
    seen, total = set(), 0
    for directory, _, names in os.walk(path):
        for name in names:
            info = (Path(directory) / name).lstat()
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                total += info.st_size
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
            for parent in sorted(components.iterdir()) if parent.is_dir() and not parent.name.startswith('.')
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
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')  # last use
        state = 'unfinished build' if key.startswith('build-') else _summary(path)
        print(f'{component:22} {key[:12]:12} {_human(size):>7}  {when}  {state}')
    components = Path(cache or cache_root()) / 'components-v2'
    print(f'{len(rows)} entries, {_human(total)} ({_human(_on_disk(components))} on disk) in {components}')


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
        remove_unshared(root / 'components-v2')


def prune(keep, cache=None):
    """Keep the KEEP most recently used entries of each component; delete the rest.

    Also removes builds that never finished and shares identical files of
    entries published before sharing existed.
    """
    if keep < 1:
        raise ValueError('--keep must be at least 1')
    root = Path(cache or cache_root())
    components = root / 'components-v2'
    if not components.is_dir():
        print('Component cache is empty')
        return
    with component_lock(root):
        before = _on_disk(components)
        kept = {}
        for component, key, path in reversed(entries(root)):  # newest use first
            if key.startswith('build-') or kept.setdefault(component, 0) >= keep:
                shutil.rmtree(path)
                print(f'Removed {component}/{key}')
                continue
            kept[component] += 1
            share_files(components, path, json.loads((path / 'component.json').read_text()))
        remove_unshared(components)
        after = _on_disk(components)
    print(f'Freed {_human(before - after)}; {_human(after)} on disk in {components}')
