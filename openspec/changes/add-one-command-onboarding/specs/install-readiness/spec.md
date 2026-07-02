## MODIFIED Requirements

### Requirement: Uv-First Local Setup

The system SHALL document `uv sync` as the canonical local setup path and
`uv run python -m exomem ...` as the canonical source-checkout execution path.
It SHALL keep `pip install -e .` documented only as a fallback for users who
manage Python environments manually. The zero-install packaged proof
(`uvx exomem demo`) SHALL be documented ahead of the Install section as a
pre-install trial step that requires no checkout; it SHALL NOT replace
`uv sync` / `uv run python -m exomem` as the documented path for ongoing local
development.

#### Scenario: New user follows the quickstart

- **WHEN** a new user reads the README or local setup guide
- **THEN** the first install commands use `uv sync`
- **AND** the first exomem commands use `uv run python -m exomem`
- **AND** pip appears only as a fallback path

#### Scenario: Proof precedes install

- **WHEN** a new user reads the README
- **THEN** the packaged proof command `uvx exomem demo` appears before the
  Install section
- **AND** the Install section still documents `uv sync` as canonical and
  `pip install -e .` only as a fallback

### Requirement: Sample Vault Smoke

The system SHALL provide a packaged, read-only `exomem demo` command that
proves the lean install path using a sample vault shipped inside the
installed package, without model downloads, without a git checkout, and
without mutating the vault or the installed package. The command SHALL be
runnable with no user-supplied vault and no manual configuration. It SHALL
validate, in order: `doctor --profile lean`, a keyword `find` for "retrieval"
that must return a known sample page, a full-page `get` of that page, and a
read-only `audit`. It SHALL copy the packaged sample vault into a temporary
directory before running any check, and SHALL exit non-zero with an
actionable message identifying the failing step if any check fails. It SHALL
support `--json` (a stable envelope with `success`, per-step `name`/`ok`/
`seconds`, and `total_seconds`) and `--keep` (retain the temporary vault and
print its path instead of deleting it).

#### Scenario: Bare uvx run needs nothing but the package

- **WHEN** `uvx exomem demo` is run with no prior checkout, vault, or
  configuration
- **THEN** all four steps run against a temporary copy of the sample vault
  shipped inside the installed package
- **AND** the command exits 0 and prints a `demo PASS` summary line

#### Scenario: Installed package is never mutated

- **WHEN** `exomem demo` runs from a read-only site-packages install
- **THEN** the packaged sample vault is copied to a temporary directory
  before any step runs
- **AND** no file mtime under the installed package changes across the run

#### Scenario: Failing step exits non-zero actionably

- **WHEN** any of the four steps fails (for example a broken wikilink in the
  sample vault)
- **THEN** the command exits non-zero
- **AND** the failing step and an actionable message are identified in the
  output

### Requirement: CI Install-Readiness Gates

CI SHALL validate the cheap public-readiness gates: OpenSpec specs, package
build, and the packaged lean demo (`exomem demo --json`) run against the
vault shipped inside the package. CI SHALL additionally run a wheel-path
onboarding gate that builds the wheel, installs only that wheel into a fresh
virtual environment with no repository checkout present, and runs
`exomem demo --json` and `exomem setup --yes --skip-claude-register` from a
temporary working directory, asserting both succeed and the combined sequence
completes within a documented wall-time budget. CI MUST NOT require model
downloads, GPU, media extras, external services, or a private vault for any
of these gates.

#### Scenario: Pull request runs public readiness checks

- **WHEN** CI runs for a pull request
- **THEN** OpenSpec specs validate, the package builds, and
  `exomem demo --json` succeeds against the vault shipped inside the package

#### Scenario: Wheel-only build proves the packaged onboarding path

- **WHEN** the `onboarding` CI job builds the wheel and installs only that
  wheel into a fresh virtual environment
- **THEN** `exomem demo --json` succeeds from a temporary working directory
  outside the checkout
- **AND** `exomem setup --vault <tmp> --yes --skip-claude-register` succeeds
  against a temporary vault
- **AND** the combined sequence completes within the documented wall-time
  budget

### Requirement: Release Hygiene

The project SHALL document a maintainer release checklist that includes
tests, lint, OpenSpec validation, package build, the packaged demo command,
relevant doctor profiles, and a wheel-path time-to-value check before
publishing.

#### Scenario: Maintainer prepares a release

