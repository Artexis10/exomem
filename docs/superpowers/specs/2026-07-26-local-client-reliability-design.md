# Local client reliability and truthful recovery guidance

Date: 2026-07-26
Status: approved

## Problem

Four failures were reported together, but they have separate causes.

First, local MCP clients are registered through stdio by default. Every active
Claude Code or Codex session therefore owns a complete Exomem server process. The
July resource work made model loading lazy and media workers disposable, but a
session that performs semantic retrieval or a write can still load its own
BGE/Torch-MPS stack. The Claude Code plugin began declaring that full stdio
server on July 20, matching the reported return of multi-gigabyte processes after
the upgrade. Restarting appears to fix the issue because process exit is the only
reliable boundary for reclaiming every imported model runtime and allocator.

Second, `doctor` currently totals `ps` RSS on macOS. That is not the physical
footprint shown by Activity Monitor and can understate model-heavy processes by
gigabytes.

Third, the reviewed-none creation handshake is internally sound but publicly
contradictory. Runtime accepts `reviewed_none`, remediation says
`reviewed-none`, and validation returns `draft_hash` while bootstrap tells the
caller to use a separately named relation-review hash.

Fourth, an editable checkout can import the current source version while its
environment still contains dependencies resolved against an older `uv.lock`.
Version output therefore looks current even though the runtime is only
half-upgraded, and `doctor` does not expose the split.

The same-basename adoption overwrite and the original entity-capture nudge are
already repaired and covered by regressions. They stay out of scope unless a
fresh reproduction from the omitted incident attachment contradicts that
evidence.

## Design

### Prefer the existing shared service when one is configured

`exomem setup` will model client registration as one explicit route:

1. An explicit `--mcp-url` selects the shared HTTP service.
2. Otherwise a valid `EXOMEM_BASE_URL` from cwd `.env` selects its `/mcp`
   endpoint, matching the server's override behavior.
3. Otherwise a valid inherited `EXOMEM_BASE_URL` selects its `/mcp` endpoint.
4. Otherwise setup retains the existing stdio registration as the compatibility
   fallback.

An explicit `--stdio` option forces the fallback. Public service URLs must use
HTTPS. Loopback HTTP remains valid only for an already-supported local service.
URLs containing credentials, query strings, fragments, or unrelated paths are
rejected before client configuration changes.

Claude Code and Codex will receive native streamable-HTTP registrations rather
than a proxy process. Both clients already support HTTP and OAuth. Setup will
print the exact client-login follow-up and will never persist bearer tokens.
Interactive replacement still requires confirmation. Non-interactive setup
changes an existing registration only when the caller passes
`--replace-client-registration`, so a stale URL cannot silently mask a working
stdio route.

The Claude plugin stops declaring a full stdio core. It continues to provide
skills and hooks, and its MCP component becomes an optional HTTP placeholder
backed by `userConfig.mcp_url`. The configured value must be the full canonical
`/mcp` URL so Claude can deduplicate it against an identical manual route. A
blank value is inert ("not configured") on the supported Claude Code floor,
2.1.208, and never launches an Exomem process. Local-only users run
`exomem setup --stdio` to receive the existing manual stdio registration.

Manual setup remains the authoritative compatibility path. It inspects
`claude plugin list --json` without health-checking or starting MCP servers. If
an enabled legacy Exomem plugin still declares stdio, setup refuses to claim the
shared route is complete and gives the exact plugin-update command. The wizard
also replaces its broken `claude mcp list --scope` probe with direct,
non-starting configuration inventory across local, project, and user scopes.
Higher-precedence local/project entries cannot silently shadow a desired user
route. Confirmed replacement snapshots every affected Claude configuration and
restores all of them if any removal/add step fails.

`EXOMEM_BASE_URL` is read from the cwd `.env` written by remote setup before the
inherited process environment, matching the service's effective precedence
without mutating the caller's environment. A present but invalid value is an
error rather than a silent stdio fallback.

