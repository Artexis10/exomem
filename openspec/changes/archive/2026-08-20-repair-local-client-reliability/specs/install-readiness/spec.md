## ADDED Requirements

### Requirement: Service-Aware Local Client Registration

Local setup SHALL resolve one MCP client route in this order: an explicit valid service
URL, a valid cwd `.env` service origin, a valid inherited `EXOMEM_BASE_URL`, then the existing
manual stdio fallback. An explicit stdio selection MUST override automatic service
discovery. Claude Code and Codex service routes SHALL use their native streamable-HTTP
configuration and MUST NOT persist OAuth bearer tokens. A present invalid configured URL
MUST fail before configuration mutation.

#### Scenario: Configured service becomes the shared client route

- **WHEN** setup runs with a valid `--mcp-url` or configured `EXOMEM_BASE_URL`
- **THEN** Claude Code and Codex receive the normalized `/mcp` HTTP endpoint
- **AND** their configuration does not launch a full Exomem stdio process
- **AND** setup prints the native client authentication follow-up

#### Scenario: Local-only setup retains stdio

- **WHEN** no valid service URL is configured or the caller explicitly selects stdio
- **THEN** setup registers the existing durable stdio command
- **AND** the plugin does not start a second Exomem core

#### Scenario: Unsafe service URL is rejected before configuration mutation

- **WHEN** a service URL contains credentials, a query, a fragment, an unrelated path,
  malformed authority, or public plain HTTP
- **THEN** setup rejects it with an actionable error
- **AND** existing client and plugin configuration remains unchanged

### Requirement: Conservative Client Registration Convergence

Setup SHALL leave an existing explicit client registration unchanged unless an interactive
user confirms replacement or a noninteractive caller passes the explicit replacement flag.
The desired route MUST pass validation before any existing registration is removed.
Inventory MUST NOT health-check or start MCP servers. Replacement SHALL use each client's
supported configuration interface, preserve the Codex backup/parse safety fallback, restore
the prior explicit configuration after an add failure, and never disable or rewrite plugin
state. Claude inventory and confirmed convergence MUST cover applicable local, project, and
user registrations so a higher-precedence entry cannot shadow the desired route.

#### Scenario: Noninteractive setup preserves an existing route by default

- **WHEN** `setup --yes` finds an existing Exomem client registration without the explicit
  replacement flag
- **THEN** it reports the existing route and leaves it unchanged

#### Scenario: Explicit replacement converges on the service

- **WHEN** the caller selects a valid service URL and confirms or explicitly requests
  replacement
- **THEN** explicit Claude Code and Codex registrations converge on that URL
- **AND** a repeated setup run is idempotent
- **AND** plugin state is untouched

#### Scenario: Higher-precedence stdio registrations cannot shadow HTTP

- **WHEN** local and project stdio registrations exist and the desired route is user HTTP
- **THEN** setup reports every shadowing scope before mutation
- **AND** confirmed replacement removes all explicit conflicts transactionally
- **AND** a failure restores the original local and project registrations

#### Scenario: Failed replacement restores the prior route

- **WHEN** a confirmed replacement removes an explicit registration but the desired add
  fails
- **THEN** setup restores the prior explicit configuration
- **AND** reports the failure without claiming convergence

### Requirement: Non-Fan-Out Claude Plugin

The Claude plugin SHALL retain its skills and hooks but MUST NOT auto-start a full Exomem
stdio core. It SHALL expose an optional streamable-HTTP entry backed by
`userConfig.mcp_url`, where the non-empty value is the full canonical `/mcp` URL. On Claude
Code 2.1.208 or newer, an empty value SHALL remain inert and a plugin/manual pair with an
identical endpoint SHALL deduplicate. Setup SHALL detect an enabled legacy Exomem plugin
that still declares stdio and MUST NOT claim the shared route is complete until it is
updated. Migration guidance SHALL require `/reload-plugins` or a client restart so an
already-running legacy stdio server does not survive the update in a live session.

#### Scenario: Plugin endpoint is not configured

- **WHEN** the plugin is enabled without `mcp_url`
- **THEN** its MCP entry is reported as not configured
- **AND** no Exomem process is launched
- **AND** its skills and hooks remain available

#### Scenario: Legacy plugin would preserve fan-out

- **WHEN** setup discovers an enabled Exomem plugin whose MCP entry still uses stdio
- **THEN** setup reports a failed convergence preflight with the exact plugin-update command
- **AND** it does not claim that client sessions share one core

### Requirement: Editable Environment Lock Consistency

Doctor SHALL identify a validated editable Exomem project root and compare the active
environment with the selected locked runtime dependency set through a bounded offline `uv`
check using `--locked` and `--no-dev`. Setup SHALL atomically merge the selected
`cli_profile` into install state without deleting service or unknown fields, and Doctor
SHALL resolve that intended value before dependency-based inference. The check MUST NOT sync
packages, rewrite the lock, download artifacts, mutate the checkout, load optional models,
or treat unrelated extra packages as drift. It MUST NOT require the optional MLX backend
unless that backend is explicitly configured. An older uv that does not support the safety
flags SHALL produce an unverifiable warning, not an outdated-environment failure. Wheel and
managed release installs SHALL skip the checkout lock comparison.

#### Scenario: Current source over stale dependencies fails doctor

- **WHEN** editable source imports the current Exomem version but the active environment is
  not synchronized with the checkout lock for the selected profile
- **THEN** doctor reports a failed install-consistency check
- **AND** its remediation gives the exact `uv sync` command for that checkout/profile

#### Scenario: Consistent editable environment passes read-only

- **WHEN** the active editable environment matches the selected locked dependency set
- **THEN** doctor reports the source version, distribution version, interpreter, and
  editable origin as consistent
- **AND** no environment or checkout file changes

#### Scenario: Consistency cannot be verified

- **WHEN** uv is unavailable, the check times out, or editable metadata cannot be validated
- **THEN** doctor emits a bounded warning rather than crashing or claiming consistency

### Requirement: Truthful Cross-Platform Process Memory

Doctor and the persistent-core resource verifier SHALL label the process-memory metric they
report. On Darwin they SHALL prefer `proc_pid_rusage` physical footprint for each matching
Exomem core process while retaining RSS as compatibility detail. The native call MUST use
the exact complete `rusage_info_v0` layout and validate its size. On other platforms, or
when native sampling fails for a PID, they SHALL use explicitly labelled RSS. Mixed native
and fallback rows MUST expose separate totals and MUST NOT be summed under one metric label.
The verifier SHALL preserve its existing RSS limit and use a distinct selected physical-
memory limit that is skipped when native sampling is incomplete.

#### Scenario: Darwin physical footprint is available

- **WHEN** a running Exomem PID can be sampled through Darwin process rusage
- **THEN** its selected memory value and aggregate use physical footprint
- **AND** its RSS remains available as a separately named detail

#### Scenario: Darwin sampling fails safely

- **WHEN** a PID exits, access is denied, or the Darwin API is unavailable
- **THEN** that process falls back to labelled RSS
- **AND** doctor and the verifier continue without treating RSS as physical footprint

#### Scenario: Darwin sampling is mixed

- **WHEN** physical footprint is available for some matching PIDs but not others
- **THEN** the aggregate metric is `mixed` with separate physical-footprint and RSS-fallback
  totals
- **AND** the selected physical-memory release gate is not evaluated against a partial sum
