## Why

An agent can silently alter an original when it retypes a tool result into a Source.
Managed HTTP clients also lack a credential-free way to send an original file through their existing MCP session.

## What Changes

- Add a one-use upload handoff that an agent consumes with `exomem attach --handoff-file`.
- Verify caller SHA-256 assertions before text or original-file capture.
- Capture corrected Sources with an explicit, immutable correction chain.
- Resolve Source citations to the current original while retaining explicit historical access.
- Define the same handoff contract for the separate Cloud gateway bridge.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `attachment-source-ingestion`: Admit session-bound uploads and verify intended original bytes.
- `durable-source-capture`: Preserve correction chains and resolve current originals without rewriting earlier captures.

## Impact

The change extends existing capture, transfer and read arguments. It reuses artifact preservation and the held filesystem.
It adds canonical correction receipts and temporary upload tickets. It adds no model, service or package dependency.
Cloud upload remains unavailable until its gateway implements the handoff contract. Existing Cloud file handles receive hash verification now.
