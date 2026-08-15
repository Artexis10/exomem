## ADDED Requirements

### Requirement: Observe Memory Accepts Governed Unit Metadata
The single command registry SHALL expose `verdict`, `check_by`, and `id` on `observe_memory` consistently across MCP, REST, CLI, OpenAPI, and generated capability documentation. `verdict` and `check_by` SHALL require an explicit governed non-observation kind, because the compact form carries no metadata rows, and SHALL be refused with a stable machine-readable code otherwise. `id` SHALL set the unit's authored anchor, SHALL be validated against the existing anchor grammar, and SHALL be refused when it would collide with another unit's anchor on the same parent.

#### Scenario: Governed metadata reaches the rendered rich unit
- **WHEN** a caller adds a unit with kind `prediction`, `verdict` `refuted`, and `check_by` `2026-11-01`
- **THEN** the written rich block carries both metadata rows and the returned unit reports both values

#### Scenario: Governed metadata on a compact observation is refused
- **WHEN** a caller adds a unit with a `verdict` and no explicit governed non-observation kind
- **THEN** the call fails with a stable code stating that governed unit metadata requires the rich form

#### Scenario: An explicit anchor is honoured
- **WHEN** a caller adds a unit with `id` set to a valid anchor
- **THEN** the written unit uses that anchor and the returned unit reference ends with it

#### Scenario: A colliding anchor is refused
- **WHEN** a caller adds a unit with `id` set to an anchor another unit on the same page already uses
- **THEN** the call fails with a stable duplicate-anchor code

#### Scenario: The parameter is present on every generated surface
- **WHEN** the generated MCP, REST, CLI, and capability-documentation surfaces for `observe_memory` are inspected
- **THEN** all of them expose the same `verdict`, `check_by`, and `id` parameters

### Requirement: Observe Memory Reconstruction Preserves Unowned Metadata
`observe_memory` SHALL reconstruct a rich unit without discarding any authored leading metadata row it does not itself own. Rows for keys the command owns — category, id, tags, context, relations, verdict, and check_by — SHALL be re-emitted from the resolved arguments; every other authored row SHALL be preserved verbatim. The governed metadata arguments SHALL be preserve-by-default on update, where omitting an argument keeps the current value and passing an empty string clears it. The command's round-trip assertion SHALL cover the preserved rows, so a dropped row fails the write instead of committing silently.

#### Scenario: An unrelated content edit keeps the verdict
- **WHEN** a caller updates only the content of a rich unit that carries `- verdict: refuted`
- **THEN** the rewritten unit still carries that verdict row and the returned unit still reports it

#### Scenario: An unknown authored metadata row survives an update
- **WHEN** a caller updates only the content of a rich unit that carries an authored metadata row the parser does not interpret
- **THEN** the rewritten unit still carries that row verbatim

#### Scenario: An explicit empty value clears a governed key
- **WHEN** a caller updates a rich unit carrying `- verdict: refuted` and passes `verdict` as an empty string
- **THEN** the rewritten unit carries no verdict row

#### Scenario: A supplied value replaces the current one
- **WHEN** a caller updates a rich unit carrying `- verdict: inconclusive` and passes `verdict` as `confirmed`
- **THEN** the rewritten unit carries `- verdict: confirmed`
