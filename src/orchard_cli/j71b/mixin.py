"""Prepare J71b's firmware, then use the shared data-only mixin packer."""
from pathlib import Path
from typing import Tuple

from orchard_cli.contracts import MixinReceipt
from orchard_cli.firmware import pack_mixin
from .prepare import prepare, verify
from .config import MANIFEST, PROFILE


def build(firmware_dir: Path, outdir: Path) -> Tuple[Path, MixinReceipt]:
    if outdir.exists():
        raise ValueError('Choose a new private output directory')
    outdir.mkdir(parents=True, mode=0o700)
    prepared = prepare(firmware_dir, outdir / 'prepared')
    receipt = verify(prepared, MANIFEST)
    metadata = {'schema': 1, 'profile': PROFILE, 'files': receipt['files']}
    return pack_mixin(prepared, metadata, outdir)
