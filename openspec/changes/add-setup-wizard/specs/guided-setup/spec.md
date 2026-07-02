# guided-setup

## ADDED Requirements

### Requirement: One-command guided local setup
The system SHALL provide an `exomem setup` CLI subcommand that performs, in
order: vault-path selection, a pre-init structure scan of the chosen vault, a
statement of the write contract (writes only under `Knowledge Base/`; existing
files untouched and read-only), Knowledge Base initialization, search-profile
selection (lean/hybrid), a doctor preflight, Claude Code MCP registration,
skill installation, optional hook installation, and a per-step summary with
next steps. Each step SHALL report `[done]`, `[skipped: <reason>]`, or
`[failed: <reason>]`.

#### Scenario: Fresh vault happy path
- **WHEN** `exomem setup` runs against a directory with existing non-KB content
- **THEN** the pre-init scan reports the existing files and the write contract,
  `Knowledge Base/` is created, the skill is installed, and the summary lists
  every step's outcome

### Requirement: Idempotent re-run
Re-running the wizard against an already-configured environment SHALL be safe:
already-satisfied steps report `[skipped]` and the run exits 0 without
modifying existing state. The wizard SHALL NOT pass a force/overlay flag to
initialization, and SHALL NOT overwrite a skill install whose `SKILL.md` does
not identify as the bundled skill.

#### Scenario: Full re-run converges to no-ops
- **WHEN** `exomem setup` runs a second time with the same inputs
- **THEN** initialization and skill installation report `[skipped]` and the
  exit code is 0

#### Scenario: Foreign skill install is preserved
- **WHEN** the skill target exists but its `SKILL.md` does not carry the
  bundled skill's name
- **THEN** the wizard warns and skips instead of overwriting

### Requirement: Non-interactive mode with a hard doctor gate
The wizard SHALL support `--yes` (requiring `--vault`) plus flags for profile
(`--lean`/`--hybrid`), hooks (`--with-hooks`/`--no-hooks`), registration
(`--skip-claude-register`, `--scope user|local|project` defaulting to `user`).
In non-interactive mode a failed doctor preflight SHALL abort with exit code 1.

#### Scenario: Scripted run aborts on failed preflight
- **WHEN** `exomem setup --yes --vault <path>` runs and the doctor preflight fails
- **THEN** the wizard prints the doctor report and exits 1 without registering
  or installing anything further

#### Scenario: --yes requires a vault
- **WHEN** `exomem setup --yes` runs without `--vault`
- **THEN** the command exits with a usage error (exit code 2)

### Requirement: Claude Code registration with fallback
When the `claude` CLI is found, the wizard SHALL register the server via
`claude mcp add` as an argv list (never a shell string), carrying
`KB_MCP_VAULT_PATH` (and `KB_MCP_DISABLE_EMBEDDINGS=1` for the lean profile) in
the registration env, using `uv --directory <repo>` in a repo checkout and the
running interpreter otherwise. When the CLI is absent, the wizard SHALL print a
valid `.mcp.json` snippet produced by JSON serialization.

#### Scenario: Registration command shape
- **WHEN** the wizard registers with the lean profile at scope `user`
- **THEN** the invoked argv contains `mcp add exomem`, `--scope user`, both env
  assignments, and a `--`-separated server command

#### Scenario: No claude CLI
- **WHEN** no `claude` executable is on PATH
- **THEN** the wizard prints an `.mcp.json` snippet containing the `mcpServers`
  entry instead of failing
