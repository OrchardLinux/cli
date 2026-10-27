# Orchard CLI

Host-side tools for installing, booting, and debugging Linux on supported Apple devices.

## Install or update

Requires Linux or macOS, Python 3.9+ with venv/pip, libusb, and OpenSSH. Linux also requires sudo, udev, and [apfs-fuse](https://github.com/sgan81/apfs-fuse) with its FUSE runtime for firmware extraction. Install `apfs-fuse` separately and put it in PATH, or set `IPSW_APFS_FUSE_PATH` to its executable. `setup-host` checks for the executable before downloading tools or installing USB rules.

```sh
python3 -m venv ~/.local/share/orchard/venv
~/.local/share/orchard/venv/bin/python -m pip install --upgrade --only-binary=:all: -r \
  https://github.com/OrchardLinux/cli/releases/latest/download/requirements.txt
mkdir -p ~/.local/bin
ln -sfn ~/.local/share/orchard/venv/bin/orchard ~/.local/bin/orchard
export PATH="$HOME/.local/bin:$PATH"
orchard setup-host
```

The release requirements select compatible CLI and hardware wheels from GitHub, with SHA-256 hashes. Other Python dependencies come from PyPI; the project packages do not require PyPI publication. Rerun these commands to update both Orchard and its paired hardware package. If an installation says “Upgrade Orchard to at least …”, rerun the commands above, then retry the installation.

`setup-host` installs missing palera1n and ipsw executables into `~/.local/bin`, leaving existing executables untouched. Linux USB rules are installed with sudo when needed; reconnect the device afterward. It never boots or modifies a device.

## Select a release

```sh
orchard distros omarchy-a https://github.com/OrchardLinux/omarchy-a/releases/latest/download/index.toml
orchard install omarchy-a
```

The registered name stores a URL. Each resolution fetches the current index; verified images are cached by SHA-256. Publishing a new distro index updates future installations, not an already installed operating system.

## Launch an installed system

Connect the device in DFU mode, then run:

```sh
orchard launch
```

The detected device selects its shared launch assets automatically. Pongo and
m1n1 show a black screen, without distro branding. Recovery discovers compatible
installed systems; select one and type the requested confirmation to start it.
This does not download a root image, format storage, or reinstall the system.

`--directory /omarchy` and `--target PARTUUID` optionally filter discovered
installations. `--manifest PATH_OR_URL` is a development override, not required
for normal use. Firmware downloads require `--allow-firmware-download` when the
verified local firmware cache is missing.

The hardware release must publish the device's launch manifest and boot assets;
see the [launcher build and publication instructions](https://github.com/OrchardLinux/hardware/blob/main/boot/quiet/README.md).

## Documentation

- [Release publishing and updates](docs/releases.md)
- [Device operation protocol](docs/device-protocol.md)
- [Host adapter contract](docs/adapter-contract.md)
- [SSH lifecycle](docs/ssh-lifecycle.md)
