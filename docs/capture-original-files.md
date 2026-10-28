<!-- authority:non-specification -->

# Capture an original file

Use original bytes for documents, transcripts and other material that an agent did not author.
Retyping an original can change it silently. A SHA-256 assertion makes an altered copy fail before capture.

## HTTP MCP clients

1. Compute the file's SHA-256 in the client process; retain the digest.
2. Call `transfer_artifact` with `operation="upload"`, `lane="source"`, `hold=true` and `expected_sha256`; receive a handoff.
3. Save the returned handoff JSON to a local file; keep its short-lived token private.
4. Run `exomem attach original.txt --handoff-file handoff.json --root .`; receive a held file handle.
5. Call `capture_source` with the returned `file` in `files`, a title, source kind and digest; receive the custody receipt.

The upload handoff uses the current MCP authorization. It needs no separate local client token.
The CLI reads the original under the selected root. The service receives bytes and cannot select a client filesystem path.
An upload only holds bytes temporarily. The later capture receipt proves that the Source was stored.

Handoffs expire within fifteen minutes and permit one upload. The byte limit is at most 100 MiB, or a smaller deployment limit.
The issuing authorization must remain active. Another session cannot redeem its held file.
After an upload loses its reply, obtain a fresh handoff; the unused hold expires.

## Sandbox clients

A chat client with a code sandbox and no local `exomem` install can use the same handoff on a personal server or on Exomem Cloud.
This covers claude.ai, Claude desktop chat and Cowork. It needs the user's opt-in: code execution on, and the Exomem host allowed for network access.
It needs no user token.

1. Compute the file's SHA-256 in the sandbox, for example with `sha256sum original.txt`; retain the digest.
2. Call `transfer_artifact` with `operation="upload"`, `lane="source"`, `hold=true` and `expected_sha256`; receive a handoff.
3. Save the returned handoff JSON to `handoff.json` in the sandbox; keep its token private.
4. Run `pip install exomem`, then `exomem attach original.txt --handoff-file handoff.json --root .`; receive a held file handle.
5. If the sandbox cannot run that command, send the fallback request below instead; receive the same reply.
6. Call `capture_source` with the returned `file` in `files`, a title, source kind and digest; receive the custody receipt.

The fallback request is one multipart `POST` to the handoff's `upload_url`.
It carries the header `Authorization: Bearer <token>` and the form fields `file`, `hold=1` and `lane` (the handoff's lane).
Do not add other fields such as `scope` or `category`; the service refuses them. An installed CLI that predates `--handoff-file` needs the fallback request.
Check the reply before step 6: `state` is `held`, and `hash` and `size` match the file.

The client sets its own attachment limit; use that limit. The handoff's `max_bytes` is the service limit.
A pasted inline image never reaches the sandbox.

## Other clients

Existing client file handles work with `capture_source(files=[...], expected_sha256=...)`, including Cloud handles.
An assertion binds exactly one file. Without files, it describes the exact supplied UTF-8 `content`, including whitespace and line endings.
It never describes the rendered Source page.

Standalone stdio has no verified HTTP session and cannot issue upload handoffs.
Connect through personal HTTP MCP or supply a supported client file handle.
These clients can still capture an original through a supported client file handle.

## Exomem Cloud

A Cloud cell configured for gateway upload handoffs uses the same steps and descriptor fields.
Its token starts with `exomem-cloud-upload:v1:`, and its `upload_url` is the Cloud gateway's `/upload` endpoint.
The gateway checks that the issuing login is still current before it reads any bytes.
Only that login can capture the held file. Treat the token as private, as on a personal server.

On a cell without this configuration, `transfer_artifact` is not offered. Use a supported client file handle instead.
On Cloud, `transfer_artifact` prepares held uploads only. It does not issue download links.

## Correct an earlier Source

1. Read the predecessor with `follow_corrections=false`; retain its canonical reference and `content_hash`.
2. Capture the corrected original with `correction={"supersedes": reference, "expected_hash": hash, "reason": reason}`; receive its new identity.
3. Read the earlier reference normally; receive the current Source and its resolution envelope.

The earlier Source and original remain unchanged. A correction adds a new Source and an immutable receipt.
Compiled citations retain their authored identities; current reads and compilation resolve the correction chain.
Use `follow_corrections=false` to inspect a historical page. Exact unit reads and explicit artifact downloads remain exact.
Correct the current successor to reverse a correction. Never overwrite or delete the earlier original or its correction receipt.
