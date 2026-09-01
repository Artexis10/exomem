# install-readiness Specification

## Purpose
Make exomem reproducible and diagnosable for new installs by documenting a
`uv`-first setup path and providing a read-only local preflight command for core
and optional capability profiles.

## Requirements

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

#### Scenario: Models are already cached locally

- **WHEN** `doctor --profile hybrid` is run and the embedding model and reranker are already
  present in the local Hugging Face hub cache
- **THEN** the `models.cache` check reports a passing status
- **AND** no network request is made and no model is downloaded as part of the check

#### Scenario: Models are not yet cached locally

- **WHEN** `doctor --profile hybrid` or `doctor --profile media` is run and one or more of the
  embedding model, reranker, or (when CLIP is enabled) CLIP model are not present in the local
  Hugging Face hub cache
- **THEN** the `models.cache` check reports a warn-level status naming the missing model(s)
- **AND** the remediation is to run `exomem warm`
- **AND** no vault file, cache directory, or model file is created, modified, or downloaded by the
  check itself

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

### Requirement: Actionable Human Output

The doctor command SHALL render human-readable output grouped by check status and
SHALL include concrete remediation text for every warning and failure. It SHALL
exit `0` when no failures are present, `1` when any failure is present, and `2`
for usage errors.

#### Scenario: Human output names remediations

- **WHEN** `doctor` finds warnings or failures
- **THEN** the terminal output includes the check id, message, and remediation
- **AND** the process exit code follows the documented status convention

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

#### Scenario: New user runs sample smoke

- **WHEN** the sample smoke script is run from a source checkout
- **THEN** it validates `doctor --profile lean`, a keyword `find`, a full-page
  read, and a read-only `audit`
- **AND** it exits non-zero with an actionable message if any check fails

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

### Requirement: Guided setup generates a starter access policy

After `init`, the `exomem setup` wizard SHALL run an idempotent personalize step that
proposes an access policy for the vault's top-level sibling folders and, on confirmation
(or under `--yes`), writes/merges `Knowledge Base/_access.yaml`. The step SHALL report
`[done]` / `[skipped]` / `[failed]` like every other wizard step and SHALL be safe to
re-run.

#### Scenario: Fresh vault with a sibling folder
- **WHEN** `setup` runs against a vault that has a top-level sibling folder outside
  `Knowledge Base/`
- **THEN** the personalize step proposes and (with `--yes` or confirmation) writes
  `Knowledge Base/_access.yaml`, and the summary shows a `personalize` line

#### Scenario: Re-run converges
- **WHEN** `setup` runs a second time on an already-governed vault
- **THEN** the personalize step reports it skipped because no sibling folders need
  governing

#### Scenario: No sibling folders
- **WHEN** the vault has no folders outside `Knowledge Base/` to govern
- **THEN** the personalize step is skipped without writing a file

### Requirement: Cross-OS Runtime Recommendation

Setup and install-readiness documentation SHALL recommend a runtime shape based on
host capabilities and tradeoffs rather than forcing one universal path. Windows
live-vault installs SHALL default to native service guidance. Linux hosts with
NVIDIA container runtime SHALL be able to choose CUDA Docker as the low-friction
hybrid/GPU-capable route. Windows+WSL2 CUDA Docker SHALL be offered with an
explicit file-watcher bind-mount tradeoff. macOS Apple Silicon SHALL default to
native setup for MPS/MLX support.

#### Scenario: Windows native remains recommended

- **WHEN** setup or documentation addresses a Windows user with a live local vault
- **THEN** it recommends the native Windows service path by default
- **AND** it explains that Docker Desktop bind mounts can miss live file-watch events

#### Scenario: Linux NVIDIA can choose CUDA Docker

- **WHEN** setup or documentation addresses a Linux user with Docker and NVIDIA
  runtime available
- **THEN** it offers the CUDA Docker path as a supported one-command route
- **AND** it states that the service still boots resource-safe unless performance
  mode or an explicit CUDA device is selected

