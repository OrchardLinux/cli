"""Record the source contents that produced a local build."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


EXCLUDED = {'.git', '.work', '.venv', '__pycache__', '.mypy_cache', '.pytest_cache', 'build', 'dist'}


def snapshot(directory):
    """Fingerprint package contents and report a Git head only when this is tracked source."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError('Source package directory is missing: ' + str(root))
    files = {}
    for current, directories, names in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in EXCLUDED and not name.endswith('.egg-info'))
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError('Linked source package directory: ' + str(Path(current) / name))
        for name in sorted(names):
            if name in EXCLUDED or name.endswith('.egg-info'):
                continue
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Linked or unsupported source package file: ' + str(path))
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            files[path.relative_to(root).as_posix()] = {
                'bytes': path.stat().st_size, 'mode': path.stat().st_mode & 0o777, 'sha256': digest.hexdigest()}
    result = {'sha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
              'kind': 'installed'}
    try:
        top = Path(subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                                           stderr=subprocess.DEVNULL, text=True).strip()).resolve()
        tracked = (root / '__init__.py').relative_to(top)
        subprocess.run(['git', '-C', str(top), 'ls-files', '--error-unmatch', '--', str(tracked)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        head = subprocess.check_output(['git', '-C', str(top), 'rev-parse', 'HEAD'],
                                       stderr=subprocess.DEVNULL, text=True).strip()
        status = subprocess.check_output(['git', '-C', str(top), 'status', '--porcelain', '--untracked-files=all'],
                                         stderr=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return result
    result.update(kind='checkout', git={'head': head, 'clean': not bool(status)})
    return result


def cli():
    import orchard_cli
    import orchard_cli_schemas
    return {'code': snapshot(Path(orchard_cli.__file__).parent),
            'schemas': snapshot(Path(orchard_cli_schemas.__file__).parent)}
