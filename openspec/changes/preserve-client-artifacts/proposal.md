## Why

Exomem's binary preservation contract assumes the client runtime can upload bytes to `/upload`, which permanently strands attachments in clients such as ChatGPT whose file-owning sandbox has no network egress. OpenAI now exposes attachments to MCP tools as temporary file handles, so Exomem can preserve those bytes server-side without base64 or client-side `curl`.

## What Changes

- Add one client-neutral `preserve_artifacts` command that accepts a batch of temporary remote file handles and preserves them through Exomem's existing append-only Evidence pipeline.
- Advertise the command's `files` field through OpenAI's `openai/fileParams` tool metadata so ChatGPT can supply attached files in one MCP call.
- Stream downloads with HTTPS-only, redirect, DNS/IP, timeout, and byte-count protections before committing under the existing vault mutation boundary.
- Return an explicit result for every requested file, including stored path, size, SHA-256, media ID, or stable failure details; successful files remain truthful when another item fails.
- Keep `transfer_artifact` and `/upload` as the compatibility transport for clients, including Claude, that cannot expose attachment handles to an MCP call.
- Update bootstrap, tool descriptions, and the generic skill scaffold to route by client capability instead of promising sandbox egress.

## Capabilities

### New Capabilities

- `client-artifact-preservation`: Direct, governed preservation of client-provided temporary file handles with bounded server-side retrieval and batch result reporting.

### Modified Capabilities

- `command-surface`: Generated MCP tools carry command-specific metadata, and artifact-preservation guidance exposes one canonical capability with transport-specific fallbacks.

## Impact

The command registry, FastMCP registration, preservation/transfer helpers, bootstrap guidance, scaffold documentation, tool-schema fixtures, and upload/security tests change. The implementation uses pinned standard-library HTTPS retrieval and the existing `preserve_stream` sink; it adds no model or reasoning service and does not change `/upload` compatibility.
