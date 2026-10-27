"""J71b firmware recipe and board-specific selections; shared code owns acquisition."""
import json
from pathlib import Path
import tempfile

from orchard_cli.apple import ipsw
from orchard_cli.cache import cache_root
from orchard_cli.progress import Clock, Progress
from orchard_cli.firmware import ensure_archive, missing_inputs, store_input
from orchard_cli.integrity import digest
from .config import MANIFEST, PROFILE
from orchard_cli.apple.smartio import extract as smartio


def ensure(clock: Clock, progress: Progress, *, allow_download: bool = True) -> Path:
    spec = json.loads(MANIFEST.read_text())
    root = cache_root() / 'firmware' / PROFILE
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    expected = {row['name']: row['sha256'] for row in spec['files'] + spec['extracted_inputs']}
    locations = {name: root / ('extra' if name in ('t8010-smartio.bin', 'BCM4355C1-FigaroA-USI.hcd') else 'extracted') / name
                 for name in expected}
    if not missing_inputs(locations, expected):
        progress.phase('Verified cached firmware')
        return root
    if not allow_download:
        raise ValueError('Verified private firmware cache is missing. Launch does not download an IPSW by default; '
                         'explicitly use --allow-firmware-download to acquire it.')
    archive = ensure_archive(spec['ipsw_url'], spec['ipsw_bytes'], spec['ipsw_sha256'],
                             root / 'archive' / (spec['ipsw_sha256'] + '.ipsw'),
                             clock, progress, label='Apple IPSW')
    executable = ipsw.tool(clock, progress)
    progress.phase('Extracting firmware…')
    with tempfile.TemporaryDirectory(prefix='extract-', dir=root.parent) as directory:
        raw = Path(directory)
        ipsw.extract_files(executable, archive, (*spec['extract_patterns'], 'usr/sbin/BlueTool$'), raw)
        for name in missing_inputs(locations, expected):
            if name == 'BCM4355C1-FigaroA-USI.hcd':
                candidates = list(raw.rglob('BlueTool'))
                if len(candidates) != 1:
                    raise ValueError('Expected one extracted BlueTool')
                data = ipsw.hcd_from_binary(candidates[0].read_bytes(), expected[name])
            elif name == 't8010-smartio.bin':
                data, _ = smartio(ipsw.extract_kernel(executable, archive, spec['device'], raw))
            else:
                candidates = [p for p in raw.rglob(name) if p.is_file() and digest(p) == expected[name]]
                if len(candidates) != 1:
                    raise ValueError('Expected one verified extracted firmware input: ' + name)
                data = candidates[0].read_bytes()
            store_input(locations[name], data, expected[name])
    if missing_inputs(locations, expected):
        raise ValueError('Incomplete firmware input cache')
    return root
