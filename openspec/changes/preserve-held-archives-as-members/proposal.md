## Why

An MCP client that holds a zip export through `transfer_artifact(hold=true)` can preserve it only as one opaque file. `preserve_artifacts` has no archive option, and `/upload` refuses `archive=members` on a held upload. A collection import reads an export only through the manifest of a members preservation. So a client without a local client token cannot refresh a dataset from an export through the ordinary MCP path.

## What Changes

- `preserve_artifacts` gains one optional field, `archive`, whose one value is `members`. Exomem preserves each supplied held zip exactly as `/upload` with `archive=members` does: one compressed blob per distinct member and one export manifest under `Evidence/<scope>/<category>/`, all owner-only.
- Each handle's outcome keeps `stored`, `already_stored` or `failed`. Its `path` is the export manifest, which a collection import takes as `source_ref`. Its `archive` block reports the archive's SHA-256 and size. A `stored` block adds the member count and the blobs written; an `already_stored` block adds the blobs restored to the pool.
- A handle that is not a held upload fails with `INVALID_FILE` and names the held-upload route. Bytes that are not a readable zip fail with `ARCHIVE_INVALID` and leave the hold redeemable. A call that combines `archive` with `adoption` or `transcriptions` is refused before any handle is staged.

Out of scope: the 100 MiB upload-handoff cap, which `attachment-source-ingestion` fixes, and Exomem Cloud's stopped-cell archive import.

## Capabilities

### Modified Capabilities

- `client-artifact-preservation`: `preserve_artifacts` preserves a held zip as content-addressed members.

## Impact

- Code: `src/exomem/client_artifacts.py`, `src/exomem/commands.py`, and `src/exomem/mutation_terminal.py` so that the compact reply keeps the `archive` block.
- Tool surface: `preserve_artifacts` adds the `archive` parameter. Generated tool schemas, capabilities and the cloud plugin are refreshed.
- Undo: revert the release. Manifests and blobs written through this route are ordinary members preservations and stay readable.
