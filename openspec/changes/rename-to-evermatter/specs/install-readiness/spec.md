## MODIFIED Requirements

### Requirement: Uv-First Local Setup

The system SHALL document `uv sync` as the canonical local setup path and
`uv run python -m exomem ...` as the canonical source-checkout execution path,
with `pip install -e .` only as a fallback for users who manage Python
environments manually. It SHALL document the zero-install packaged proof,
`uvx evermatter demo`, before the Install section as a trial step that needs
no checkout; that proof SHALL NOT replace the `uv sync` path for ongoing
local development.

#### Scenario: New user follows the quickstart

- **WHEN** a new user reads the README or local setup guide
- **THEN** the first install commands use `uv sync`
- **AND** the first exomem commands use `uv run python -m exomem`
- **AND** pip appears only as a fallback path

#### Scenario: Proof precedes install

- **WHEN** a new user reads the README
- **THEN** the packaged proof command `uvx evermatter demo` appears before the
  Install section
- **AND** the Install section still documents `uv sync` as canonical and
  `pip install -e .` only as a fallback
