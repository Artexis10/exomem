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

## Other clients

Existing client file handles work with `capture_source(files=[...], expected_sha256=...)`, including Cloud handles.
An assertion binds exactly one file. Without files, it describes the exact supplied UTF-8 `content`, including whitespace and line endings.
It never describes the rendered Source page.

Standalone stdio without a configured HTTP transfer endpoint cannot use upload handoffs.
Cloud upload handoffs require a gateway bridge; the existing transfer exclusion remains until that bridge is verified.
These clients can still capture an original through a supported client file handle.

## Correct an earlier Source

1. Read the predecessor with `follow_corrections=false`; retain its canonical reference and `content_hash`.
2. Capture the corrected original with `correction={"supersedes": reference, "expected_hash": hash, "reason": reason}`; receive its new identity.
3. Read the earlier reference normally; receive the current Source and its resolution envelope.

The earlier Source and original remain unchanged. A correction adds a new Source and an immutable receipt.
Compiled citations retain their authored identities; current reads and compilation resolve the correction chain.
Use `follow_corrections=false` to inspect a historical page. Exact unit reads and explicit artifact downloads remain exact.
Correct the current successor to reverse a correction. Never overwrite or delete the earlier original or its correction receipt.
