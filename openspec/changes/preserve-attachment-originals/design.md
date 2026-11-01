## Context

`preserve_artifacts` and `capture_source` stage client file handles through one bounded,
hostile-input fetcher, then commit each staged file under the vault mutation boundary.
The handle is a four-field object (`download_url`, `file_id`, `mime_type`, `file_name`)
that matches OpenAI's file-parameter contract, so ChatGPT can fill it.

Local clients have their own authenticated door: the supervisor's loopback listener
stamps requests, a worker verifies a per-client local token, and `/upload` accepts that
token and preserves straight to Evidence. The upload response is not something a tool
call can consume.

## Goals / Non-Goals

**Goals**

- One handle shape across clients, so guidance says "pass the handle" everywhere.
- Local clients get Evidence-first custody through the same commands, without an HTTPS
  round trip and without any path argument.
- A transcription can only be recorded together with its original.
- Any supported client can recover a permitted original from canonical storage,
  even when its external source is unavailable.

**Non-Goals**

- A claude.ai file-handle path. The remote connector exposes none; code execution plus
  `/upload` remains the documented, opt-in route.
- Holding uploads on the public path. A public upload credential carries no identity to
  bind a held upload to.
- Transcriptions for the Sources lane or for adoption calls.

## Decisions

- **D1. The held handle rides in `download_url`.** A held upload's handle is
  `{download_url: "exomem-held:<secret>", file_id, mime_type, file_name}`. The file
  object keeps its exact four fields, so neither the OpenAI file-parameter contract nor
  the REST, OpenAPI or CLI validators change. `stage_artifact` redeems the reference
  before any network code runs; every other `download_url` still goes through the HTTPS
  fetcher. `file_id` is `held-` plus a digest prefix of the secret, never the secret,
  because file ids are echoed into results and adoption receipts.
- **D2. Binding and lane.** A hold is recorded under the SHA-256 of its secret with the
  local session id that sent it (`mcp-local:<session>`) and its lane (`evidence` or
  `source`). Redemption requires a verified local grant for the same session and a
  command of the same lane: `preserve_artifacts` for `evidence`, `capture_source` for
  `source`. The lane is fixed when the bytes are sent, as for minted upload tokens.
- **D3. Withheld is absent.** Unknown, malformed, foreign-session, expired and spent
  handles all fail with `HELD_UPLOAD_UNAVAILABLE` and one reason. A lane mismatch is
  reported distinctly (`HELD_UPLOAD_LANE`) only after the session matched, since the
  caller then already owns the hold. Checks run before the claim, so a refused
  redemption consumes nothing.
- **D4. Once, within an hour.** Redemption claims the held bytes with an atomic rename,
  so exactly one caller wins. The claimed bytes are re-hashed before use and refused if
  they no longer match the hold. The claim is final: if the later commit fails, the file
  is sent again (`exomem attach` is cheap and local). Expired holds are swept when a new
  hold is made. Holds live in `<vault state dir>/held-uploads/` (0700 directory, 0600
  files), never in the vault.
- **D5. Holding is additive to `/upload`.** A multipart `hold=1` field on local ingress
  streams the part into the hold store under the same byte cap and returns the handle.
  No route, credential or door rule changes; on any other ingress `hold=1` is refused
  with `HELD_UPLOAD_LOCAL_ONLY`, and the existing direct-preserve path is untouched.