### Requirement: Deterministic Native Dependency Gates

Native service setup SHALL gate the selected dependency profile before declaring the
service installed or restarted successfully. A hybrid native service SHALL fail the
gate when `sentence-transformers`, `torch`, `Pillow`, or `sqlite-vec` are missing.
A media native service SHALL additionally fail the gate when media dependencies are
missing. Remediation SHALL name the locked `uv sync` command for the selected profile.

#### Scenario: Hybrid service missing embeddings fails before success

- **WHEN** native service setup or restart is run for a hybrid profile and
  `sentence-transformers` is not importable
- **THEN** the operation reports failure instead of declaring the service healthy
- **AND** the remediation names `uv sync --frozen --extra embeddings`

#### Scenario: Media service missing media dependencies fails before success

- **WHEN** native service setup or restart is run for a media profile and media
  dependencies are not importable
- **THEN** the operation reports failure instead of declaring the service healthy
- **AND** the remediation names the media extra and any required system tools

### Requirement: Runtime And Compute Diagnostics

The doctor command SHALL report the effective runtime and compute profile when that
information is available. The report SHALL distinguish native versus container
runtime, dependency profile, compute mode, selected torch device, CUDA availability,
and CUDA residency or initialization status when detectable. Diagnostics MUST
distinguish "CUDA-capable but not resident" from "running on CUDA".

#### Scenario: Docker CUDA image in normal mode reports capability separately

- **WHEN** doctor runs inside a CUDA-capable container in normal mode
- **THEN** it reports the CUDA image/runtime capability
- **AND** it reports normal compute mode and CPU-default selected device
- **AND** it does not describe CUDA as resident unless CUDA has actually been
  initialized

#### Scenario: Native hybrid install reports missing extras clearly

- **WHEN** doctor runs for a native hybrid profile without the embeddings extra
- **THEN** it reports the missing dependency checks as failures
- **AND** it does not require inspecting server logs to understand that search would
  degrade to lexical ranking

### Requirement: Terminal-Safe Doctor Output

Human-readable doctor output SHALL be safe on Windows consoles using legacy code
pages as well as UTF-8 terminals. It MUST NOT crash while rendering advisory text
because of a non-ASCII symbol. Any optional decorative symbol SHALL have an ASCII
fallback or be omitted.

#### Scenario: Windows cp1252 console renders doctor output

- **WHEN** doctor emits human-readable output on a Windows cp1252 console
- **THEN** it completes without `UnicodeEncodeError`
- **AND** all warnings, failures, and remediation text remain readable

### Requirement: Native release service bootstrap
The project SHALL expose one blessed native service-install command per platform.
Release mode MUST create or update a PyPI-backed service venv outside the checkout,
install the selected profile extras, and install and start the platform service.
Repository-venv mode SHALL remain available for development.

#### Scenario: Linux or macOS release install
- **WHEN** a user runs `bash scripts/install-service.sh --release --profile hybrid`
- **THEN** the installer creates or updates an external venv with
  `exomem[embeddings]` from PyPI
- **AND** it installs and starts a systemd user service on Linux or a launchd user
  agent on macOS

#### Scenario: Repository development install
- **WHEN** a contributor selects `--repo-dev` or uses the historical no-mode form
- **THEN** the installer uses the checkout `.venv` without installing a PyPI
  release package

### Requirement: Profile-complete release environment
The release installer SHALL map lean, hybrid, and media profiles to their published
extras, load the selected dotenv file into preflight and service environments, and
MUST NOT depend on the checkout working directory for runtime dotenv discovery.

#### Scenario: Media profile on Apple Silicon
- **WHEN** release mode selects the media profile on macOS arm64
- **THEN** the PyPI requirement includes embeddings, media, vision, diarization,
  and the macOS MLX media extra

