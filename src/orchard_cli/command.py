#!/usr/bin/env python3
"""Orchard: guided installation and composable device commands."""
import argparse
from contextlib import ExitStack
import json
import os
import subprocess
from pathlib import Path
import sys

from importlib.metadata import version
from orchard_cli.devices import load, choose
from orchard_cli.progress import Progress
from orchard_cli.protocol import Client
from orchard_cli.releases import resolve, catalogue, register
from orchard_cli.integrity import digest
from orchard_cli.service import install
from orchard_cli.state import operation_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=version('orchard-cli'))
    parser.add_argument('--verbose', action='store_true', help='show internal device events')
    commands = parser.add_subparsers(dest='action', required=True)
    guided = commands.add_parser('install', help='guided installation from distro name, index or manifest')
    guided.add_argument('source', nargs='?', help='registered distro, local TOML or exact HTTPS URL')
    guided.add_argument('--manifest', '--release', dest='manifest', help='explicit index or installation manifest')
    launcher = commands.add_parser('launch', help='start an existing installation through shared recovery; no root download')
    launcher.add_argument('--manifest', help='override the device-selected launch manifest (development/testing)')
    launcher.add_argument('--directory', help='installed root directory, such as /omarchy')
    launcher.add_argument('--target', help='installed partition PARTUUID')
    launcher.add_argument('--allow-firmware-download', action='store_true', help='allow IPSW acquisition if verified host firmware cache is missing')
    distros = commands.add_parser('distros', help='list or register distro index locations')
    distros.add_argument('name', nargs='?')
    distros.add_argument('source', nargs='?')
    check = commands.add_parser('resolve', help='verify inputs without accessing a device')
    check.add_argument('source')
    check.add_argument('--profile', default='j71b')
    check.add_argument('--boot', action='store_true', help='resolve only boot assets')
    commands.add_parser('devices', help='list USB devices without modifying them')
    commands.add_parser('setup-host', help='install missing boot tools and Linux USB permissions; no device access')
    boot = commands.add_parser('boot', help='boot recovery from a manifest or explicit image')
    boot.add_argument('source', nargs='?', help='registered distro, local TOML or exact HTTPS URL')
    boot.add_argument('--manifest', '--release', dest='manifest')
    boot.add_argument('--image', type=Path)
    boot.add_argument('--receipt', type=Path)
    boot.add_argument('--pongo-image', type=Path)
    adt = commands.add_parser('capture-adt', help='save a fresh recovery ADT privately')
    adt.add_argument('--output', type=Path, required=True)
    mixin = commands.add_parser('mixin', help='prepare a data-only private mixin')
    for name in ('adt', 'firmware-dir', 'output'):
        mixin.add_argument('--' + name, type=Path, required=True)
    for action in ('status', 'diagnostics', 'targets', 'destination', 'prepare', 'install-root', 'handoff', 'abort'):
        command = commands.add_parser(action, help='invoke the device-owned ' + action + ' operation')
        command.add_argument('--recovery', action='store_true', help='provision access through the recovery serial shell')
        if action in ('destination', 'install-root'):
            command.add_argument('--target', required=True, help='observed partition identifier')
            command.add_argument('--token', required=True, help='device-issued selection token')
            command.add_argument('--directory', required=True)
        if action == 'prepare':
            command.add_argument('--mixin', type=Path, required=True)
            command.add_argument('--receipt', type=Path, required=True)
        if action == 'install-root':
            command.add_argument('--image', type=Path, required=True)
            command.add_argument('--receipt', type=Path, required=True)
            command.add_argument('--replace', action='store_true', help='permanently delete the destination without backup')
            command.add_argument('--destination-identity', type=json.loads, default=None,
                                 help='JSON identity returned by destination inspection')
    args = parser.parse_args()
    progress = Progress(verbose=args.verbose)
    try:
        with ExitStack() as stack:
            if args.action not in ('install', 'launch', 'distros', 'resolve', 'devices', 'mixin', 'setup-host'):
                stack.enter_context(operation_lock())
            if args.action == 'install':
                source = args.manifest or args.source
                if not source:
                    raise ValueError('Specify a distro, index or manifest: orchard install SOURCE')
                result = install(source, progress=progress)
            elif args.action == 'launch':
                from orchard_cli.launch import launch
                result = launch(args.manifest, directory=args.directory, target=args.target,
                                allow_firmware_download=args.allow_firmware_download, progress=progress)
            elif args.action == 'distros':
                if bool(args.name) != bool(args.source):
                    raise ValueError('Use orchard distros NAME INDEX_URL_OR_FILE, or orchard distros to list')
                result = register(args.name, args.source) if args.name else catalogue()
            elif args.action == 'resolve':
                record, assets = resolve(args.source, args.profile, progress, purpose='boot' if args.boot else 'install')
                result = {'manifest': record, 'files': {key: str(path) for key, path in assets.items()}}
            elif args.action == 'devices':
                result = load().list_devices()
            elif args.action == 'setup-host':
                from orchard_cli.host_setup import setup
                result = setup(progress)
            elif args.action == 'boot':
                adapter, device = choose()
                if args.image:
                    if not args.receipt:
                        raise ValueError('--image requires --receipt')
                    receipt = json.loads(args.receipt.read_text())
                    expected = receipt.get('image_sha256', receipt.get('sha256'))
                    if args.image.is_symlink() or digest(args.image) != expected:
                        raise ValueError('Explicit recovery image differs from receipt')
                    if device['mode'] == 'dfu' and not args.pongo_image:
                        raise ValueError('DFU boot with explicit files requires --pongo-image')
                    assets = {'recovery': args.image, 'pongo': args.pongo_image}
                    record = {'assets': {'recovery': {'sha256': expected}}}
                else:
                    source = args.manifest or args.source
                    if not source:
                        raise ValueError('Specify a manifest or explicit --image/--receipt inputs')
                    record, assets = resolve(source, adapter.PROFILE, progress, purpose='boot')
                adapter.boot(device, assets, record, progress)
                result = {'recovery_observed': True}
            elif args.action == 'capture-adt':
                adapter, device = choose()
                if args.output.exists() or args.output.is_symlink():
                    raise ValueError('ADT destination already exists')
                data = adapter.capture_adt(device)
                args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                    stream.write(data)
                result = {'saved': str(args.output), 'bytes': len(data)}
            elif args.action == 'mixin':
                adapter = load()
                archive, result = adapter.prepare_mixin(args.adt, args.firmware_dir, args.output)
                result = {**result, 'path': str(archive)}
            else:
                adapter, device = choose()
                connection = adapter.connect if args.recovery else adapter.connect_saved
                with connection(device) as ssh:
                    client = Client(ssh, progress)
                    request = {}
                    if args.action in ('destination', 'install-root'):
                        request = {'target': args.target, 'selection_token': args.token, 'directory': args.directory}
                    if args.action in ('prepare', 'install-root'):
                        path = args.mixin if args.action == 'prepare' else args.image
                        receipt = json.loads(args.receipt.read_text())
                        if path.is_symlink() or path.stat().st_size != receipt['bytes'] or digest(path) != receipt['sha256']:
                            raise ValueError('Explicit input differs from receipt')
                        request.update({k: receipt[k] for k in ('bytes', 'sha256')})
                        if args.action == 'prepare':
                            request['release'] = client.call('status')['release']
                        else:
                            request['replace'] = args.replace
                            request['destination_identity'] = args.destination_identity
                        with path.open('rb') as source:
                            result = client.call('prepare' if args.action == 'prepare' else 'install', request,
                                                 source=source, size=receipt['bytes'], timeout=1200)
                    else:
                        result = client.call(args.action, request)
            with progress.lock:
                progress.end_line()
            if args.action not in ('install', 'launch') or not sys.stdout.isatty():
                print(json.dumps(result, indent=2))
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as error:
        with progress.lock:
            progress.end_line()
        details = f'\nDiagnostic log: {progress.log_path}' if progress.log_path else ''
        parser.exit(1, f'orchard: {error}{details}\n')


if __name__ == '__main__':
    main()
