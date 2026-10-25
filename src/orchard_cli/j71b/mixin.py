"""Prepare J71b's private inputs, then use the shared data-only mixin packer."""
from pathlib import Path
from typing import Tuple

from orchard_cli.contracts import MixinReceipt
from orchard_cli.firmware import pack_mixin
from .prepare import prepare, verify
from .config import MANIFEST, PROFILE


def build(adt: Path, firmware_dir: Path, outdir: Path) -> Tuple[Path, MixinReceipt]:
    if outdir.exists():
        raise ValueError('Choose a new private output directory')
    outdir.mkdir(parents=True, mode=0o700)
    prepared = prepare(adt, firmware_dir, outdir / 'prepared')
    receipt = verify(prepared, MANIFEST, adt)
    metadata = {'schema': 1, 'profile': PROFILE, 'adt_sha256': receipt['adt_sha256'],
                'files': receipt['files']}
    return pack_mixin(prepared, metadata, outdir)