#### Scenario: Service receives dotenv values
- **WHEN** the selected `.env` contains Exomem vault and OAuth settings
- **THEN** those values are available to doctor and the installed service
- **AND** generated files containing secrets are readable only by the current user

### Requirement: Transactional readiness and endpoint verification
Native installers SHALL run the selected capability doctor and remote-environment
doctor before changing the service manager. They SHALL start the service only after
both gates pass and SHALL verify `/mcp` before reporting success.

#### Scenario: Doctor failure preserves service-manager state
- **WHEN** either doctor gate fails
- **THEN** the installer exits nonzero before invoking launchd, systemd, or NSSM
  installation commands

#### Scenario: Authenticated MCP endpoint is healthy
- **WHEN** the started service returns HTTP `401` from its local `/mcp` endpoint
- **THEN** the installer reports the service as installed and healthy

#### Scenario: Unauthenticated MCP endpoint fails closed
- **WHEN** the started service returns HTTP `200` from its local `/mcp` endpoint
- **THEN** the installer stops the service, exits nonzero, and reports that OAuth
  enforcement is missing

### Requirement: Standard multimodal product profile
Native release installers SHALL default to a `standard` profile that installs embeddings and
media extras, plus the MLX media extra on Apple Silicon. Existing `lean`, `hybrid`, and `media`
profile names SHALL remain accepted. Missing optional OS-level OCR tooling SHALL be reported
with remediation but MUST NOT prevent the remaining standard service from installing.

#### Scenario: Default release install
- **WHEN** a user runs a native release service command without an explicit profile
- **THEN** the release venv contains the standard embeddings and media extras
- **AND** the installed service uses demand-loaded disposable media workers

#### Scenario: Apple Silicon standard install
- **WHEN** the default release install runs on macOS arm64
- **THEN** the package requirement includes the MLX media extra
- **AND** ASR remains unloaded until media work arrives

#### Scenario: Explicit compatibility profile
- **WHEN** an existing automation selects lean, hybrid, or media
- **THEN** the installer preserves that profile's existing dependency mapping
- **AND** no profile name is silently reinterpreted

### Requirement: Doctor Infers The Available Capability Profile

When neither `--profile` nor `EXOMEM_PROFILE` selects a profile, doctor SHALL infer the highest locally installed capability profile without importing or loading models and without downloading assets. `EXOMEM_DISABLE_EMBEDDINGS` SHALL force lean inference.

#### Scenario: Embeddings install defaults to hybrid

- **WHEN** the embeddings dependency set is discoverable, media dependencies are not complete, and no profile is configured
- **THEN** doctor runs and labels the report as `hybrid`

#### Scenario: Lean install remains lean

- **WHEN** embeddings dependencies are unavailable or explicitly disabled and no profile is configured
- **THEN** doctor runs and labels the report as `lean`

#### Scenario: Explicit profile wins

- **WHEN** a valid profile is supplied by CLI or environment
- **THEN** doctor uses that profile regardless of inferred installed capabilities

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

### Requirement: Lean suite is time-bounded and diagnosable
Each required lean Python matrix lane SHALL request pytest session termination between test items at 1,500 seconds through `--session-timeout=1500`, retain the repository's sixty-second per-item timeout, and run inside a GitHub job deadline no greater than thirty minutes. The lane SHALL report its slowest tests and write JUnit timing evidence whose path and immutable artifact name both include the matrix Python version. CI SHALL attempt to upload that evidence with `if: always()` and `if-no-files-found: warn` after ordinary success or test failure. The GitHub job deadline is the hard process bound for collection, a final in-flight item, teardown, plugin, or runner hangs and does not require an artifact from a forcibly terminated job.

#### Scenario: Contended runner reaches the requested session stop
- **WHEN** a contended runner reaches 1,500 seconds between test items
- **THEN** pytest requests session termination, reports the available failure and duration evidence, and leaves the existing per-item timeout plus outer job ceiling to bound an item already in flight

