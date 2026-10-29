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
| `status` | none | Profile, live ECID/boot ID, release, phase/failure state, desktop readiness, device-owned `installation_attempted` flag, formatting receipt and capabilities (including `reset-ext4-v1` in supporting installer recovery). |
| `prepare` | release, bytes, sha256 + mixin stdin | Verify fresh-ADT binding/data allowlist; apply in RAM, prepare hardware once, and clean owned leftover staging before target selection. |
| `targets` | none | Boot ID, device-issued selection token, capacity estimate and GPT Linux-data partition/signature descriptions; free space for verified ext4. |
| `format-target` | token, target ID, boot ID, operation ID, explicit confirmation | Once per recovery boot after preparation: revalidate a Linux-data target without recognized signatures, create ext4 without force/discard, verify UUID and restore fencing. Return a matching receipt; invalidate the old selection. |
| `reset-target` | token, target ID, boot ID, operation ID, expected filesystem UUID, exact `reset <target ID>` confirmation | Capability `reset-ext4-v1` required. Reset a verified existing ext4 Linux-data partition; preserve GPT and partition geometry, assign a new UUID, verify emptiness and restore fencing. Shares the once-per-boot permission with `format-target`; return a matching receipt and invalidate the selection. |
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

## Existing-filesystem reset

Only supporting installer recovery advertises `reset-ext4-v1`; launcher mode refuses
`reset-target`. The host package version alone does not establish support. Existing
clients and recovery retain the unformatted-only `format-target` operation.

After selecting ext4, a capable host offers Keep (default), Reset, or Cancel before
inspecting the installation directory. Reset identifies the partition label, ID,
capacity, type and UUID, warns that **all contents of the partition** will be lost
(including other roots, personal files and image caches), and requires the exact
phrase `reset <target ID>`. No backup is made. Partition boundaries and GPT stay
unchanged; formatting is not secure erasure. Cancelling before dispatch performs
neither formatting nor installation. Existing preparation still performs device-owned
journal replay and cleanup of recognized incomplete staging before this menu;
cancellation is not a guarantee of zero writes during preparation.

The protocol-1 request is `{selection_token, target, boot_id, operation_id,
expected_filesystem_uuid, confirmation}`. Operation IDs are 32 lowercase hex digits.
Reset accepts only unambiguous ext4 with a nonzero UUID on an eligible GPT Linux-data
partition. The device revalidates the complete selection, GPT, geometry, device
identity, unmounted state, swap/holder exclusions and read-only fencing before writes.
It records uncertainty and consumes the shared formatting permission first. Reset
uses a single formatter force option, never doubled force, and disables discard.

The reset receipt contains `{operation: "reset-target", boot_id, operation_id,
target, previous_filesystem_uuid, filesystem_uuid, filesystem: "ext4", status:
"complete"}`. Completion requires a new UUID, unchanged partition identity and GPT,
restored fencing, and a read-only emptiness check (only an empty `lost+found` is
allowed), followed by unmounting. The host validates the receipt against its saved
request, then rediscovers the same partition with the new UUID, same capacity and
boot, and a new selection token. Only then may destination selection continue.

Host and device persist `status: "unknown"` before dispatch/writes respectively.
Interrupted, failed or unverified reset stops installation and is never automatically
retried. Inspect storage and the boot-scoped `last-format.json` report before making
any new destructive choice. Successful reset followed by failed rediscovery retains
the completed receipt but also stops installation. A new recovery boot does not
constitute permission to repeat a reset. Later destination failures or cancellation
retain the completed reset receipt and explicitly report that previous contents
were destroyed. Abort also reports completed resets, and preserves an unknown
reset outcome without entering storage cleanup.
