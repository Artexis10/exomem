# Design

## Context

The personal HTTP listener already serves tus sessions and a replayable Source commit. Its legacy bearer-upload commit runs with transfer authority. A verified MCP upload ticket also carries the issuing login and owner.

The Cloud gateway exposes a single `POST /upload` route. It would need its own resumable routing and authorization contract. This change covers the personal HTTP listener.

## Goals

- Let an agent send one large Source through `upload_files` and the existing attach command.
- Keep the original's expected SHA-256 and issuing login bound through upload, retries and publication.
- Reuse the personal session limit, offsets, disk admission, expiry and Source writer.

## Decisions

### Let the tool select the transport from the exact byte count

Add optional `size_bytes` to `upload_files`. A Source with a digest, a size above the actual one-shot limit and a verified personal HTTP listener gets a resumable ticket. A size over the configured session maximum gets a size refusal.

Use the existing session descriptor fields for chunk size, endpoint, protocol version and required metadata. The client needs the advertised fields to create the session and use the server's offset after a disconnect. [tus 1.0 protocol](https://tus.io/protocols/resumable-upload)

The existing hold remains available for files that fit. The hosted ingress reports its multipart limit for a larger request. A request with an import identity stays on the held Source path, which owns import replay. An oversized request with that identity reports the one-shot limit.

### Preserve the ticket authority in the session

Bind the exact size, digest, deployment and issuing principal to one ticket. Its nonce creates one session. Check those facts and validate Source metadata under the issuing principal before accepting bytes.

Keep delegated records in a private namespace that the legacy session parser cannot open. It prevents an old worker from treating a delegated Source as an ordinary transfer upload. Count both namespaces in the existing reservation and session admission. Flush the new session record and directory before returning its location.

Require the session secret and revalidate the bound login for status, resumption and cancellation. Confirmed revocation refuses access. An unreadable login store leaves the transfer retryable.

### Run the existing Source commit as the issuing principal

Reconstruct the issuing principal from the durable record for every attempt. Clear administrative ingress. Recheck the login before staging and at the guarded Source publication check. Reuse the session ID for the existing Source mutation replay.

The existing part file supplies the durable offset. If a stop leaves a full file in receiving state, reconcile it under the session lock before verification. Publish only after the whole file matches the expected SHA-256. Return the existing Source custody receipt once publication settles.

### Keep file transfer outside the tool and writer guard

Reuse the attach client's tus offset and receipt handling. Read chunks from one held local file. Verify the advertised and returned endpoints before sending credentials.

The prepare and status paths handle metadata only. Upload and hashing stream in chunks. The writer guard covers publication and its final authorization check.
The custody reference load is 500,000 originals and 25 TB. Status reads only the session and login records.
The local warmed status target is 250 ms at p95 while the original stays on disk. Test against the listener's configured size and request limits and the existing live-session quota.

## Risks

- A lost creation reply leaves one bounded session until expiry. A spent ticket cannot open a second session.
- A listener restart can interrupt hash or commit work. Test startup settlement and idempotent replay with a complete part.
- The schema surface gains one optional field. Verify the generated descriptors and transport size budget.
