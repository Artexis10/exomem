# exomem-identity Specification

## Purpose
TBD - created by archiving change rename-internals-to-exomem. Update Purpose after archive.
## Requirements
### Requirement: Exomem Is the Canonical Internal Name

The Python import package SHALL be `exomem` and all environment variables SHALL use the
`EXOMEM_` prefix. Code under `src/`, the shipped skill scaffold, tests, scripts, and docs
SHALL reference the exomem name; the CLI SHALL be reachable as `python -m exomem`.

#### Scenario: Package imports under the new name

- **WHEN** `import exomem` (or any submodule) is executed in an installed environment
- **THEN** the package loads and all functionality is available under the `exomem` name

#### Scenario: Configuration documented under the new prefix

- **WHEN** a user consults the README/deployment docs for configuration
- **THEN** every variable is documented as `EXOMEM_*`, with a note that legacy `KB_MCP_*`
  names remain honored

### Requirement: Legacy `kb_mcp` Imports Keep Working

The distribution SHALL ship a `kb_mcp` compatibility package such that `import kb_mcp`,
`import kb_mcp.<submodule>`, and `from kb_mcp import <name>` resolve to the same module
objects as their `exomem` counterparts (single module state), emit a `DeprecationWarning`
on first import, and `python -m kb_mcp` SHALL start the CLI exactly like `python -m exomem`.

#### Scenario: Module identity, not duplication

- **WHEN** `kb_mcp.find` and `exomem.find` are both imported in one process
- **THEN** they are the identical module object (`is`-equal) with shared state

#### Scenario: Old entrypoint still runs

- **WHEN** `python -m kb_mcp --help` is invoked
- **THEN** it exits successfully with the CLI help output

### Requirement: Legacy `KB_MCP_*` Environment Variables Keep Working

The system SHALL honor legacy `KB_MCP_*` environment variables by promoting each to its
`EXOMEM_*` equivalent at package import when the new name is unset. An explicitly set
`EXOMEM_*` value SHALL take precedence over a conflicting legacy value. The promotion SHALL
be re-runnable for environments populated after import, and SHALL log a single advisory
line when legacy names were promoted.

#### Scenario: Old .env keeps working unchanged

- **WHEN** a process starts with only `KB_MCP_VAULT_PATH` set
- **THEN** the vault resolves exactly as before, via the promoted `EXOMEM_VAULT_PATH`

#### Scenario: New name wins on conflict

- **WHEN** both `EXOMEM_DISABLE_CLIP` and `KB_MCP_DISABLE_CLIP` are set to different values
- **THEN** the `EXOMEM_DISABLE_CLIP` value is used

#### Scenario: Late-set legacy variables are recoverable

- **WHEN** a legacy variable is set after import and `promote_legacy()` is called again
- **THEN** the corresponding `EXOMEM_*` variable becomes visible

