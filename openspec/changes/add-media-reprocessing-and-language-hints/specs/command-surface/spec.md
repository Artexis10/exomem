## ADDED Requirements

### Requirement: Process Media Offers An Explicit Reprocess Operation

The single `process_media` registry entry, and the `change_media` operation that wraps it, SHALL accept `operation=reprocess` with a required path or paths and an optional `language` code. MCP, `/api/process_media`, OpenAPI and the CLI SHALL expose it through the same leaf and return the shared envelope. It SHALL run under the same per-artifact commit guard, writer-lease classification and egress classification as `process` and `retry`. `language` SHALL be refused on any other operation, and on media that has no speech stage.

#### Scenario: Reprocess appears on every surface

- **WHEN** the command registry is built
- **THEN** `reprocess` and `language` appear for `process_media` in MCP, REST, OpenAPI and CLI without per-surface business logic

#### Scenario: Reprocess requires a path

- **WHEN** a caller invokes `reprocess` without a path
- **THEN** the call fails with an actionable error and queues nothing

#### Scenario: A language on another operation is refused

- **WHEN** a caller passes `language` with `operation=retry`
- **THEN** the call fails with an actionable error
