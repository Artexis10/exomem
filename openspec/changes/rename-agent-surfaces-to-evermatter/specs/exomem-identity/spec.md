## ADDED Requirements

### Requirement: Agent-Facing Identifiers Use the Product Name

The MCP server key of a new registration, the serverInfo name, the skill names and the settings prefix that a user or an agent sees SHALL be `evermatter` once the switch release ships. The plugin names, and the key of an existing registration that has a URL, follow in a later release, because renaming them signs the user out. Every component SHALL accept the old `exomem` identifiers through the compatibility window, so that an install made before the switch keeps working without any action.

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
- **THEN** setup renames that key to `evermatter` when the registration is a stdio server, rewrites the managed instruction block, and keeps a backup of each file it changed
- **AND** it keeps the key of a registration that has a URL and says why: a rename signs the user out
- **AND** it leaves rules and hooks that the user wrote unchanged and lists them

#### Scenario: Settings accept the new prefix

- **WHEN** a deployment sets `EVERMATTER_VAULT_PATH` and no `EXOMEM_VAULT_PATH`
- **THEN** the service and the hooks use that value
- **AND** an explicitly set `EXOMEM_VAULT_PATH`, whether exported, forced by the service or read from `.env` under its own precedence, is never overridden by an `EVERMATTER_` value

### Requirement: Stored Identifiers Keep the Internal Name

The `exomem_id` frontmatter key, state and config paths, the Python import package and the `exomem://` reference scheme SHALL keep the internal name. These names are stored in users' notes and state; renaming them would rewrite that data for no visible gain.

#### Scenario: An existing vault needs no rewrite

- **WHEN** a vault written before the rename is opened after the switch release
- **THEN** every note keeps its `exomem_id` and every stored `exomem://` ref resolves
