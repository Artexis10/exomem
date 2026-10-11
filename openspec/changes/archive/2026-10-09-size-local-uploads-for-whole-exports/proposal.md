## Why

A large private export, such as a multi-year health-data archive of a few hundred megabytes, must be preserved whole and owner-only before any import. Over local ingress, `/upload` kept the public 100 MB cap, which exists only to match the Cloudflare edge, and `exomem attach` could not ask for raw protection. The existing tools could therefore not preserve such an export.

## What Changes

- On local ingress, a direct preserve or capture uses its own cap, `EXOMEM_LOCAL_UPLOAD_MAX_BYTES` (default 1 GiB), refused early from `Content-Length`. The public path keeps `EXOMEM_UPLOAD_MAX_BYTES`.
- A held upload stays within what its redeeming command can fetch, so a hold never returns a handle that cannot be redeemed.
- `/upload` accepts `raw_protection=1` for a direct preserve or capture. A held upload refuses it, because the command that redeems the hold chooses protection.
- `exomem attach --raw-protection` sends that field with `--scope` and `--category`. A timeout after the bytes are sent reports a missing acknowledgement instead of an unreachable listener.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `local-client-ingress`: `/upload` on local ingress has its own cap for direct preserves and captures and accepts raw protection.

## Impact

`server_transfer.py`, the `exomem attach` command and `docs/deployment.md`. The public upload path keeps its cap and its tokens. It now also refuses a request early when the declared body exceeds the cap plus 1 MiB, before the multipart parser spools it.
