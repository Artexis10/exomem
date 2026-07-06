## ADDED Requirements

### Requirement: Bootstrap Is Exposed On Every Generated Surface
The system SHALL expose `bootstrap` through the single command registry on MCP,
REST, CLI, and OpenAPI. The tool SHALL be marked read-only and non-destructive in
MCP annotations.

#### Scenario: Bootstrap appears in generated surfaces
- **WHEN** the server is built
- **THEN** `bootstrap` appears in the MCP tool list
- **AND** `/api/bootstrap` appears in the REST facade and OpenAPI document
- **AND** the CLI exposes a `bootstrap` subcommand

#### Scenario: Bootstrap is accounted for by schema fidelity tests
- **WHEN** the MCP schema fidelity test runs
- **THEN** the live tool set includes `bootstrap`
- **AND** `bootstrap` is registry-generated rather than a hand-registered exception
