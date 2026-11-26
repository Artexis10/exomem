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

- **WHEN** a deployment sets `EVERMATTER_VAULT_PATH`, or only the older `EXOMEM_VAULT_PATH`
- **THEN** the service, the hooks and the service scripts use that value
- **AND** setup and the installers write `EVERMATTER_VAULT_PATH`

#### Scenario: The two prefixes disagree

- **WHEN** one source sets `EVERMATTER_VAULT_PATH` and `EXOMEM_VAULT_PATH` to different values
- **THEN** the service and every command that reads a placement setting or writes a credential refuse to start and name both variables, without their values
- **AND** the hooks skip that turn's nudge or checkpoint and log the conflict
- **AND** the service scripts that read the setting from a service environment, a `.env` file or the shell refuse and name both variables

#### Scenario: The two prefixes disagree about a setting that picks no data

- **WHEN** one source sets `EVERMATTER_EMBED_BACKEND` and `EXOMEM_EMBED_BACKEND` to different values
- **THEN** the `EXOMEM_EMBED_BACKEND` value is used
- **AND** each hook still runs, and logs one line that names both variables, without their values

#### Scenario: A value written after import wins

- **WHEN** a shell exports an alias of the state root setting and a test fixture or command then sets its internal name in the same process
- **THEN** the process uses the value that the fixture or command set
- **AND** a child process inherits only the internal name

#### Scenario: A cell on an older image keeps its settings

- **WHEN** cellctl renders a manifest for a cell whose target image predates the release that reads `EVERMATTER_*`
- **THEN** the manifest carries the `EXOMEM_*` names

#### Scenario: A plugin rename moves an enabled install

- **WHEN** a user has the `exomem` plugin enabled and the marketplace update brings the `renames` entry
- **THEN** Claude Code enables `evermatter` in its place
- **AND** the plugin tells the user the one sign-in step once, when it has a server address configured

## MODIFIED Requirements

### Requirement: Exomem Is the Canonical Internal Name

The Python import package SHALL be `exomem`, and the CLI SHALL be reachable as `python -m exomem`. Every setting SHALL be documented under the `EVERMATTER_` prefix. The `EXOMEM_` and `KB_MCP_` prefixes SHALL stay working aliases through the compatibility window. Identifiers in code, the skill scaffold, tests and scripts SHALL use the exomem name. Prose that a person reads SHALL use the product name, Evermatter.

#### Scenario: Package imports under the new name

- **WHEN** `import exomem` (or any submodule) is executed in an installed environment
- **THEN** the package loads and all functionality is available under the `exomem` name

#### Scenario: Configuration documented under the new prefix

- **WHEN** a user consults the README or deployment docs for configuration
- **THEN** every variable is documented as `EVERMATTER_*`, with a note that `EXOMEM_*` and `KB_MCP_*` names remain honored

### Requirement: Legacy `KB_MCP_*` Environment Variables Keep Working

Code SHALL read each setting under one internal name, and its other names SHALL be aliases. At import, the system SHALL rename a set alias to the internal name when that is unset, and remove the alias. A set internal name, and a later in-process write of it, SHALL win.

#### Scenario: Old .env keeps working unchanged

- **WHEN** a process starts with only `KB_MCP_VAULT_PATH` set
- **THEN** the vault resolves exactly as before

#### Scenario: New name wins on conflict

- **WHEN** both `EXOMEM_DISABLE_CLIP` and `KB_MCP_DISABLE_CLIP` are set to different values
- **THEN** the `EXOMEM_DISABLE_CLIP` value is used

#### Scenario: Late-set legacy variables are recoverable

- **WHEN** a legacy variable is set after import and `promote_legacy()` is called again
- **THEN** the corresponding setting becomes visible
