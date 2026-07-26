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
2. Otherwise a valid configured `EXOMEM_BASE_URL` selects its `/mcp` endpoint.
3. Otherwise setup retains the existing stdio registration as the compatibility
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

The Claude plugin keeps its current stdio MCP declaration. That preserves a
working plugin-only install for someone who has no service. A same-name manual
HTTP registration has higher precedence in current Claude Code, so configured
service users connect to one shared core while the plugin continues to provide
skills and hooks. Removing the manual registration is the rollback: plugin
stdio becomes active again.

This design fixes the supported path instead of introducing another daemon,
weakening OAuth for localhost, or attempting to share model memory between
unrelated stdio processes.

### Make install consistency a read-only doctor invariant

For an editable Exomem distribution backed by a checkout containing
`pyproject.toml` and `uv.lock`, `doctor` will run a bounded, offline check against
the active interpreter environment:

```text
uv sync --check --active --project <checkout> --offline --no-cache --inexact
```

`VIRTUAL_ENV` is pinned to `sys.prefix`. `--check` and `--offline` preserve the
doctor contract: no package change, download, model load, or vault mutation.
`--inexact` prevents unrelated optional packages from being treated as drift.
The hybrid profile adds the `embeddings` extra; standard and media add
`embeddings` and `media`; media on Apple Silicon also adds `media-mlx`.

An outdated environment is a failure with the exact `uv sync` remediation.
Missing `uv`, a timeout, malformed distribution metadata, or an unverifiable
editable origin is a warning rather than a crash. Wheel and managed release
installs skip the checkout-lock comparison.

The report distinguishes source version, distribution metadata version,
interpreter, and editable origin so a current import can no longer masquerade as
a complete upgrade.

### Report the memory metric macOS users actually see

Process discovery remains the existing bounded `ps` query. On Darwin, each
matching PID is additionally sampled through
`proc_pid_rusage(..., RUSAGE_INFO_V0, ...)`, using
`ri_phys_footprint` as the selected memory value. The result exposes the metric
name rather than presenting an unlabeled number.

Each row retains `rss_mb` for compatibility and adds
`physical_footprint_mb` when available, plus `memory_mb` and `memory_metric` for
the selected value. Aggregates retain the old RSS total and add a selected total
with its label. If the symbol, structure, permission, or PID lookup fails,
doctor falls back to explicitly labelled RSS for that process instead of
failing the entire report.

The release resource-envelope script uses the same helper so release gates and
doctor cannot disagree about the metric on macOS. A real Apple-Silicon smoke
must compare known PIDs with Activity Monitor before the change is claimed as
verified.

### Make reviewed-none executable from validation alone

Creation validation adds an additive `relation_review_hash` field. When
`reviewed_none_required` is true it equals the returned `draft_hash`; otherwise
it is null. Existing `draft_hash` and transition fields remain unchanged.

Bootstrap and semantic remediation will give the exact second call:

```text
relation_disposition="reviewed_none"
relation_review_hash=<relation_review_hash returned by validate_only>
relation_review_reason=<explicit reason no relation applies>
```

The public boundary accepts both the canonical `reviewed_none` and the
previously advertised `reviewed-none`, then normalizes before validation,
replay comparison, and receipt persistence. Stored review state remains
canonical. The alias does not weaken the three-field handshake: the hash must
still match the unchanged draft and the reason must still be explicit and
bounded. Invalid values name the accepted spellings.

## Error handling and safety

- Service registration is prepared and validated before an existing client
  registration is removed. A failed add leaves a concrete recovery command and
  does not touch plugin state.
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
- plugin-only stdio remains unchanged while an explicit HTTP route is generated
  for service users;
- stale and current editable environments, uv absence, timeout, wheel skip, and
  bounded doctor output;
- Darwin physical-footprint success and per-PID RSS fallback without importing
  optional model stacks;
- zero-candidate `validate_only` returns the exact hash needed for commit, both
  spellings reach the same canonical receipt, and wrong hash or missing reason
  still fails;
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
environment, upgrade/restart the existing service, rerun setup with the service
URL, authenticate clients, and run doctor. The shared-service route is
reversible by removing the explicit registration, which restores the plugin's
stdio fallback. Doctor and reviewed-none changes are additive and require no
rollback migration.

Anything unique to the missing portion of the incident report is audited before
merge. It does not block these four reproduced repairs, but it cannot be claimed
fixed without the source text or a fresh live reproduction.

## Rejected alternatives

- **A new local daemon or stdio proxy:** duplicates an already-supported native
  service and adds lifecycle and authentication failure modes.
- **Unauthenticated trusted-localhost MCP:** weakens the mature OAuth boundary
  and is unnecessary when both target clients speak authenticated HTTP.
- **Removing stdio from the plugin:** breaks plugin-only installs that have no
  managed service.
- **Shorter idle reaping or disabled embeddings:** reduces symptoms while
  preserving one heavy runtime per active session.
- **Treating `exomem --version` as upgrade proof:** editable imports make that
  demonstrably false.
