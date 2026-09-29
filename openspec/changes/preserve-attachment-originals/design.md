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

## Risks / Trade-offs

- A held upload occupies disk for up to an hour. It is owner-only, bounded by the upload
  cap, and swept on the next hold.
- A same-user process that can read the local token file can redeem that client's holds.
  That matches the local-ingress model: tokens give attribution and revocation, not
  isolation.
- If an MCP client connects through a different credential than `exomem attach` uses, its
  redemption is refused. Guidance tells local clients to use one local token for both, or
  to preserve directly with `exomem attach --scope --category`.
