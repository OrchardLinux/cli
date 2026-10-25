# Installer SSH lifecycle

Omarchy A and the minimal Alpine image use the same installer SSH policy.

## Bootstrap and retention

The installer creates a fresh, passphrase-free Ed25519 client key. Its public key is provisioned through physical USB serial. Recovery creates an Ed25519 server host key in RAM; the installer pins the returned public key with strict host-key checking. These are SSH keys, not CA-signed certificates.

Direct handoff preserves `/run/recovery`. Both images bind its `authorized_keys` inode onto an empty `/root/.ssh/authorized_keys` placeholder. Neither persists a client public key or server private key to the installed filesystem. Bind setup is idempotent. The server listens on the USB address with passwords and forwarding disabled.

Only after verified readiness and durable installation-success reporting, the installer asks:

```
Keep SSH access for this boot session? [y/N]
```

- Explicit `y`/`yes`: retain this boot's diagnostics and the private host-side credential cache.
- No, empty answer, EOF or interrupt at this prompt: revoke access.
- Preparation/install/readiness failure: do not offer or run successful-install revocation; preserve diagnostics.

Retained device credentials disappear on reboot/power loss. Host credentials and reports are stored under `private/devices/PROFILE/ECID/boots/BOOT_ID/`. Discovery reads ECID/boot identity from USB; pinned SSH status must match before credentials are used. A new boot never falls back to another boot's keys. USB ports may change between commands. One host-wide lock permits only one active device operation; multiple connected targets are rejected.

## Revocation

The shared `revoke-ssh` operation requires the current boot/release, successful image readiness, no installation failure, and matching installed/handoff receipts. It refuses unexpected/non-bind-mounted authorization paths before changing access.

It marks revocation in private RAM state, empties the shared authorization inode first, stops the image-owned diagnostic service, disconnects other Dropbear connections, unmounts/removes the empty authorization placeholder and removes RAM authorization/host-key files. Startup and serial authorization scripts refuse to re-enable this boot session after retirement.

The requesting connection lives only long enough to return its final boot/release-bound receipt, then exits. Installer SSH explicitly disables multiplexing/ControlPersist so that receipt connection cannot remain as a reusable authenticated channel. Init service shutdown preserves this one response connection; the shared revoker terminates the others.

After the receipt, the installer deletes its cached identity, known-host pin and endpoint. The active transport's temporary key is removed on context exit. A lost/mismatched receipt is recorded as **unknown**, never retried or reported as revoked. Installation success remains recorded; reboot to discard an uncertain session rather than reinstalling. Local cleanup errors are separately reported.

A root-owned, private, current-boot/release revocation receipt permits Alpine to omit the deliberately retired diagnostics process from readiness checks. All platform, PID 1, charge, DHCP, button and installed-image checks remain live and unchanged. Omarchy's desktop readiness is already independent of diagnostics. After retirement, remote status queries are unavailable; the host installation report is historical verified success, not a new live status query.

This retires installer-managed SSH, not arbitrary administrator-created remote services or commands previously launched through SSH. Root administrators can deliberately override runtime state.

## Focused validation

Host tests cover explicit retention, secure default/EOF/interrupt, final receipt matching, unknown outcomes/no replay, credential deletion and non-multiplexing. Shared runtime tests cover protected current-boot markers, RAM-inode validation and connection selection. Alpine tests cover readiness after intentional SSH retirement without dropping hardware checks.

`minimal-linux/tests/native-ssh-lifecycle.py` runs inside an isolated finished Alpine image, or with `--systemd` inside the Arch/systemd probe with the Omarchy policy/unit/helpers staged. It exercises real SSH, a second live connection, a real RAM bind with an empty underlying disk placeholder, both init shutdown paths, receipt delivery, key removal, listener closure, failed-readiness preservation and restart/reauthorization rejection. No hardware readiness success is fabricated and no physical device is used.

Alpine probe capabilities: `NET_ADMIN` for a dummy USB NIC, `SYS_ADMIN` for bind mounts, `SYS_PTRACE` for root inspection of Dropbear's non-dumpable processes. No device passthrough or external network is needed. These are fixture permissions, not kernel/readiness bypasses.
