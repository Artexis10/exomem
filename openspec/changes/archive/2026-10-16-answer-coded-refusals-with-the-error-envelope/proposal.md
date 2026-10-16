## Why

The MCP wrapper answered only an `OpError` and a semantic refusal with the
`success:false` error envelope. Every other coded leaf refusal raised a
native tool error: a "CODE: reason" error that a leaf re-wraps, and a
committed batch failure that carries its own public outcome. A client then
saw an untyped tool error for a refusal that has a stable code. For
`BATCH_CLEANUP_INCOMPLETE` it also lost the "committed, do not retry"
outcome. REST and CLI `--json` already answer these refusals with the
envelope.

## What Changes

- The MCP wrapper answers every coded leaf refusal with the error envelope:
  an `OpError`, a semantic refusal, an error with a public outcome, and an
  error that follows the "CODE: reason" convention.
- An error without a code stays a native MCP execution error.
- Lost authorization stays the content-free native tool error that the
  authorization middleware raises.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: the MCP application-envelope contract covers every
  coded leaf refusal, not only an `OpError`.

## Impact

The change affects the generated MCP wrapper and one shared helper in
`cli_ops`. MCP clients that read `isError` to find a refused call must also
read `success:false`. The Claude Code capture hook is updated to do so.
REST, CLI and the replay cache for committed failures keep their behavior.
