## ADDED Requirements

### Requirement: Uv-First Local Setup

The system SHALL document `uv sync` as the canonical local setup path and
`uv run python -m kb_mcp ...` as the canonical source-checkout execution path.
It SHALL keep `pip install -e .` documented only as a fallback for users who
manage Python environments manually.

#### Scenario: New user follows the quickstart

- **WHEN** a new user reads the README or local setup guide
- **THEN** the first install commands use `uv sync`
- **AND** the first kb-mcp commands use `uv run python -m kb_mcp`
- **AND** pip appears only as a fallback path

### Requirement: Read-Only Doctor Command

The system SHALL provide a CLI-only `doctor` admin command that checks local
installation readiness without mutating the repo, vault, environment, service
state, or model caches. It SHALL support `--profile lean|hybrid|media|remote`,
`--vault PATH`, and `--json`.

#### Scenario: Lean doctor over a valid vault

- **WHEN** `python -m kb_mcp doctor --vault <valid-vault> --json` is run
- **THEN** it returns JSON containing `success`, `profile`, and a `checks` list
- **AND** each check contains `id`, `status`, `message`, and `remediation`
- **AND** no vault file is created, modified, moved, or deleted

#### Scenario: Missing required lean setup

- **WHEN** `doctor` cannot resolve a vault containing `Knowledge Base/_Schema/SKILL.md`
- **THEN** it exits non-zero
- **AND** it reports a remediation that tells the user to set `KB_MCP_VAULT_PATH`
  or pass `--vault` and run `init` if needed

### Requirement: Profile-Specific Readiness

The doctor command SHALL validate the requested capability profile. `lean` SHALL
check Python/package/vault/registry basics. `hybrid` SHALL additionally check
embeddings dependencies and embedding sidecar state. `media` SHALL additionally
check media extraction dependencies and Tesseract discovery. `remote` SHALL
additionally check public URL and OAuth-related environment variables.

#### Scenario: Optional capability profile is requested

- **WHEN** `doctor --profile media` is run without media extraction dependencies
- **THEN** the report marks the missing media components as failures
- **AND** the remediation names the relevant `uv sync --extra ...` command

### Requirement: Actionable Human Output

The doctor command SHALL render human-readable output grouped by check status and
SHALL include concrete remediation text for every warning and failure. It SHALL
exit `0` when no failures are present, `1` when any failure is present, and `2`
for usage errors.

#### Scenario: Human output names remediations

- **WHEN** `doctor` finds warnings or failures
- **THEN** the terminal output includes the check id, message, and remediation
- **AND** the process exit code follows the documented status convention