#### Scenario: Test process hangs outside its session lifecycle
- **WHEN** collection, teardown, a plugin, or the runner prevents the pytest session deadline from completing cleanly
- **THEN** the GitHub job terminates no later than thirty minutes rather than consuming the platform default timeout

#### Scenario: Ordinary lane completion preserves timing evidence
- **WHEN** a matrix lane passes or fails normally
- **THEN** its log identifies the slowest bounded set of tests and its matrix-version-specific JUnit XML is uploaded under a matrix-version-specific artifact name for comparison

### Requirement: Release-critical concurrency tests assert semantics
Release-critical mutation, serialization, critical-section, and cleanup regression tests SHALL synchronize on explicit attempts, admissions, releases, states, or injected test budgets. They SHALL NOT treat completion within a quiet-runner wall-clock threshold as the product invariant. Any remaining timeout in such a semantic test SHALL be a generous deadlock/cleanup guard, while dedicated performance or budget tests MAY retain calibrated timing assertions with an appropriate control.

#### Scenario: Same-vault contention returns retryable backpressure
- **WHEN** a test deliberately holds the same-vault mutation boundary while other writes attempt entry
- **THEN** it asserts that refused attempts are non-committed and safely retry to complete canonical state after release rather than requiring every write to fit the production acquisition timeout

#### Scenario: Boundary placement is observed structurally
- **WHEN** narrow and wide mutation modes evaluate the same validator
- **THEN** the test distinguishes them by the observed mutation-boundary state at evaluation rather than elapsed milliseconds

#### Scenario: Cleanup semantics are separated from production budget
- **WHEN** a test proves that an expired checkpoint is tombstoned and pruned
- **THEN** it uses a test-only budget sufficient for semantic completion while separate tests retain responsibility for the production prune budget

### Requirement: Doctor Reports Resource Posture Without Heavy Allocation

The doctor command SHALL report the current resource posture for local installs
without mutating the repo, vault, environment, service state, model caches, or
CUDA state. The report SHALL identify the effective mode, CPU/GPU fallback
posture, whether CUDA is required for the requested profile, and whether the host
appears to be CPU-only or marginal for GPU use. The check MUST NOT load models,
download model files, create sidecars, or initialize CUDA solely for diagnostics.

#### Scenario: CPU-only host passes lean readiness

- **WHEN** `doctor --profile lean` runs on a host with no usable CUDA device
- **THEN** the doctor report does not fail because CUDA is absent
- **AND** it reports that CPU is the supported baseline for the current profile

#### Scenario: Marginal GPU produces remediation not failure for lean profile

- **WHEN** `doctor --profile lean` or `doctor --profile hybrid` detects that the
  GPU is absent, unavailable, or below the configured free-VRAM threshold
- **THEN** the report explains that Exomem will use CPU unless the user explicitly
  enables and satisfies GPU policy
- **AND** the check does not allocate CUDA to prove that result

#### Scenario: Resource posture appears in JSON output

- **WHEN** `doctor --json` is run
- **THEN** the JSON includes a resource-posture check with mode, policy, and
  best-effort GPU availability fields
- **AND** unknown probe results are represented as unknown or unavailable rather
  than as failures

### Requirement: Setup Recommends Safe Default Resource Mode

The setup flow SHALL keep the safe resource default unless the user explicitly
opts into performance mode. If setup detects a capable idle GPU, it MAY recommend
performance mode for faster indexing, but it MUST explain that normal mode avoids
steady-state CUDA residency and that quiet mode is available for gaming or other
foreground workloads.

#### Scenario: Capable GPU is discoverable but not silently enabled

- **WHEN** setup detects a capable idle GPU
- **THEN** setup may offer performance mode as an explicit option
- **AND** setup does not silently switch the user into performance mode without
  consent

#### Scenario: Setup documents quiet mode

