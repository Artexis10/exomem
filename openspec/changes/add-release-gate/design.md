# Design: add-release-gate

## Context

Verified anchors. The recall serialize block is `commands.py:908-922`
(`Hit.as_dict`/`as_compact_dict` + ref attachment); `hits = find_module.find(...)`
returns at `:901`; `assemble_pack` runs at `:905`. The find hot-cache stores
deep copies of `Hit`s shared across principals (`find.py:1063-1067`). The one
dispatcher shared by MCP/REST/hosted/CLI is `writer_lease.invoke_command`
(`:1279`); `bind_vault` (`command_surface.py:211-243`) covers MCP only, and the
`EXOMEM_RETRIEVE_INJECT` hook reaches memory over REST-then-CLI — the two paths
that skip `bind_vault`. Per-request identity is already derivable
(`command_surface.mcp_retry_scope:259`) but is used only for write idempotency and
never threaded to read leaves. Withhold-tokens reuse the hosted transfer-grant v2
+ `consume_transfer_jti` pattern (`hosted_transfer.py:218-353`); the HMAC key is a
per-machine `SystemRandom` value in the sidecar meta (`sidecar_store` precedent),
never an env secret, never synced.

## Goals / Non-Goals

Goals: enforce the kernel's ceilings on every content-returning surface with no
leak channel (metadata, counts, scores, graph seeds, provenance, errors);
preserve baseline behavior and latency for ungoverned vaults; ship the default-on
credential scrubber; make the projector the single serializer so a missed surface
fails closed loudly.

Non-Goals: the `govern_memory` tool, receipts, bridges, L4 redaction span maps,
budgets. No change to ranking/recall for permitted items. No new model call
anywhere.

## Decisions

### D1 — Enforce at `invoke_command`, second pass at `bind_vault`
The terminal secret scrubber + withheld cross-check live in a shared
`governance.egress.postfilter(command_name, result, vault_root)` called inside
`writer_lease.invoke_command` (covers MCP/REST/hosted/CLI in one place), with a
second call in the `bind_vault` wrapper where FastMCP context is live (MCP
defense-in-depth). This removes the entire REST/CLI/retrieve-inject bypass class.
The walker special-cases MCP `ToolResult` content blocks (scan text blocks only,
never image bytes from `op_get_video_frames`). Documented residuals: hand-
registered adoption tools and the transfer routes bypass `invoke_command` and get
explicit `postfilter` calls at their handlers; `query_log` records hits
pre-annotation into the never-synced local `logs/` (existing local-trust-domain
behavior).

### D2 — Decisions in op_find, cache stays principal-free
`governance.egress.annotate_hits(vault_root, hits, principal, purpose)` runs at
`commands.py:901→902`, strictly after `find()` returns and before
`assemble_pack` (`:905`) and serialize (`:908`). Nothing principal-dependent
enters `find()` or `_FIND_CACHE`; a separate small memo keys decisions per
`(fingerprint, path, audience, purpose, grants-hash)`. A two-principal cache test
pins that cached `Hit` copies carry `decision=None`. Purpose never enters the
`_FIND_CACHE` key (would let model-declared purpose bust the relevance cache).

### D3 — Per-level projector is the only serializer
`project(payload, level)` is the sole path to a wire dict. Level allow-lists: L0
nothing (item omitted); L1 rule-id + scope-label only; L2 +constraint string;
scores/signals/`graph.seed`/`relation_match`/`matched_units`/`superseded_by`/
`parent_ref` only at L5–L6, and any of them naming a sub-notice path stripped at
every level. `find_types` raw serializers are removed from the egress path; a
command with no registered projector fails closed (cannot emit
path/title/excerpt) — a missed surface becomes a loud test failure. This collapses
the metadata/score/provenance oracle class into one grep-able invariant.

### D4 — Request-deterministic backfill; L0 silent
Withheld slots are refilled from a pre-committed over-fetch pool so the shown
count is a function of the request, not of how many items were withheld. At pool
exhaustion, L1+ scopes still show their notice; **L0 scopes return a silently
shorter list** — a fixed "governance active" marker would itself be an existence
oracle for a silent scope. Graph-expanded hits whose only provenance is a
withheld seed are dropped post-hoc (they return only if they matched a lane on
their own), plus `guard_seed` in `graph_context`.

### D5 — Canonical audience, threaded, fail-closed
`governance.principal` exposes one `RequestPrincipal(audience_id, session_id,
purpose)` via a contextvar + `request_scope()` (clone of
`capabilities.active_surface` / `mcp_request_context` set/reset). Set-points:
`bind_vault` wrapper (MCP, where `mcp_retry_scope` already binds), `_rest_gate`
(REST), hosted invoke wrapper (cell principal), `__main__` (CLI = `owner`). One
normalization maps OAuth `sub`/`iss`, CF-Access claims, REST key, and stdio to a
single comparable id so a grant authored on one surface matches the same human on
another. Unresolved-but-expected identity fails closed to most-restrictive, never
OPEN. `purpose` is an optional param on the recall leaves; `derive_params`
propagates it to REST/CLI.

### D6 — Withhold-tokens: content-fingerprint-bound, consume-once, box-local
Token wire form `wh1.<jti>.<exp>.<hmac>`, HMAC over
`{jti, item-fingerprints, audience, exp}`. Bound to **content fingerprints**, not
paths, so a content swap after mint fails closed (with a one-step fresh-escalation
hint — no re-confirm treadmill) and approval-by-substitution is impossible.
Consumed once under `BEGIN IMMEDIATE` in `.governance.sqlite withhold_tokens`
(JTI-consume semantics); TTL sweep opportunistic. HMAC key is per-machine sidecar
meta. "Session" on stdio is the canonical audience + lease identity, never a
shared `session_id`.

### D7 — Always-on credential scrubber
The secret scrubber is content-pattern-based and policy-independent: it runs even
on the empty-policy fast path (the one intentional ungoverned behavior change).
Compiled alternation over private-key blocks, AWS/GitHub/JWT/bearer shapes, and
high-entropy tokens; a structural-field allowlist (`content_hash`, `ref`,
`fingerprint`, `expected_hash`) prevents false positives. A standing rule disables
it. Budget target < 2 ms per 100 KB result, pinned by the overhead micro-gate.

## Risks / Trade-offs

- **`bind_vault` is not the universal choke point** (verified): mitigated by D1
  (`invoke_command` primary). A startup assertion checks every product command
  resolves to a projector-registered leaf.
- **Byte-pinned tool surface**: adding `purpose` changes
  `tool_surface_contract.json` and schema-fidelity pins; regenerate via
  `scripts/dump-tool-schemas.py` in-task, never hand-edit.
- **Latency gate bypasses `op_find`** (calls `find()` directly): governance
  overhead is invisible to it, so a new `test_governance_overhead.py` micro-gate
  measures at the `op_find` level over the same `gen_dense_vault` corpus; existing
  ceilings untouched.
- **Scrubber false positives**: allowlist by structural key name before the
  pattern scan; test pins zero mutations on a representative result corpus.

## Migration Plan

Empty policy → fast path; only the credential scrubber changes behavior on an
ungoverned vault (owner-confirmed default-on, one rule to disable). Tool-surface
regen is the only artifact churn. No data migration.

## Open Questions

None blocking. L4 redaction detail (span maps) and budgets are explicitly later
changes; this change ships L0/L1/L2/L3/L5/L6 with L4 falling back to L3 abstract
until `add-redaction-levels` lands.
