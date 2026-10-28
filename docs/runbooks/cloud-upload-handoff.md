<!-- authority:non-specification -->

# Cloud upload handoff configuration

A Cloud cell can issue the same exact-byte upload handoff as a personal server.
The Substrate gateway signs which OAuth login sent each request, and the cell
verifies that proof with the gateway's public key. This runbook covers the cell
side; the gateway side is in the Substrate Cloud operations runbook. It does not
authorize a live rollout.

## What a configured cell does

- It publishes `transfer_artifact`: `hold=true` uploads, and `download` of a preserved original.
  A download returns the descriptor and a `next_step` command, never a link or blob.
- It serves one private `POST /upload` route. The route requires the cell bearer,
  then a gateway proof bound to the exact ticket, before it claims the ticket.
- It refuses the route while `EXOMEM_CLOUD_READ_ONLY` is set, and leaves the
  ticket unspent.
- It serves one private `GET /download?path=<vault path>` route. The route requires
  the cell bearer, then a gateway proof with `purpose: "download"` bound to the exact
  path, before it resolves anything. Each request, including each range request, has
  its own proof; there is no ticket. It serves a single byte range, and hydrates an
  evicted original through the media store first.
- It keeps every other Cloud exclusion. It serves no REST or resumable upload route.

## Configuration

| Variable | Value |
|---|---|
| `EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY` | The PEM public half of the gateway's Ed25519 artifact signing pair. Set it only on cells selected for uploads. |

The gateway selects the same cells with `EXOMEM_CLOUD_UPLOAD_CELL_IDS`. A cell in
one list and not the other offers no working handoff: keep the two lists equal.
A malformed key stops the cell at startup and names only the variable.
The cell never receives the private half.

The platform chart and cellctl do not yet render this variable. Production
activation adds that rendering for selected cells, the gateway's upload
variables and ingress rules for `POST /upload` and `GET /download` to the gateway.
The edge rule for `GET /download` sets no timeout. The gateway bounds each download stream itself:
an idle bound (default 60 seconds) and a total cap (default 900 seconds).

## Check a selected cell

1. Call `tools/list` through the gateway; expect `transfer_artifact` and no `read_media`.
2. Call `transfer_artifact(operation="upload", lane="source", hold=true, expected_sha256=<digest>)`.
3. Run `exomem attach <file> --handoff-file handoff.json --root <dir>`; expect a held file handle.
4. Call `capture_source` with the returned file and the same digest; expect a stored receipt with that hash.
5. Call `transfer_artifact(operation="download", artifact=<path>)`, run its `next_step` (`exomem cloud download -- '<path>'`, which writes to the current folder and refuses to replace an existing file unless given `--overwrite`); expect JSON with `path`, `bytes` and `sha256`, and a file whose SHA-256 matches.

## Undo

1. Remove the cell from the gateway's `EXOMEM_CLOUD_UPLOAD_CELL_IDS`; new uploads to it then fail.
2. Unset `EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY` on the cell and restart it; `transfer_artifact` is withdrawn.

Existing capture receipts stay valid. Unused holds and tickets expire through
their normal lifetimes.
