## ADDED Requirements

### Requirement: Connector-riding Claude edition
The Claude bundle SHALL also ship a connector edition from the same definition. It SHALL carry the hooked edition's hook payloads and skills unchanged, SHALL bundle no MCP server, and SHALL be listed in the repository's own marketplace under a plugin name and display name distinct from the hooked edition, and each of the two SHALL open its description by naming the account it serves. Its documentation SHALL state that it uses the Evermatter connection the account already has.

#### Scenario: Connector edition rides an existing connection
- **WHEN** the Claude packages are generated
- **THEN** the connector edition contains the hooked edition's hook files with identical bytes and the canonical skills
- **AND** it contains no `.mcp.json` and no `mcpServers` declaration
- **AND** the repository marketplace lists it under its own name, pointing at the generated connector edition
- **AND** its display name and description differ from the hooked edition's

### Requirement: Claude manifests fit claude.ai's plugin limits
Every generated Claude manifest SHALL have a lowercase-hyphenated name of at most 64 characters and a description of at most 500 characters, and generation SHALL fail when either is exceeded.

#### Scenario: Overlong description fails the build
- **WHEN** a Claude edition's description would exceed 500 characters
- **THEN** package generation fails and names the plugin and its description length