- **D6. Transcriptions go on the original's page.** `preserve_artifacts(transcriptions=
  [{file_id, text}])` passes the text to the existing Evidence writer as the artifact's
  extracted text, so the binary and its transcription commit in one write and the text
  makes the original findable. The page records `extracted_by: client-transcription` and
  a `transcription` block with the original's SHA-256, size and content type. A
  transcription naming no supplied file is refused before anything is staged. An
  `already_stored` original keeps its page unchanged and the row says the transcription
  was not recorded; a failed original records nothing. Conclusions drawn from the
  original stay a separate step: `remember(sources=[...])` naming the returned page.
- **D7. Both commands declare file parameters.** `capture_source` gains the same
  `openai/fileParams` metadata `preserve_artifacts` already has, so either lane can
  receive a ChatGPT attachment.
- **D8. Recover through the existing custody and transfer owners.** Resolve the
  selected original to its canonical stored artifact and verified content hash.
  Prefer a documented native file interface that the client can consume. Otherwise
  use authenticated HTTPS or a short-lived grant for that artifact version.
  Keep direct attachments, resource references and readable URLs distinct until
  the actual client proves consumption. Do not create another artifact store.
  The fallback must open in a normal browser without a hidden bearer header.
  Apply this path to all stored original media, including audio and video;
  bounded HTTPS handles files that cannot fit the native client payload.
  Existing note reads and exports remain separate from original-media recovery.
- **D9. Preserve authority when bytes leave storage.** New artifact grants bind
  the exact artifact version, issuing principal, session and expiry. Existing
  read permission alone does not authorize external disclosure. Reuse the live
  full-byte release and session checks before transfer. Reject missing, expired,
  revoked, foreign or mismatched authority without an existence oracle. Keep
  credentials out of logs and referrers. An existing authenticated client needs
  no separate account; a bearer grant grants only its named artifact.
- **D10. Match delivery to the actual deployment.** Desktop transfer already
  serves governed bytes. Cloud needs a confined authenticated outbound route;
  its inbound broker and file-preservation proof do not establish that route.
  Reuse the existing gateway, cell identity and deployment owners. Do not give
  a cell public ingress, general egress or cross-cell artifact access.
- **D11. Reuse the retained copy owner for bounded delivery.** Copy the selected
  original through `HeldFilesystem.copy` into a private temporary directory,
  then hash that copy in chunks. Keep `GenericFileSnapshot.data` as bytes; one
  owner-produced `GenericFileCopy` carries the verified source binding, digest
  and size. Thread it through the existing connector, companion, raw-protection,
  grant, decision and disclosure-receipt owners. Parse current companion Markdown
  as before. Do not reread the whole original behind a streaming response.
  Recheck canonical identity and bytes, session, policy and full release before
  headers. Hold no global writer boundary during copying or delivery. Clean the
  private copy in `finally` on success, refusal, disconnect and cancellation.
  The existing copy owner accepts optional byte and deadline bounds. It checks
  them between chunks on POSIX and Windows; an active OS syscall can finish
  after its deadline. Recovery copies at most the observed source size, reuses
  the request budget, and admits temporary bytes against available disk space.
  Two concurrent spools bound process disk use. Native responses default to
  4 MiB before base64; `EXOMEM_RECOVERY_NATIVE_MAX_BYTES` owns that payload budget.
  Older custody pages may lack stored MIME. The descriptor identifies filename
  inference or the generic fallback explicitly instead of claiming stored MIME.
  Amended (decided on 2026-11-01 under the operator's standing rule for
  reversible choices; the operator may revert it): downloads no longer copy the
  original. The request holds the original open and serves the response from that
  handle, with no private copy and no disk-space admission. The two request slots
  still bound the work. The contract for the digest, the cache identity and the
  stop on a change is in `openspec/changes/add-cloud-media-store/specs/cloud-media-store/spec.md`.
  The private copy and its spool remain only for the parent video of a scene frame.
- **D12. Keep native identity and delegated browser authority distinct.** Native
  resource reads match the caller's audience, client and origin session before
  restoring the signed issuing authorization context. Browser possession delegates
  only the named artifact version under its issuing principal; it does not prove
  that the holder is the issuing human. Another browser identity cannot widen
  that authority. Existing broad transfer tokens remain header-only. New links
  retain expiry and current revocation checks on every request; already sent
  bytes cannot be recalled. Responses use attachment disposition, `no-store`,
  `no-referrer` and `nosniff`; deployed logs must omit the capability query.
  Native admission carries the verified grant beside a sanitized dispatch URI,
  so framework resource spans do not retain capability bytes. Application log
  filters remove signed capabilities from framework errors and access records.
  Access filtering parses the reserved grant query field, including encoded keys
  and values, before formatting the request target.

The final release checks the canonical file version before and after the live
disclosure decision. POSIX change time and Windows `ChangeTime` detect ordinary
in-place updates, including writes that restore the modification time.
These witnesses do not resist a hostile host that can forge file metadata.
Scene frames reuse only request-owned parent byte proof; their metadata and
disclosure decisions remain live. The final pass refuses missing or changed
parent proof instead of copying or hashing the parent on the event loop.
If task cancellation interrupts stage entry, the worker retains cleanup ownership
until entry finishes within the existing copy budget.

## Risks / Trade-offs

- A held upload occupies disk for up to an hour. It is owner-only, bounded by the upload
  cap, and swept on the next hold.
- A same-user process that can read the local token file can redeem that client's holds.
  That matches the local-ingress model: tokens give attribution and revocation, not
  isolation.
- If an MCP client connects through a different credential than `exomem attach` uses, its
  redemption is refused. Guidance tells local clients to use one local token for both, or
  to preserve directly with `exomem attach --scope --category`.
- A bearer download grant permits its holder to retrieve the named artifact
  during its lifetime. Exact version binding and live session checks limit that
  authority; they do not make a leaked grant harmless. Native and URL paths
  must retain the same full-byte disclosure decision.

## Original recovery acceptance

Use invented binary inputs in isolated state. Remove the external source after
preservation, then recover the canonical original through each supported delivery
path. Verify its SHA-256, size, media identity and successful client consumption.
Create a derivative and preserve it through the existing writer with provenance
to the original. Test expiry, session revocation, foreign principals and a changed
artifact version. Retain unsupported and unverified client paths explicitly.

This contract prevents loss of access and release beyond the selected artifact.
An incorrect refusal blocks one download, so transient delivery failures must
leave stored bytes intact and return a bounded actionable result. The transfer
owner carries that recovery cost; the user does not approve each routine retry.

The desktop transfer and native resource batch precedes the separate Cloud
gateway/runtime integration. Cloud must revalidate the existing originating
OAuth binding and current cell mapping; a bare cell-owner identity is insufficient.
Neither stage claims another client's native attachment support without proof.
