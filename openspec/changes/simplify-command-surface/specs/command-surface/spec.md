## ADDED Requirements

### Requirement: Command Registry Carries Simple Action Metadata
The command registry SHALL expose enough metadata to derive the simple product action catalog without maintaining a separate operation list.

#### Scenario: Action metadata is registry-derived
- **WHEN** the product action catalog is built
- **THEN** it derives command routes from registry metadata
- **AND** canonical commands remain available on their original MCP, REST, and CLI surfaces

#### Scenario: Advanced tools remain discoverable
- **WHEN** a tool is not part of the primary simple action flow
- **THEN** it remains listed as advanced rather than hidden or removed
- **AND** tier-2 and destructive-operation controls continue to apply

### Requirement: CLI Exposes Simple Aliases Without Breaking Canonical Commands
The CLI SHALL expose simple action aliases for common workflows while preserving every canonical registry subcommand.

#### Scenario: Simple alias and canonical command both work
- **WHEN** a user invokes a supported simple CLI action
- **THEN** the action routes to the same canonical leaf used by the equivalent registry command
- **AND** invoking the canonical command directly still works with the same behavior as before

#### Scenario: JSON envelopes stay consistent
- **WHEN** a user invokes a simple CLI action in JSON mode
- **THEN** the output uses the same success/error envelope semantics as canonical CLI operations
- **AND** failures carry stable error codes and remediation where the canonical operation provides them
