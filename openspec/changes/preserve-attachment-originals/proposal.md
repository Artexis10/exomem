## Why

When a user hands an AI client a file that becomes evidence (a photo of a letter, a
scanned form, a recording), the original bytes are the record and a transcription is
derived from them. A dogfood incident showed the opposite outcome: a chat client
transcribed attached images into notes and never preserved the images, so the derived
text survived and the proof did not.

Exomem already has a file-handle path (`preserve_artifacts`), but three gaps let the
failure recur:

- `capture_source` accepts the same handles but does not declare them as OpenAI file
  parameters, so a ChatGPT attachment routed to the Sources lane has no handle to pass.
- A same-machine client (Claude Code, Codex CLI) holds the attachment as a local file.
  `exomem attach` can send its bytes to the local listener, but the tool calls cannot
  consume the result: they accept only an HTTPS `download_url`, and a local path
  argument is ruled out because a prompt-injected model could read any file the service
  can.
- Nothing lets a transcription travel with the original, so an agent that has already
  transcribed a file saves the text and moves on.

## What Changes

- `capture_source` declares `_meta["openai/fileParams"] == ["files"]`, like
  `preserve_artifacts`.
- `/upload` on local ingress can hold bytes instead of preserving them (`hold=1`, with a
  lane). It returns a file handle whose `download_url` is an opaque
  `exomem-held:<secret>` reference. `preserve_artifacts` and `capture_source` redeem it
  without any HTTPS round trip. A held upload is bound to the local client session that
  sent it and to its lane, is redeemable once, and expires after one hour. An unknown,
  foreign, expired or spent handle fails with one indistinguishable code.
- `exomem attach <file>` without `--scope`/`--category` holds the file and prints the
  handle; `--lane source` holds it for `capture_source`.
- `preserve_artifacts` takes optional `transcriptions` (`{file_id, text}`). A
  transcription is written only with its original, on the original's Evidence page, marked
  `extracted_by: client-transcription` and bound to the original's SHA-256, size and
  content type. When the original is not stored, no transcription is written.
- Bootstrap, tool descriptions and the scaffold skill say to preserve the original first,
  name each client's path, and say plainly where a client cannot pass bytes.

No tool argument names a local path.

## Capabilities

### Modified Capabilities

- `client-artifact-preservation`: file parameters on both file-handle commands, held
  uploads for local clients, transcriptions recorded with their original, and
  custody-first guidance.

## Impact

- New `src/exomem/held_uploads.py`; `client_artifacts.py`, `preserve.py`,
  `mutation_terminal.py`, `commands.py`, `server_transfer.py` (additive hold branch),
  `__main__.py` (`attach`), the scaffold skill, bootstrap guidance, and the regenerated
  tool-surface artifacts.
- The local-ingress security model is unchanged: the hold branch runs only under a
  verified local grant, adds no route, and grants nothing a local client cannot already
  do.
- Held bytes live in the vault's private state directory, never in the vault, until a
  command commits them through the existing Evidence or Sources writer.
- claude.ai remote connectors expose no file-handle argument. Their path stays
  `transfer_artifact` plus `/upload` from code execution, which needs the user's opt-in
  (network access to the Exomem host); guidance says so rather than implying custody.