This design fixes the supported path instead of introducing another daemon,
weakening OAuth for localhost, or attempting to share model memory between
unrelated stdio processes.

### Make install consistency a read-only doctor invariant

For an editable Exomem distribution backed by a checkout containing
`pyproject.toml` and `uv.lock`, `doctor` will run a bounded, offline check against
the active interpreter environment:

```text
uv sync --check --locked --no-dev --active --project <checkout> --offline --no-cache --inexact
```

`VIRTUAL_ENV` is pinned to `sys.prefix`. `--check`, `--locked`, and `--offline`
preserve the doctor contract: no package change, lock rewrite, download, model
load, or vault mutation. `--no-dev` checks the runtime rather than the repo's
default development group, and `--inexact` prevents unrelated optional packages
from being treated as drift. The hybrid profile adds the `embeddings` extra;
standard and media add `embeddings` and `media`. `media-mlx` is required only
when the configured ASR backend explicitly selects MLX, never merely because the
host is Apple Silicon.

Setup atomically merges the selected `cli_profile` into Exomem's install state
without erasing managed-service or unknown fields. Doctor resolves an explicit
argument first, then `EXOMEM_PROFILE`, then that persisted value, before falling
back to dependency inference. The lock check always uses the resolved intended
profile, so a missing optional dependency cannot downgrade the check that was
supposed to catch it. An older uv that lacks the required safety flags produces
an unverifiable warning, not a false dependency-drift failure.

An outdated environment is a failure with the exact `uv sync` remediation.
Missing `uv`, a timeout, malformed distribution metadata, or an unverifiable
editable origin is a warning rather than a crash. Wheel and managed release
installs skip the checkout-lock comparison.

The report distinguishes source version, distribution metadata version,
interpreter, and editable origin so a current import can no longer masquerade as
a complete upgrade.

### Report the memory metric macOS users actually see

Process discovery remains the existing bounded `ps` query. On Darwin, each
matching PID is additionally sampled through an exact complete, size-checked
`rusage_info_v0` structure passed to
`proc_pid_rusage(..., RUSAGE_INFO_V0, ...)`, using
`ri_phys_footprint` as the selected memory value. The result exposes the metric
name rather than presenting an unlabeled number.

Each row retains `rss_mb` for compatibility and adds
`physical_footprint_mb` when available, plus `memory_mb` and `memory_metric` for
the selected value. Aggregates retain the old RSS total. Uniform rows add a
selected total with its label; mixed native/fallback rows are labelled `mixed`
and expose separate physical-footprint and RSS-fallback totals, never a fake sum.
If the symbol, structure, permission, or PID lookup fails, doctor falls back to
explicitly labelled RSS for that process instead of failing the entire report.

The release resource-envelope script uses the same helper so release gates and
doctor cannot disagree about the metric on macOS. Its existing RSS limit remains
RSS-only; a distinct physical-memory limit is skipped when native sampling is
incomplete. A real Apple-Silicon smoke
must compare known PIDs with Activity Monitor before the change is claimed as
verified.

### Make reviewed-none executable from validation alone

Creation validation adds an additive `relation_review_hash` field. When
`reviewed_none_required` is true it equals the returned `draft_hash`; otherwise
it is null. Replacement validation rewrites both public fields to the same
predecessor-bound hash before returning. Existing `draft_hash` and transition
fields remain unchanged.

Bootstrap and semantic remediation will give the exact second call:

```text
relation_disposition="reviewed_none"
relation_review_hash=<relation_review_hash returned by validate_only>
relation_review_reason=<explicit reason no relation applies>
```

The public boundary accepts both the canonical `reviewed_none` and the
previously advertised `reviewed-none`, then normalizes before validation,
idempotency digesting, replay comparison, and receipt persistence. Stored review state remains
canonical. The alias does not weaken the three-field handshake: the hash must
still match the unchanged draft and the reason must still be explicit and
bounded. Invalid values name the accepted spellings.

