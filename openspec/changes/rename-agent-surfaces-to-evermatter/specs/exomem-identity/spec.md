## ADDED Requirements

### Requirement: Agent-Facing Identifiers Use the Product Name

The MCP server key, the serverInfo name, the plugin names, the skill names and the settings prefix that a user or an agent sees SHALL be `evermatter` once the switch releases ship. A connection that signs in moves in the release that renames it, and that release tells the user to sign in once. Every component SHALL accept the old `exomem` identifiers through the compatibility window, so that an install made before the switch keeps working without any action.

#### Scenario: An install from before the switch keeps working

- **WHEN** a client holds the server key `exomem`, and its hooks or permission rules name `mcp__exomem__*` or `mcp__plugin_exomem_exomem__*`
- **THEN** the tools answer, our hooks fire on those tool names, and doctor reports the install as healthy
- **AND** doctor passes while an `exomem` server answers each rule or hook that names `mcp__exomem__*`

#### Scenario: A rule that names the old prefix has no server left

- **WHEN** a user-written permission rule or hook names `mcp__exomem__*` in a client that holds no `exomem` server
- **THEN** doctor warns and names the file and the rule, and says to change it to `mcp__evermatter__*`
- **AND** setup never edits that rule

#### Scenario: A new install uses the new names

- **WHEN** a user installs after the switch release
- **THEN** setup writes the server key `evermatter` and the skills install as `evermatter-*`
- **AND** the client exposes the tools under the `evermatter` prefix

#### Scenario: Setup migrates an existing configuration

- **WHEN** a user runs `evermatter setup --migrate` on a client configuration that holds the server key `exomem`
- **THEN** setup renames that key to `evermatter`, rewrites the managed instruction block, and keeps a backup of each file it changed
- **AND** for each renamed registration that has a URL, it prints the client's sign-in step
- **AND** it leaves rules and hooks that the user wrote unchanged and lists them

#### Scenario: Settings use the new prefix and keep the old one as an alias

- **WHEN** a deployment sets `EXOMEM_VAULT_PATH` and no `EVERMATTER_VAULT_PATH`
- **THEN** the service and the hooks use that value as `EVERMATTER_VAULT_PATH`
- **AND** setup and the installers write `EVERMATTER_VAULT_PATH`

#### Scenario: The two prefixes disagree

- **WHEN** one source sets `EVERMATTER_VAULT_PATH` and `EXOMEM_VAULT_PATH` to different values
- **THEN** the service and every command that writes a credential refuse to start and name both variables, without their values
- **AND** the hooks log the conflict and use the `EVERMATTER_` value

#### Scenario: A plugin rename moves an enabled install

- **WHEN** a user has the `exomem` plugin enabled and the marketplace update brings the `renames` entry
- **THEN** Claude Code enables `evermatter` in its place
- **AND** the plugin tells the user the one sign-in step if its server holds no sign-in
