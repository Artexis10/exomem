## Context

The native Exomem service already provides one authenticated HTTP core per vault, while
local setup and the Claude plugin register stdio. Stdio is process-per-client by design:
every session that reaches semantic retrieval or post-write indexing can load a private
BGE/Torch runtime. The July lazy-load and disposable-media-worker work reduced when this
happens but could not pool unrelated processes. The same incident showed that macOS
process RSS, editable source version, and reviewed-none remediation are not truthful enough
to diagnose or recover the resulting state.

Claude Code and Codex both support native streamable HTTP and OAuth. The supported repair
can therefore reuse the existing service instead of adding a daemon, proxy, shared-memory
model runtime, or unauthenticated localhost mode.

## Goals / Non-Goals

**Goals:**

- Converge configured local clients on one existing HTTP service while retaining a working
  manual stdio fallback.
- Keep registration migration explicit, reversible, and free of persisted bearer tokens.
- Detect a stale editable uv environment even when source import reports the new version.
- Report Darwin physical footprint with honest metric labels and portable fallback.
- Make a zero-candidate reviewed-none commit possible using only the validation response and
  published instructions.

**Non-Goals:**

- Build or auto-start a new service, proxy, or model-sharing daemon.
- Remove the Claude plugin's skills/hooks or weaken HTTP OAuth.
- Reduce model quality, disable embeddings, or change retrieval ranking.
- Reopen already-fixed adoption collision or entity-nudge behavior without a reproduction.
- Claim fixes for the omitted portion of the incident report before it is supplied or
  reproduced.

## Decisions

### 1. Represent registration as a validated route

