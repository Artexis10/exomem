# Upload folders over MCP

## Why

A client without a shell cannot import a folder. `exomem import <folder>` needs a shell on the vault's host, and `imports.file` takes a staged path that only that command or the Cloud import creates. No client can import an archive at all.

An agent can already send bytes: `upload_files` hands it a one-use upload that the server holds for the session. The missing step is a way for the held bytes to become a staged folder that `imports.file` already files, with its dedupe and its one-sentence summary.

## What changes

- **`upload_files` takes the lane `import`.** The reply keeps its descriptor and adds `next`: the multipart POST that sends the archive, as a ready `curl` line, and the follow-up call, `manage_memory` `imports.file` with `{"upload": "<token>"}`. The lane implies a held upload and never charges the media allowance, because the archive is unpacked and released, not stored.
- **`imports.file` takes `upload` as an alternative to `path`.** The first call unpacks the held tar or zip into a new `_Imports/<id>`, writes the stage record that `exomem import` writes, and goes on as `file` does. The reply names the `path`. A repeated call, with the same `upload` or with the `path`, carries on the same import. `upload` with `path` is refused.
- **One module unpacks the archive safely.** It reuses the stdlib-only tar unpacker of the Cloud import and the zip member rules of the archive preserver, and refuses each unsafe member by name.
- **Guidance.** `read_instructions` for `upload_files` and `imports.file`, the import guide, the operations reference and the `exomem-ingest` skill lead with this route for a client without a shell. A client with a shell keeps `exomem import <folder>`.
- **Setting.** `EXOMEM_IMPORT_MAX_EXPANDED_BYTES` bounds the bytes that one archive unpacks to. It defaults to 1 GiB.

## Capabilities

### Modified Capabilities

- `adoption-studio`: `file` takes an uploaded archive as the staged folder.
- `client-artifact-preservation`: the held-upload handoff has a lane whose bytes are unpacked into a stage and released.

## Impact

- **Code:** `import_upload.py` (new), `upload_handoff.py`, `held_uploads.py`, `server_transfer.py`, `media_store.py`, `cloud_import.py` and `archive_members.py` (the refusing member's name), `commands.py`, `operation_entries.py`, `public_tools.py`, `public_surface.py`, the skill scaffold references and the generated tool-schema, skill and plugin artifacts.
- **Tool surface:** one enum value on `upload_files.lane` and one `upload` argument on the `imports.file` door operation. The schema budget does not rise.
- **Out of scope:** an upload page for browsers, ChatGPT archive pulls and the Exomem Cloud route. Cloud keeps its own import path until a later change gives the gateway the lane.
