## Why

Exomem's best behavior currently lives partly in the Claude Skill, so generic MCP
clients see tools but not the operating contract that makes those tools reliable.
ChatGPT/Codex/Cursor-style usage showed that Exomem should teach agents how to use
itself through MCP, while keeping Claude Skills as the richer native UX.

## What Changes

- Add a read-only `bootstrap` operation that returns a versioned, deterministic
  agent contract for generic MCP clients.
- Include workflow guidance, preferred tool defaults, retry/search guidance,
  performance profiles, and current compute policy in the bootstrap payload.
- Extend `/upload` success responses with structured metadata so agents can report
  exactly what was stored.
- Extend opt-in `find` timing diagnostics with compact request/profile metadata so
  performance discussions distinguish retrieval knobs from compute mode.
- Update user-facing docs to say Claude Skills are best UX, while generic MCP
  clients should call `bootstrap()` first.

## Capabilities

### New Capabilities
- `agent-bootstrap-contract`: A versioned MCP-visible operating contract that lets
  non-Skill clients become Exomem-native after one read-only call.

### Modified Capabilities
- `command-surface`: Expose `bootstrap` consistently through MCP, REST, CLI, and
  OpenAPI from the command registry.
- `find-recall-efficiency`: Add profile/policy metadata to opt-in timing diagnostics
  without changing default `find` response shape or retrieval behavior.

## Impact

- Affected code: command registry, server upload route, preserve streaming result
  metadata, find timing serialization, CLI/REST generated surfaces.
- Public APIs: new `bootstrap` tool/REST/CLI command; additive fields in `/upload`
  success JSON and `find(include_timings=true).timings`.
- Docs: README/Quickstart generic-client guidance.
- No new model path or reasoning dependency; this is deterministic instruction and
  measurement metadata under the pure-substrate constraint.
