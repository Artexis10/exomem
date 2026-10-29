## Why

The transactional batch writer reads every staged binary again before each
destination replacement, and reads every installed binary again after it. A
binary in a batch of W writes is hashed W + 9 times: an 8-write adoption chunk
hashes a 105 MiB file 17 times (1,785 MiB). On a cell whose page cache cannot
hold the file, each pass is a disk read, so large recordings and video cannot
be filed within one call's time budget. Remote and local clients must behave
the same, and this cost is what holds the remote large-file ceiling down.

The same code has a tamper seam. The copy from a stage into its destination
does not check the bytes it copies. If a stage changes after its last check,
the changed bytes are installed. The batch then fails to bind the installed
file, cannot remove it, and ends in `BATCH_ROLLBACK_INCOMPLETE` with the
changed bytes left in the vault.

## What Changes

- Hash the bytes while copying a stage into its destination, and refuse the
  installation when the digest differs from the planned digest. Nothing is
  installed for that stage and the batch rolls back completely.
- Revalidate a binary stage and an installed binary destination by exact file
  identity and a trusted change witness (size, modification time, change time)
  instead of rereading the bytes. Platforms and routes without a trusted
  witness keep the content check.
- Remove duplicate rechecks of installed destinations.
- Stream the exact content checks that rollback and post-error binding still
  need, so no path holds a whole binary in memory.

## Capabilities

### Modified Capabilities

- `transactional-vault-writes`: the batch verifies bytes at the copy into the
  destination, and may use a trusted change witness instead of rereading binary
  bytes. The accepted residual risk is a same-size rewrite within one timestamp
  tick of the last witness.

## Impact

- `src/exomem/vault.py`: `_WorkspaceArtifact`, `_BatchArtifactGuard`,
  `_BatchWorkspace.create_stream_artifact`, `replace_artifact`,
  `bind_installed_after_error`, `unlink_installed`, and the batch commit loop.
- No public API, envelope, or stored format changes. Callers that write
  binaries (`add`, `preserve`, `scene_frames`, adoption filing) get faster
  writes. Notes keep their current checks.
