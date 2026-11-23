# Proposal

## Why

A Source upload that starts with `upload_files` uses a single multipart request. That request cannot carry a larger original or resume after a connection drops.

## What Changes

- Offer a resumable Source handoff when the requested file exceeds the one-shot limit.
- Reuse the existing tus session size limit and Source commit. A valid handoff belongs to its issuing owner, MCP session and deployment.
- Keep the existing handoff for smaller requests, Evidence and imports. A connection that cannot serve a resumable Source handoff reports that limit before it receives bytes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `client-artifact-preservation` gains an authenticated resumable Source handoff through `upload_files`.

## Impact

The change affects the upload descriptor, the verified personal upload route and the hosted size refusal, and the client instructions for large Source files. It uses the existing session storage and governed commit. It adds no model or external storage.
