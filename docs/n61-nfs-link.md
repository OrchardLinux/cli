# Experimental iPhone 6 NFS host link

The N61 RAM experiment uses a USB network link to a private NFS root. Its host
address is `10.61.0.1/30`; the phone uses `10.61.0.2`. This command manages the
macOS host address for that link. The NFS server and any forwarding process
remain separate host resources.

Stage the RAM runtime with `--handoff manual` and the exact `nfs-root.json`
from the private NFS root. If the root is on a Linux server, copy that identity
file to the Mac without changing its bytes and compare the SHA-256 values. Use
the Mac copy for every host-link command in this session.

With the iPhone in DFU, run `orchard devices` and verify its ECID, chip `0x7000`
(28672) and board `0x06` (6) against the selected phone. Set `N61_LOCATION` in
the Mac shell to the reported numeric USB location. Boot the matching RAM image
with `orchard run /path/to/staged-ram-directory` and retain the same cable and
location through the transition. Stop to inspect the connection if the location
changes or another boot device appears.

Set `N61_ROOT_IDENTITY` to the Mac copy of `nfs-root.json` and
`N61_LINK_STATE` to a new private receipt path. Inspect the resulting gadget:

```sh
orchard nfs-link inspect --root-identity "$N61_ROOT_IDENTITY" --location "$N61_LOCATION"
```

The result includes `endpoint.serial_port` and `endpoint.boot_id`. Set
`N61_SERIAL_PORT` to the reported port and open it from the Mac:

```sh
screen "$N61_SERIAL_PORT" 115200
```

Run the following command in the phone's rescue shell:

```sh
cat /proc/sys/kernel/random/boot_id
```

Compare the result with `endpoint.boot_id`. Keep the serial session open and
use a second Mac terminal for host commands. Set `N61_BOOT_ID` there to the
matching UUID, along with `N61_LOCATION`, `N61_ROOT_IDENTITY` and
`N61_LINK_STATE` from the first terminal. Then configure the host address:

```sh
orchard nfs-link apply --root-identity "$N61_ROOT_IDENTITY" \
  --location "$N61_LOCATION" --boot-id "$N61_BOOT_ID" --state "$N61_LINK_STATE"
```

The command verifies the root, boot UUID, USB location, serial port and network
interface before requesting administrator access. It rejects conflicting routes,
an unowned existing address and ambiguous boot-device discovery. If address
setup fails after the intent is recorded, inspect the host and the receipt before
another attempt.

Start the NFS server and any required forwarding process for this session.
For the qualified network settings, the phone reaches the configured NFSv4.0
export through `10.61.0.1:32061`. For a server bound to loopback on a separate
Linux host, one SSH terminal can forward the port after the host address exists:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 10.61.0.1:32061:127.0.0.1:32061 "$NFS_SERVER_SSH"
```

Set `NFS_SERVER_SSH` to the server's SSH destination and add its port and key
options when needed. Verify the listening address or forward reaches
the intended server and that the export exposes the staged root with the same
identity bytes and root ownership. Keep the server and any forward running
throughout the Linux session.

Recheck the selected gadget and the owned host address using the same receipt:

```sh
orchard nfs-link inspect --root-identity "$N61_ROOT_IDENTITY" \
  --location "$N61_LOCATION" --boot-id "$N61_BOOT_ID" --state "$N61_LINK_STATE"
```

The result should report `already-owned`. On the checked serial shell, compare
`/proc/sys/kernel/random/boot_id` with `N61_BOOT_ID` again. When the link and
export are ready, request handoff once:

```sh
[ ! -e /run/orchard/nfs-handoff-attempted ] &&
  [ ! -e /run/orchard/nfs-handoff ] &&
  touch /run/orchard/nfs-handoff
```

The runtime makes one mount attempt and keeps the RAM rescue shell available
if handoff fails. Inspect the failure and device state before another boot.

After shutting down the Linux session and disconnecting its NFS gadget, retire
the address owned by that receipt:

```sh
orchard nfs-link cleanup --root-identity "$N61_ROOT_IDENTITY" --state "$N61_LINK_STATE"
```

Cleanup refuses to run while any Apple `05ac:4144` experimental gadget remains
connected, including the RAM-only shell. It checks the recorded interface and
hardware address before removing an alias. If the interface has disappeared,
the receipt is retired without changing another interface. The NFS server,
forwarding processes and other host exports remain under their owners' control.

This gadget advertises the profile, root UUID and boot UUID. It does not yet
expose the device ECID or implement Orchard's serial-pinned SSH bootstrap and
device protocol. The operator must retain the verified DFU location throughout
the transition. To qualify full recovery, the RAM runtime needs to parse the
chosen `unique-chip-id` from the live ADT and bind it to the selected ECID,
root UUID and kernel boot UUID. It must provision an ephemeral client key over
the checked serial endpoint, return its boot-scoped SSH host key over that same
endpoint, and answer the shared recovery status request through the pinned SSH
session. The host must check the same device and boot at each transition.
Treat this link as a bring-up interface until those checks pass on the phone.
