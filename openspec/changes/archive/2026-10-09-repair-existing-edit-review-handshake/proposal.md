## Why

Existing governed pages can become permanently unwritable when a stale reviewed-none relation disposition forces a validate-then-commit handshake. The handshake is currently non-deterministic in some edit leaves and ambiguous to generic MCP clients, so neither documented recovery path is reliably usable.

## What Changes

- Make every existing-page edit kind validate and commit the exact same normalized bytes, including the server-owned `updated:` stamp.
- Return the required relation review fingerprint as `relation_review_hash` and let validation return that value even when review intent is already present.
- Keep remediation text, the `edit_memory` schema, full bootstrap guidance, and runtime behavior aligned.
- Document the accepted note-level typed-relation syntax with a copy-pasteable example and distinguish it from unsupported Dataview inline fields.
- Remove the misleading `(missing: ['semantic'])` suffix from semantic transition errors.
- Add cross-kind regressions for stale, current, and missing `updated:` frontmatter and an end-to-end Aberdeen-style append.

## Capabilities

### New Capabilities

- `existing-edit-review-handshake`: Deterministic validation and commit of existing-page edits, including the explicit reviewed-none relation round-trip.

### Modified Capabilities

- `command-surface`: `edit_memory` remediation, documentation, response fields, and errors must match the fields and behavior clients can actually use.
- `agent-bootstrap-contract`: Full bootstrap must teach the existing-edit review round-trip and accepted note-level typed-relation syntax with concrete examples.

## Impact

This changes the semantic preflight response, existing-page transition-token handling, `edit_memory` error rendering and documentation, and full bootstrap guidance. The implementation affects the shared edit leaf plus the separate batch and frontmatter writers; existing transition tokens remain compatible because the reviewed stamp is already optional in the current token format. No migration, model, optional dependency, or background processing is introduced.
