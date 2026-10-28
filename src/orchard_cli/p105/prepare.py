"""Make an owner-local boot manifest from qualified private components."""
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Dict

from .boot import IBEC_SHA, KLOADER_SHA
from .config import regular


def manifest(components: Path, ibec: Path, kloader: Path, output: Path) -> Dict[str, Any]:
    record = json.loads(regular(components/'components.json', private=True).read_text())
    if (record.get('profile') != 'p105' or record.get('private') is not True or
            record.get('publishable') is not False or record.get('mode') != 'orchard-recovery' or
            type(record.get('device_ecid')) is not int or not 0 < record['device_ecid'] < 2**64 or
            record.get('flash_controller') != 'disabled'):
        raise ValueError('Expected owner-bound P105 Orchard recovery components')
    inputs = {'boot':components/'staging-bundle.bin','loader':components/'staging-loader.bin',
              'ibec':ibec,'kloader':kloader}
    hashes = {role:hashlib.sha256(regular(path).read_bytes()).hexdigest() for role,path in inputs.items()}
    if hashes['ibec'] != IBEC_SHA or hashes['kloader'] != KLOADER_SHA:
        raise ValueError('P105 needs the qualified iBEC and kloader inputs')
    for role in ('boot','loader'):
        if hashes[role] != record['components'][inputs[role].name]['sha256']:
            raise ValueError('Private boot components changed')
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a fresh owner boot directory')
    os.umask(0o077)
    output.mkdir(parents=True,mode=0o700)
    rows = ['schema = 1','kind = "orchard.boot"','device = "p105"',
            'boot_method = "jailbreak-kdfu"','min_orchard = "0.1.11"','protocol = 1',
            'private = true','target_ecid = '+str(record['device_ecid'])]
    for role,path in inputs.items():
        name = {'boot':'staging-bundle.bin','loader':'staging-loader.bin','ibec':'ibec.raw','kloader':'kloader'}[role]
        shutil.copyfile(path,output/name)
        (output/name).chmod(0o600)
        rows += ['','['+role+']','file = '+json.dumps(name),'bytes = '+str(path.stat().st_size),
                 'sha256 = '+json.dumps(hashes[role])]
    destination = output/'boot.toml'
    destination.write_text('\n'.join(rows)+'\n')
    destination.chmod(0o600)
    return {'manifest':str(destination),'private':True,'publishable':False}