- **WHEN** the release checklist is followed
- **THEN** it includes commands for pytest, ruff, OpenSpec spec validation,
  `uv build`, `exomem demo`, and relevant doctor profiles
- **AND** it includes running `scripts/time-to-value.py` to confirm the
  measured wheel-path time-to-value budget

## ADDED Requirements

### Requirement: Packaged Demo Command

The system SHALL provide an `exomem demo` subcommand that proves the install
works using a sample vault shipped inside the installed package, runnable
with no git checkout, no manual configuration, and no user-supplied vault. It
SHALL never mutate the installed package: it SHALL copy the packaged sample
vault into a temporary directory before running any check. It SHALL run, in
order and each timed, a lean `doctor` check, a keyword `find` for "retrieval"
asserting the known sample insight page is a hit, a `get` of that page, and a
read-only `audit`. It SHALL print one line per step naming the step and its
duration, followed by a final `demo PASS — total <N>s` line and a pointer to
`exomem setup`. It SHALL support `--json` (a stable envelope with `success`,
a `steps` list of `{name, ok, seconds}`, and `total_seconds`) and `--keep`
(retain the temporary vault and print its path so it can be opened directly).
It SHALL exit non-zero if any step fails.

#### Scenario: Bare uvx run needs nothing but the package

- **WHEN** `uvx exomem demo` is run with no prior checkout, vault, or
  configuration
- **THEN** all four steps run against a temporary copy of the packaged
  sample vault
- **AND** the command exits 0 and prints `demo PASS`

#### Scenario: JSON envelope for CI

- **WHEN** `exomem demo --json` is run
- **THEN** it prints one JSON object containing `success`, a `steps` list of
  `{name, ok, seconds}` entries, and `total_seconds`

#### Scenario: Keep flag retains the vault

- **WHEN** `exomem demo --keep` is run
- **THEN** the temporary vault directory is not deleted afterward
- **AND** its path is printed so it can be opened directly

#### Scenario: A failing step aborts with exit 1

- **WHEN** any of the four steps fails (for example against a corrupted
  packaged vault)
- **THEN** the command exits 1
- **AND** the failing step is identified in the output

### Requirement: Agent Connect Matrix

The project SHALL document a single connect matrix covering every supported
MCP client: Claude Code (via `exomem setup`), Codex CLI (a `codex mcp add`
command plus the equivalent manual `~/.codex/config.toml`
`[mcp_servers.exomem]` block), claude.ai remote connectors (a pointer to the
remote deployment guide), and other MCP-capable clients (a generic stdio JSON
server-config example).

#### Scenario: New user picks their client

- **WHEN** a new user reads the README connect matrix
- **THEN** each supported client has a runnable command or config snippet in
  one place, rather than scattered across multiple docs

#### Scenario: Codex CLI path is documented

- **WHEN** a user follows the Codex CLI row of the matrix
- **THEN** they see both a `codex mcp add` command and the equivalent manual
  `~/.codex/config.toml` block naming `EXOMEM_VAULT_PATH`

### Requirement: Durable Server Registration

The setup wizard's Claude Code registration step SHALL select the server
launch command in this order: (1) a source-checkout invocation
(`uv --directory <repo> run python -m exomem --transport stdio`) when run
from a repository containing `pyproject.toml` with `uv` on `PATH`; (2) the
durable `exomem` console script (resolved via `shutil.which`) when present,
for `pip`/`uv tool` installs; (3) otherwise, a
`uvx exomem --transport stdio` fallback, printed together with a note
recommending `uv tool install exomem` for a registration that survives cache
pruning. It SHALL NOT register a command that resolves into an ephemeral
`uvx` cache environment when a durable alternative is available.

#### Scenario: Repo checkout keeps the uv-directory form

- **WHEN** `exomem setup` runs from a source checkout with `uv` on `PATH`
- **THEN** the registered command is
  `uv --directory <repo> run python -m exomem --transport stdio`

#### Scenario: Installed console script is preferred over the interpreter path

- **WHEN** `exomem setup` runs outside a source checkout and
  `shutil.which("exomem")` resolves
- **THEN** the registered command invokes that console script, not
  `sys.executable -m exomem`

#### Scenario: Ephemeral uvx run falls back with a durability note

- **WHEN** `uvx exomem setup` runs with no repo checkout and no `exomem`
  console script on `PATH`
- **THEN** the registered command is `uvx exomem --transport stdio`
- **AND** the wizard prints a note recommending `uv tool install exomem` for
  a registration that survives uvx cache pruning
