# Device protocol v1

Images expose `orchard-device` and image-owned generic `ready`. Omarchy-specific checks described below belong to its image.

## Ownership

- Host: device confirmation, USB boot transport, release/cache resolution, Apple firmware acquisition/conversion, destination-selection UI, byte transport, progress and timing.
- Device: hardware activation, storage inspection and safety, destination validation, extraction, finalization, promotion, handoff and diagnostics.
- Public producer: modules, indexes, scripts, services, permissions and runtime integrity lists.
- Private mixin: exactly the device firmware data allowlist plus `mixin.json`. No modules, runtime scripts or userspace binaries.

## Transport

Initial USB serial invokes the versioned `orchard-device bootstrap BASE64_JSON` operation with a public key. The runtime owns authorization, daemon/PID/key-file state and startup; it returns the host key and endpoint. The host only pins that response and waits for authenticated connectivity. No device filesystem or service-management shell snippets belong in the host.

Authenticated USB-network SSH invokes `orchard-device OPERATION BASE64_JSON`. Requests include `protocol: 1`. `prepare` and `install` carry their byte stream on stdin. Other operations do not consume payload data.

Stdout is newline-delimited JSON:

- `phase`: named device activity, no fabricated percentage.
- `progress`: a bounded descriptive message.
- `result`: one completed operation result.
- `error`: actionable error, with `retry: false`.

All records include `protocol: 1`. Lost connections during mutations mean unknown outcome, not retry permission. Read-only readiness polling is safe.

## Operations

| Operation | Request | Result / effect |
|---|---|---|
| `bootstrap` | public key, profile, ECID, boot ID (serial) | Authorize in RAM, ensure the bundled SSH daemon, return host key and endpoint. No hardware preparation or storage access. |
| `status` | none | Profile, live ECID/boot ID, release, phase/failure state, desktop readiness, device-owned `installation_attempted` flag, formatting receipt and capabilities. |
| `prepare` | release, bytes, sha256 + mixin stdin | Verify fresh-ADT binding/data allowlist; apply in RAM, prepare hardware once, and clean owned leftover staging before target selection. |
| `targets` | none | Boot ID, device-issued selection token, capacity estimate and GPT Linux-data partition/signature descriptions; free space for verified ext4. |
| `format-target` | token, target ID, boot ID, operation ID, explicit confirmation | Once per recovery boot after preparation: revalidate a Linux-data target without recognized signatures, create ext4 without force/discard, verify UUID and restore fencing. Return a matching receipt; invalidate the old selection. |
| `destination` | token, target ID, relative directory | Read-only inspection; return existence, destination identity, free/reclaimable bytes, estimated needed bytes and inode sufficiency. No ownership is inferred from installation markers. |
| `install` | selected target/directory, destination_identity, explicit replace boolean, bytes, sha256 + root stdin | Revalidate selection; stream into fresh staging, verify, apply mixin and promote. |
| `handoff` | none | Recheck the completed installation, preserve diagnostics and ask recovery PID 1 to switch root. |
| `diagnostics` | none | Phase/failure state and bounded kernel/system logs. |
| `abort` | none | Device-owned incomplete-staging cleanup, unmount/fence storage, retain used-phase guards and require a fresh recovery boot. |
| `revoke-ssh` | current profile, ECID, `boot_id`, `release` | Only after successful readiness and matching installed/handoff receipt: retire listener/other connections, clear RAM credentials, return the final receipt over this non-multiplexed connection. No hardware/storage actions. |

Host code must not infer device paths or encode module loads, filesystem offsets, mounts, permissions or PID 1 commands. Target IDs are displayable identifiers, not host-authored storage paths.

## Streaming safety

- Root size, digest, compression and expansion limit are baked into recovery from the public release receipt.
- Package-defined privileged entries have a public metadata manifest. Producers validate contents before restoring special modes from pacman MTREE; runtime verifies content, UID/GID and modes before activation. Missing `sudo` setuid/root ownership is a finalization error, not a broken system to promote.
- Decode Zstandard and extract concurrently with incoming bytes; no temporary compressed archive.
- All file writes use no-follow directory descriptors; links are deferred. Traversal, duplicate paths, special files, reserved metadata and escaping hardlinks are rejected.
- Verify compressed-byte count/digest and decoder completion before creating links or promoting. Preserve file ownership, modes and timestamps.
- Replacement requires explicit approval and matching destination identity. Delete the selected directory before extraction; never create an automatic backup. Reject symlinks, non-directories and nested mounts. Estimate required bytes as expanded payload × 1.2 + 1 GiB and check free inodes (archive member count + 1024; legacy fallback 65536 + 1024). Recheck actual capacity after deletion. Failures must warn that replacement deletion cannot be undone.
- New staging receives a root-owned ownership marker before extraction; archive members cannot overwrite it. Cleanup requires matching partition/filesystem identity and compatible public release metadata. Staging symlinks, mounts, completed roots, backups and unrecognized directories are not deleted.
- Compatibility with pre-marker staging comes from explicitly supplied previous public root receipts, not hardcoded device identifiers or hashes.
- On phase failure the device attempts safe cleanup and restores the storage fence. If interrupted, the next fresh recovery scans eligible filesystems immediately after hardware preparation, before target selection. Normal ext4 journal replay occurs before scanning.
- A rerun asks for cleanup before preparing another mixin. Used-phase guards are never cleared for same-session reuse: the result instructs a fresh recovery boot and `orchard install`. Cleanup errors are explicit; there is no continuation/RAM-update framework.

## Readiness and diagnostics

- Handoff preserves only this boot's host key and explicitly provisioned authorized key in `/run`.
- A separate installed service starts key-only SSH on the USB address, independent of the platform/login gate.
- Pre-exec PID 1 failures attempt to restore moved pseudo-filesystem aliases and restart key-only recovery diagnostics, never retrying handoff/storage. Reported failures stop host readiness polling immediately. Kernel failure or failure inside the final root-switch executable can still require DFU recovery.
- The desktop shell reports readiness only after constructing its root and successfully capturing a compositor frame to `/dev/null`. The device checks the boot ID, live shell process and owned Wayland socket.
- No screenshot is retained. Readiness does not attest touch gestures, brightness interaction or Wi-Fi association.