- **WHEN** setup completes
- **THEN** the user-facing next steps mention the CLI command for entering quiet
  mode or inspecting resource status

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

### Requirement: Runtime Health Distinguishes Transport And Recall Admission

Local health surfaces SHALL distinguish process/transport liveness from retrieval admission. Retrieval SHALL be reported ready only when both required recall projections are live and both maintained catalogue checkpoints are proven exactly equal to those projections. A previously ready bit SHALL be revoked when that equality no longer holds. A process whose transport responds but whose projection/catalogue is warming or unavailable MUST NOT be reported as fully ready.

#### Scenario: Live transport with warming recall is not fully ready

- **WHEN** the service responds to health probes while required recall projection or catalogue proof is incomplete
- **THEN** liveness reports the process as running
- **AND** readiness reports retrieval as warming or unavailable with `admitted=false`

#### Scenario: Converged repair updates readiness

- **WHEN** a later background repair proves both maintained catalogues against live recall checkpoints
- **THEN** readiness reports retrieval as ready with `admitted=true`
- **AND** installers and deployment acceptance can distinguish that state from a generic HTTP response

#### Scenario: Stale catalogue proof revokes readiness

- **WHEN** either live projection advances beyond the maintained catalogue checkpoint after admission
- **THEN** health reports retrieval as warming or unavailable with `admitted=false`
- **AND** background repair must prove the new equality before readiness returns

### Requirement: ASR accelerator readiness binds the runtime actually used
Media installation and doctor checks SHALL distinguish the ASR runtime from unrelated model frameworks. Accelerator readiness SHALL bind `ctranslate2>=4.6.3,<5`, `nvidia-cublas-cu12>=12.8.4.1,<13`, `nvidia-cuda-runtime-cu12>=12.8.90,<13`, and `nvidia-cudnn-cu12>=9.5.0.50,<10`, Exomem's conservative computation policy, the device capability reported by that ASR engine, and a real model-execution probe when an explicit probe is requested. Engine capability reporting alone SHALL NOT override Exomem's known-safe computation policy. Wheel-owned CUDA libraries SHALL be placed on the loader path before the ASR child process starts; mutating `LD_LIBRARY_PATH` after process startup SHALL NOT be treated as readiness.

#### Scenario: PyTorch CUDA works but ASR CUDA does not
- **WHEN** the torch accelerator probe succeeds but the ASR engine cannot load or execute against its required native runtime
- **THEN** install readiness reports ASR acceleration unavailable
- **AND** it does not claim that the torch runtime repairs or proves the ASR runtime

#### Scenario: ASR CUDA works without PyTorch
- **WHEN** CTranslate2 reports a usable CUDA device and supported computation types but torch is not installed
- **THEN** ASR accelerator admission may succeed
- **AND** the absence of torch does not force the disposable ASR worker onto CPU

#### Scenario: Blackwell-capable media install
- **WHEN** a standard or media profile is installed on a supported Blackwell host
- **THEN** the ASR engine can resolve a compatible CUDA runtime and select a supported computation type
- **AND** an explicit media GPU probe executes model compute before reporting success

#### Scenario: Incompatible system cuBLAS precedes the service
- **WHEN** the host loader would otherwise resolve an older `libcublas.so.12`
- **THEN** the parent launches the disposable worker and explicit verifier with the media extra's compatible CUDA library directories first
- **AND** a fresh subprocess proves the selected native component identities and versions meet the pinned floors before claiming readiness

#### Scenario: Wheel name matches but native version is too old
- **WHEN** a selected cuBLAS, CUDA-runtime, or cuDNN component reports a version below the supported floor
- **THEN** ASR readiness fails with the component name, selected identity, observed version, and required floor
- **AND** path precedence or package presence alone does not satisfy readiness

#### Scenario: CPU-only installation
- **WHEN** no accelerator is present
- **THEN** the profile remains installable and reports bounded CPU ASR
- **AND** accelerator absence does not make the core service unhealthy