Setup resolves exactly one desired route. Explicit `--mcp-url` wins, then a valid
`EXOMEM_BASE_URL` from cwd `.env` (matching the server's override behavior), then a valid
inherited value, then stdio. `--stdio` forces stdio and is
mutually exclusive with `--mcp-url`. A bare origin is normalized to `/mcp`; an existing
`/mcp` path is accepted. Credentials, query strings, fragments, unrelated paths, public
plain HTTP, and malformed ports are rejected. Plain HTTP is accepted only for loopback.

The route object renders both client forms: Claude's `--transport http` invocation and
Codex's `--url` invocation/TOML. This avoids duplicating validation and keeps credentials
out of configuration. OAuth login remains a client operation printed as a next step.

The plugin no longer declares a full stdio core. It retains skills/hooks and declares an
optional HTTP server whose `userConfig.mcp_url` value is either blank or the full canonical
`/mcp` URL. Blank is inert on the supported Claude Code floor (2.1.208); an identical
configured plugin/manual URL deduplicates by endpoint. Local-only users receive stdio from
manual setup rather than plugin startup.

Setup reads `EXOMEM_BASE_URL` from the cwd `.env` written by remote setup, then the process.
A present invalid value fails closed. Before claiming a shared route, setup inspects
`claude plugin list --json` without starting servers and blocks with an update command if an
enabled legacy Exomem plugin still declares stdio.

Alternative rejected: relying on manual-server precedence does not suppress plugin stdio;
Claude matches plugin/manual duplicates by endpoint, so different transports run together.

### 2. Keep registration replacement deliberate

An absent registration is added. An existing registration is left alone in noninteractive
mode unless `--replace-client-registration` is present; interactive mode asks. Replacement
removes explicit Claude scopes through Claude's CLI, writes Codex through its CLI when
available, and preserves the existing parse-before-write/backup fallback for Codex TOML.
The desired route is fully validated before removal. Claude inventory reads configuration
directly instead of invoking health-checking `mcp list/get`, and includes every applicable
local, project, and user scope. Higher-precedence local/project registrations cannot be left
to shadow a desired user route. Confirmed replacement snapshots all affected explicit
configuration and restores it if any removal/add step fails. Plugin state is never disabled
or rewritten by setup.

Alternative rejected: silently replacing every registration during `--yes` could turn a
stale environment URL into a complete loss of a working fallback.

### 3. Check editable lock parity with uv's own resolver

Install metadata gains an internal, validated editable project root derived from
`direct_url.json`. Doctor invokes `uv sync --check --locked --no-dev --active --project
<root> --offline --no-cache --inexact` with `VIRTUAL_ENV=sys.prefix` and a bounded
timeout. Setup atomically merges the selected `cli_profile` into the install manifest while
preserving service and unknown fields; doctor resolves an explicit argument, then
`EXOMEM_PROFILE`, then that persisted value before dependency inference, and the check uses
that intended profile. Extras are embeddings for hybrid and embeddings+media for
standard/media. `media-mlx` is added only when the configured ASR backend explicitly
selects MLX. `--inexact` tolerates extra optional packages but still requires the selected
locked runtime set.

Outdated is a failure; an unavailable or unverifiable check (including an older uv that
does not support the safety flags) is a warning; non-editable installs skip it. Output is
summarized and never echoes an unbounded transaction plan.

Alternative rejected: distribution/import version comparison alone cannot detect a
current editable source tree over old but still importable dependencies.

### 4. Centralize process memory selection

A dependency-free helper keeps `ps` RSS as the portable value and, on Darwin, calls
`proc_pid_rusage` only after defining and checking the exact complete stable
`rusage_info_v0` layout and size, then obtains
`ri_phys_footprint`. Doctor and the resource-envelope verifier use the same helper. Rows
retain `rss_mb` and add the selected `memory_mb`/`memory_metric`; Darwin adds
`physical_footprint_mb` when available. Native errors and exited PIDs fall back to labelled
RSS rather than failing diagnostics. Mixed rows are labelled `mixed` and expose separate
physical-footprint and RSS-fallback totals; they are never summed under one metric. The
resource verifier preserves `--max-rss-mb` as an RSS limit and uses a separate selected
physical-memory limit, declining that comparison when native sampling is incomplete.

Alternative rejected: continuing to compare macOS RSS with Activity Monitor makes the
release gate capable of passing while several gigabytes are resident.

### 5. Canonicalize reviewed-none before durable validation

Creation validation adds `relation_review_hash`, equal to `draft_hash` only when a
reviewed-none decision is required. Replacement validation rewrites both public fields to
the same predecessor-bound hash. Public handling accepts `reviewed_none` and the previously
advertised `reviewed-none`, canonicalizes to the underscore form before writer-lease
idempotency digesting, and then performs all existing hash/reason/replay checks. Durable
receipts remain underscore-only. Bootstrap and semantic remediation name the exact three
arguments and the validate-only round trip.

Alternative rejected: correcting prose without accepting the advertised spelling leaves
older cached skills and agents on a needless failure path.

## Risks / Trade-offs

- **A configured URL can be stale** → validate strictly, never replace implicitly in
  noninteractive mode, and restore the snapshotted explicit route or rerun `setup --stdio`.
- **OAuth requires a fresh client login** → print exact Claude/Codex login commands and
  never store tokens in setup state.
- **Claude CLI output is not a stable machine API** → keep parsing bounded and treat
  unknown output conservatively; all mutation still goes through the client CLI.
- **The uv check adds doctor latency** → run only for editable installs, offline, with a
  timeout and bounded output.
- **Darwin ABI or PID access can fail** → use the oldest sufficient structure and fall
  back per PID to labelled RSS.
- **An alias could fork durable vocabulary** → normalize before every receipt comparison
  and persistence boundary.

## Migration Plan

1. Ship setup, plugin, doctor, and semantic-contract behavior.
2. Existing users synchronize the editable environment, update the Claude plugin,
   `/reload-plugins` or restart live Claude sessions, and upgrade/restart the existing
   service. Plugin-only local users explicitly run setup with stdio or configure HTTP.
3. Service users rerun setup with `--mcp-url <origin>/mcp` and, when replacing an old
   route noninteractively, `--replace-client-registration`.
4. Users authenticate each native client and run doctor.
5. Rollback reruns setup with `--stdio`; clearing the optional plugin endpoint leaves its
   skills/hooks active. Doctor and relation-review additions require no data rollback.

## Open Questions

None for implementation. A real Apple-Silicon acceptance pass and the omitted incident
attachment remain release evidence, not unresolved design choices.
