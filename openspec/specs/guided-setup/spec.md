# guided-setup Specification

## Purpose
TBD - created by archiving change add-setup-wizard. Update Purpose after archive.
## Requirements
### Requirement: One-command guided local setup
The system SHALL provide an `exomem setup` CLI subcommand that performs, in
order: vault-path selection, a pre-init structure scan of the chosen vault, a
statement of the write contract (writes only under `Knowledge Base/`; existing
files untouched and read-only), Knowledge Base initialization, search-profile
selection (lean/hybrid), a doctor preflight, agent/client registration, skill
installation, optional hook installation, and a per-step summary with next
steps. Each step SHALL report `[done]`, `[skipped: <reason>]`, or
`[failed: <reason>]`.

When the chosen vault already contains non-KB content, setup SHALL offer the
adoption workflow as the next step: scan-only report by default, with explicit
options to save an adoption manifest, copy selected material as sources, or
compile selected material into governed knowledge. Setup SHALL NOT imply that
existing notes must be restructured before Exomem is useful.

#### Scenario: Fresh vault happy path
- **WHEN** `exomem setup` runs against a directory with existing non-KB content
- **THEN** the pre-init scan reports the existing files and the write contract,
  `Knowledge Base/` is created, the skill is installed, and the summary lists
  every step's outcome

#### Scenario: Existing vault routes to adoption
- **WHEN** setup detects substantial existing non-KB content
- **THEN** it states that the content remains untouched/read-only
- **AND** it offers the adoption workflow as a next step instead of telling the
  user to restructure their vault

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
`EXOMEM_VAULT_PATH` (and `EXOMEM_DISABLE_EMBEDDINGS=1` for the lean profile) in
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

### Requirement: Setup Teaches The Cognition Layer
The setup summary and first-run docs SHALL explain the simple Exomem model:
built-in AI memory stores preferences/routing; Exomem stores durable governed
knowledge with sources, proof, history, decisions, records, and review. The
summary SHALL include first prompts that use simple verbs rather than internal
ontology. Setup SHALL also present knowledge packs as beginner-facing product
choices and persist selected packs under the governed Knowledge Base layer.

#### Scenario: Fresh vault chooses useful packs
- **WHEN** setup runs against a fresh or structurally empty vault
- **THEN** it presents available packs with beginner-facing descriptions
- **AND** non-interactive setup persists the default personal-records pack
- **AND** interactive setup can persist multiple selected packs

#### Scenario: Existing vault confirms inferred packs
- **WHEN** setup detects existing non-KB content and adoption suggests packs
- **THEN** setup shows the suggested packs as choices rather than a migration
- **AND** persisted selection records only guidance metadata under
  `Knowledge Base/`

#### Scenario: First prompts are simple
- **WHEN** setup finishes successfully
- **THEN** the printed next steps include prompts such as "what does this vault
  look like?", "import/adopt my old notes safely", "what do we know about X?",
  "show the sources", and "what needs review?"
- **AND** the prompt examples do not require the user to know internal page
  types
