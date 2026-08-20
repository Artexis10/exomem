## ADDED Requirements

### Requirement: Widened profiles inherit registry mutation guardrails unchanged

When a Hosted surface profile is widened to expose page-level mutation commands, the gateway SHALL forward those commands with registry-identical names, input schemas, read/write metadata, result envelopes, and stable error codes, exactly as it does for every other registry command. The gateway MUST NOT fork a mutation command's discovery schema per surface, add a profile-local argument coercion or allowlist, or relax the cell's proposal-first and mutation-boundary checks. Supersession integrity, relation review, stale-write rejection, and the vault mutation boundary MUST remain enforced by the cell's command leaf rather than by the forwarding layer.

#### Scenario: A supersession is forwarded through a widened profile

- **WHEN** a routed caller invokes `replace_memory` on a Hosted profile that exposes it
- **THEN** the gateway forwards the canonical command name and arguments to the mapped cell without a profile-local schema
- **AND** the cell requires the replacement to carry its bound predecessor path and predecessor content hash before committing

#### Scenario: A revision commits without a fresh relation review

- **WHEN** a forwarded mutation supplies a relation review whose hash does not match a fresh validation pass, or supplies no approval where one is required
- **THEN** the cell rejects it with its stable relation-review error code before mutating governed content
- **AND** the gateway preserves that code and envelope rather than substituting surface-specific handling

#### Scenario: The same mutation command is compared across surfaces

- **WHEN** a mutation command exposed by a Hosted profile is compared with the same command on the cell's own MCP and REST surfaces
- **THEN** its serialized name, input schema, annotations, and read/write classification are identical
- **AND** no narrowed or widened per-surface variant of that command exists
