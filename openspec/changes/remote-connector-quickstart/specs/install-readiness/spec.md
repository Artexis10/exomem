## MODIFIED Requirements

### Requirement: Read-Only Doctor Command

The system SHALL provide a CLI-only `doctor` admin command that checks local
installation readiness without mutating the repo, vault, environment, service
state, or model caches. It SHALL support `--profile lean|hybrid|media|remote`,
`--vault PATH`, `--json`, and an opt-in `--probe` flag. Without `--probe`, `doctor`
SHALL perform zero network calls regardless of profile — the command's read-only,
fully offline default is preserved unchanged. Documentation SHALL point users to
the matching profile before wiring a client or optional capability.

#### Scenario: Lean doctor over a valid vault

- **WHEN** `python -m exomem doctor --vault <valid-vault> --json` is run
- **THEN** it returns JSON containing `success`, `profile`, and a `checks` list
- **AND** each check contains `id`, `status`, `message`, and `remediation`
- **AND** no vault file is created, modified, moved, or deleted

#### Scenario: Missing required lean setup

- **WHEN** `doctor` cannot resolve a vault containing `Knowledge Base/_Schema/SKILL.md`
- **THEN** it exits non-zero
- **AND** it reports a remediation that tells the user to set `EXOMEM_VAULT_PATH`
  or pass `--vault` and run `init` if needed

#### Scenario: Doctor defaults to fully offline

- **WHEN** `doctor` is run for any profile without `--probe`
- **THEN** it performs zero network requests
- **AND** its behavior and output are unchanged from before `--probe` existed

### Requirement: Profile-Specific Readiness

The doctor command SHALL validate the requested capability profile. `lean` SHALL
check Python/package/vault/registry basics. `hybrid` SHALL additionally check
embeddings dependencies and embedding sidecar state. `media` SHALL additionally
check media extraction dependencies and Tesseract discovery. `remote` SHALL
additionally check public URL and OAuth-related environment variables. When
`--probe` is also passed, `remote` SHALL additionally run three read-only HTTP GET
checks against the live endpoints: the local MCP endpoint expecting `401`, the
OAuth authorization-server discovery document expecting `200` JSON, and the bare
OAuth protected-resource discovery path expecting `200` JSON whose `resource`
field equals `{EXOMEM_BASE_URL}/mcp`. Each of these three checks is a normal
`DoctorCheck` with pass/fail status and remediation; a network error (connection
refused, timeout, DNS failure) is a failure with an actionable message.

#### Scenario: Optional capability profile is requested

- **WHEN** `doctor --profile media` is run without media extraction dependencies
- **THEN** the report marks the missing media components as failures
- **AND** the remediation names `uv sync --extra media` and any required system
  tool such as Tesseract

#### Scenario: Remote probe confirms the live endpoint triple

- **WHEN** `doctor --profile remote --probe` is run against a working tunnel
- **THEN** the report includes a passing check for `http://127.0.0.1:8765/mcp`
  returning `401`
- **AND** a passing check for `{EXOMEM_BASE_URL}/.well-known/oauth-authorization-server`
  returning `200` JSON
- **AND** a passing check for the bare `{EXOMEM_BASE_URL}/.well-known/oauth-protected-resource`
  returning `200` JSON with `resource == {EXOMEM_BASE_URL}/mcp`

#### Scenario: Remote probe catches the bare well-known 404 that breaks connector registration

- **WHEN** `doctor --profile remote --probe` is run and the bare
  `{EXOMEM_BASE_URL}/.well-known/oauth-protected-resource` path returns `404`
- **THEN** the corresponding check fails
- **AND** its remediation names the `mcp_registration_failed` failure mode
  claude.ai's gateway hits and points at the server's workaround route being
  live through the tunnel

#### Scenario: Remote probe reports an unreachable endpoint actionably

- **WHEN** `doctor --profile remote --probe` cannot connect to one of the three
  endpoints (connection refused, timeout, or DNS failure)
- **THEN** the corresponding check fails
- **AND** its remediation is actionable, e.g. naming that the tunnel is not
  running and how to start it

## ADDED Requirements

### Requirement: Ingress Profile Guidance

The project SHALL document an ingress-profile decision table so a new user
without a domain is not steered toward Tailscale Funnel by default. The
documentation SHALL present, in order: Cloudflare Tunnel (for users who own a
domain, scripted via `scripts/setup-cloudflared.ps1`), ngrok (no domain needed;
one free static dev domain per account, scripted on Windows via
`scripts/setup-ngrok.ps1`), and an SSH reverse tunnel to a VPS as a fallback.
Tailscale Funnel SHALL be documented only as a demoted footnote naming its
relay-throttling rationale, not deleted.

#### Scenario: New user picks an ingress profile without a domain

- **WHEN** a new user without a domain reads `docs/remote-quickstart.md`
- **THEN** the decision table surfaces ngrok as a no-domain option before any
  mention of Tailscale Funnel
- **AND** Tailscale Funnel appears only in a footnote naming the relay-throttling
  rationale for why it is no longer the default recommendation

#### Scenario: ngrok ingress is scripted on Windows and documented inline elsewhere

- **WHEN** a Windows user without a domain follows the ngrok path
- **THEN** `scripts/setup-ngrok.ps1` verifies ngrok on `PATH` and an authtoken is
  configured, writes the static dev domain config, and installs auto-start
- **AND** macOS/Linux users instead follow two inline-documented commands with no
  script required