## Error handling and safety

- Service registration is prepared and validated before an existing client
  registration is removed. A failed replacement restores the prior explicit
  configuration and leaves plugin state untouched.
- HTTP registration never copies OAuth credentials into repository or client
  configuration.
- Editable-environment inspection is offline, bounded, and non-mutating.
- Darwin native-memory inspection is optional at runtime and always falls back
  to labelled RSS.
- Reviewed-none input normalization happens before persistence so no second
  vocabulary enters durable review artifacts.
- No existing vault content, relation review, or client token requires data
  migration.

## Verification

Automated coverage will prove:

- explicit service URL, configured-service auto selection, forced stdio,
  invalid URL refusal, idempotent re-run, confirmed replacement, and Codex TOML
  fallback rendering;
- the plugin's legacy full-stdio declaration is gone, its blank HTTP placeholder
  is inert on Claude Code 2.1.208+, and a canonical configured endpoint dedupes
  against the same manual route;
- setup reads cwd `.env`, inventories explicit scopes without starting servers,
  blocks on an enabled legacy stdio plugin, and rolls back a failed replacement;
- stale and current editable environments, uv absence, timeout, wheel skip, and
  bounded doctor output;
- Darwin physical-footprint success and per-PID RSS fallback without importing
  optional model stacks;
- zero-candidate `validate_only` returns the exact hash needed for commit, both
  spellings reach the same canonical receipt, and wrong hash or missing reason
  still fails;
- replacement validation returns a predecessor-bound `relation_review_hash`
  equal to its public `draft_hash`, and alias/canonical calls share one explicit
  idempotency receipt;
- OpenSpec validation, generated MCP schema/capability fidelity, lint, public
  artifact privacy, focused tests, and the full lean suite.

Final acceptance on Yusuke's Mac is operational: update and `uv sync`, rerun
setup against the existing service URL, authenticate the local clients, open
several Claude Code sessions, perform semantic work in each, and confirm one
heavy service process rather than one heavy model stack per session. Doctor's
per-PID total is compared with Activity Monitor, and a zero-candidate note is
committed using only fields returned by validation.

## Rollout and rollback

The release notes will give one post-upgrade sequence: synchronize the editable
environment, update the Claude plugin, `/reload-plugins` or restart every live
Claude session, upgrade/restart the existing service, rerun setup with the
service URL, authenticate clients, and run doctor. The
shared-service route is reversible by rerunning setup with `--stdio`; clearing
the plugin's optional endpoint leaves its skills and hooks intact. Doctor and
reviewed-none changes are additive and require no rollback migration.

This is an explicit plugin migration: an old plugin-only install loses its
auto-started stdio tools after update until the user runs `exomem setup --stdio`
or configures the canonical service endpoint. That break is intentional because
retaining the old default retains the multi-gigabyte fan-out.

Anything unique to the missing portion of the incident report is audited before
merge. It does not block these four reproduced repairs, but it cannot be claimed
fixed without the source text or a fresh live reproduction.

## Rejected alternatives

- **A new local daemon or stdio proxy:** duplicates an already-supported native
  service and adds lifecycle and authentication failure modes.
- **Unauthenticated trusted-localhost MCP:** weakens the mature OAuth boundary
  and is unnecessary when both target clients speak authenticated HTTP.
- **Per-project `disabledMcpServers` migration:** misses future projects and
  leaves stale opt-outs after the plugin changes transport.
- **Keeping plugin stdio behind manual HTTP precedence:** Claude deduplicates
  plugin/manual servers by endpoint, so different transports run side by side.
- **Shorter idle reaping or disabled embeddings:** reduces symptoms while
  preserving one heavy runtime per active session.
- **Treating `exomem --version` as upgrade proof:** editable imports make that
  demonstrably false.
