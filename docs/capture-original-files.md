<!-- authority:non-specification -->

# Capture an original file

Use original bytes for documents, transcripts and other material that an agent did not author.
Retyping an original can change it silently. A SHA-256 assertion makes an altered copy fail before capture.

## ChatGPT attachments

ChatGPT supplies file handles to `save_source` and `save_evidence` through the top-level `files` argument.
Each handle requires `download_url` and `file_id`; `mime_type` and `file_name` are optional.
The `file_id` correlates the attachment with its receipt. The ID alone cannot fetch bytes; an HTTPS `download_url` provides them.

Exomem preserves the bytes that the client supplies. A rendered preview or screenshot does not prove custody of the camera original.
Use the original file through a supported handle or the CLI and HTTP upload steps below.
Issuing an upload handoff only permits a transfer; it does not transfer or preserve the file.
Verify the capture receipt and stored SHA-256 before treating the original as preserved.

## HTTP MCP clients

1. Compute the file's SHA-256 in the client process; retain the digest.
2. Call `upload_files` with `operation="upload"`, `lane="source"`, `hold=true` and `expected_sha256`; receive a handoff.
3. Save the returned handoff JSON to a local file; keep its short-lived token private.
4. Run `exomem attach original.txt --handoff-file handoff.json --root .`; receive a held file handle.
5. Call `save_source` with the returned `file` in `files`, a title, source kind and digest; receive the custody receipt.

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
2. Call `upload_files` with `operation="upload"`, `lane="source"`, `hold=true` and `expected_sha256`; receive a handoff.
3. Save the returned handoff JSON to `handoff.json` in the sandbox; keep its token private.
4. Run `pip install exomem`, then `exomem attach original.txt --handoff-file handoff.json --root .`; receive a held file handle.
5. If the sandbox cannot run that command, send the fallback request below instead; receive the same reply.
6. Call `save_source` with the returned `file` in `files`, a title, source kind and digest; receive the custody receipt.

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

On a cell without this configuration, `upload_files` is not offered. Use a supported client file handle instead.
On Cloud, `upload_files` prepares held uploads only. It does not issue download links.

## Signed document containers

Capture an ASiC-E `.asice` file through the same original-file interfaces.
Exomem preserves the container unchanged and gives each supported DOCX or PDF payload its own unchanged original and searchable companion.
The initial container and payloads publish together. Each payload records the container's path and SHA-256, its member path, and its own SHA-256 and size.
Use `transfer_artifact(operation="download", artifact=path)` to retrieve each exact original through the existing download interface.

Structural inspection does not verify signatures, certificates, signer identity or trust.
The companion and capture receipt report `signature_verification: not_verified`, including when signature material is malformed.
Document extraction uses the existing media jobs and complete text parts. Missing extraction software leaves the originals intact with an actionable processing state.

Unsafe archives, corrupt members, unsupported payload types or exceeded limits retain the unchanged container without publishing partial payloads.
The `document_container` result reports `inspection_state`, a reason and `next_action`.
A failed inspection reports an unknown supported-payload count, never a claim that the archive contains zero documents.
Inspection permits at most 1,024 ZIP entries, 64 document payloads and 256 MiB of expanded bytes, within the shared ZIP limits.

A payload and its text parts inherit the container's current access restrictions.
Changing the container's restrictions also changes payload search, reads and original downloads.
Explicit raw protection covers the entire payload subtree.

1. For an older opaque container, call `manage_memory(operation="media.process", arguments={"path": container_path})`; receive payload custody and processing states.
2. After fixing a processing failure, call `manage_memory(operation="media.retry", arguments={"path": container_path})`; reuse the preserved originals.
3. For completed payloads needing new extraction, call `manage_memory(operation="media.reprocess", arguments={"path": payload_path})`; retain previous text until replacement succeeds.

Repeated preservation returns `already_stored` without changing companions or repairing missing payloads.
Explicit processing creates missing payload pairs and can restore missing provenance only when existing bytes match exactly.
Conflicting bytes or provenance require restoration of the exact original or custody companion before processing can continue.
No automatic migration inspects older opaque containers.

## Correct an earlier Source

1. Read the predecessor with `follow_corrections=false`; retain its canonical reference and `content_hash`.
2. Capture the corrected original with `correction={"supersedes": reference, "expected_hash": hash, "reason": reason}`; receive its new identity.
3. Read the earlier reference normally; receive the current Source and its resolution envelope.

The earlier Source and original remain unchanged. A correction adds a new Source and an immutable receipt.
Compiled citations retain their authored identities; current reads and compilation resolve the correction chain.
Use `follow_corrections=false` to inspect a historical page. Exact unit reads and explicit artifact downloads remain exact.
Correct the current successor to reverse a correction. Never overwrite or delete the earlier original or its correction receipt.
