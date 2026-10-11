## Why

An agent with a shell, such as a coding agent, calls `transfer_artifact(operation="upload")` to send a file to a desktop Exomem. The result names only the single-request `/upload` URL, and the proxy edge caps one request at 100 MB. The desktop already serves resumable upload sessions, but the agent does not learn about them, so a file of several gigabytes cannot get in.

`preserve_artifacts` has the same limit from the other side. It pulls at most 100 MiB per client file handle and per call, and a fixed 20-second deadline stops each pull. A large held upload or a large host-provided download fails, even on a fast link.

## What Changes

- The upload handoff for the Evidence lane adds a compact `resumable` object. It names the session creation URL, the protocol, the header that a client resends, the per-request and per-session byte limits, the `Upload-Metadata` keys that creation requires, and a link to a shell recipe in `docs/deployment.md`. The token that the handoff mints creates a session through the same credential check and lane binding as `/upload`.
- Session creation refuses a request that lacks a required metadata key with one message that names every missing key. The handoff reads the same list of keys, so the two cannot disagree.
- On a standalone server, the per-file and per-call pull caps equal the upload session maximum, `EXOMEM_UPLOAD_SESSION_MAX_BYTES` (default 2 GiB). A held upload is held only up to the same cap, so it stays redeemable.
- On a standalone server, each byte that a pull receives extends the deadline of its file and of its call at a minimum rate of 256 KiB/s. A healthy multi-gigabyte pull finishes, and a stalled or trickling pull still stops.
- The held bytes of one local session together stay within the largest single hold that the listener accepts. This replaces the fixed 256 MiB, which refused any one hold above it. A hold that does not fit in free disk is refused with `HELD_UPLOAD_NO_SPACE` before a byte is copied. The open change `preserve-attachment-originals` holds that requirement, so this change edits its text there.
- The vault's atomic publish copies a new artifact a part at a time. It read the staged file into memory twice over, so one 2 GiB upload or pull took about 4 GiB of RAM.
- Hosted and Cloud cells keep the 100 MiB cap and the fixed deadlines. The artifact broker's grants carry that cap, and those cells serve no upload sessions.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `client-artifact-preservation`: the upload handoff advertises resumable sessions, and a standalone server pulls client file handles up to the upload session maximum under a minimum-rate deadline.

## Impact

- **Changed modules:** `upload_tokens.py` (the handoff), `upload_sessions.py` (the protocol constants and the handoff descriptor), `server_transfer.py` (the creation route and the hold cap), `held_uploads.py` (the held total and the free-disk check), `client_artifacts.py` (the budget and the deadline), `vault.py` and `held_fs.py` with its two backends (the streamed publish).
- **Docs:** `docs/deployment.md` gains the shell recipe that the handoff links to.
- **Tool surface:** no MCP tool description, parameter, skill scaffold or generated plugin surface changes. An Evidence-lane `transfer_artifact` result gains one object of about 300 bytes.
- **Not in this change:** sessions on hosted or Cloud cells, and the full-file read that an adoption replay does to re-verify a stored artifact.
