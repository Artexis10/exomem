## Why

Exomem already computes an ACT-R base-level activation `B = ln(Σ w_j·Δt_j^-0.5)` (decay
`d=0.5`, weights citation > read > surfaced) over the JSONL access logs it already writes for
every `find` (`logs/queries.jsonl`), `get` (`logs/reads.jsonl`), and cited write
(`logs/writes.jsonl` `cited_sources`) — see the shared canonicalizer/JSONL reader
(`_relevance_canon`, `_relevance_read_jsonl`) at `src/kb_mcp/audit.py:866-887` and the event
extraction/activation math (`_stale_access_events`, `_activation`) at
`src/kb_mcp/audit.py:1106-1179`. By deliberate design this activation only orders the human
stale-review queue and never touches `find` ranking — the docstrings say so explicitly at
`audit.py:1085-1086` ("These weight the review-queue SORT only — they never touch the
stale_review gate or `find` ranking") and `audit.py:1191-1192` ("it never decays, down-ranks,
moves, or hides anything (`find` ordering is unchanged)").

That split leaves a real gap: exomem has usage evidence sitting on disk that `find` never
consults, while the engram-memory competitor ships something structurally similar — an
activation-weighted retrieval signal — baked into ranking by default, with no kill switch and
no transparency into why it fired. This change ports the one genuinely good mechanism from that
comparison — a bounded, deterministic, explainable usage-activation boost — as an **opt-in**
`find` behavior, and explicitly rejects the rest of that design (a feedback tool, hidden
pairwise-preference state, decay-as-demotion, a retrieval gate that can silently drop results;
see design.md Non-Goals). The boost is deterministic given (vault state, logs, config, current
date): same inputs, same output, every time. Default `find` ranking (`prefer_used=False`, the
default) stays byte-identical to today.

Sequencing: this change is authored to land **after** `improve-find-latency-token-cost` (adds
the hot in-process `find` cache, opt-in timing, and compact/full detail) and after
`reduce-find-per-query-overhead` (a sibling change being authored in parallel that consolidates
`find`'s existing sequential post-RRF multiplier passes). Both touch the same ranking/caching
surface this change extends — the cache-bypass rule and the "4th post-RRF multiplier" framing
below assume they have landed first.

## What Changes

- Extract the activation math already living in `audit.py` into a new `src/kb_mcp/usage.py`
  module; `audit.py` delegates to it with byte-identical behavior for the existing stale-review
  audit.
- Add an opt-in `prefer_used: bool = False` parameter to `find`/`op_find`, symmetric with the
  existing `prefer_compiled`/`prefer_active` family, that applies a bounded, positive-only
  multiplicative post-boost to hits `find` already surfaced, based on their usage-activation
  (find-surfaced, read, cited).
- Add `usage_boost`, `usage_decay`, `usage_horizon_days`, `usage_w_surfaced`, `usage_w_read`,
  `usage_w_cited` knobs to `RankingConfig`, sweepable via `ranking_config.json` like every other
  ranking knob.
- Surface `activation` (raw `B`, rounded) and `usage_boost` (the multiplier applied) in a hit's
  `signals` when the boost is active and changes that hit's score.
- Add kill-switch `KB_MCP_DISABLE_USAGE_BOOST`; the boost is a strict no-op (every multiplier
  exactly `1.0`) on cold start (no logs yet), with query logging disabled
  (`KB_MCP_DISABLE_QUERY_LOG`), or with embeddings/test-mode disabled
  (`KB_MCP_DISABLE_EMBEDDINGS`).
- Record `prefer_used` in `query_log.log_find_call`; bypass the hot `find` cache (from
  `improve-find-latency-token-cost`) whenever `prefer_used=True`.

No server-side reasoning model is added. The boost is pure arithmetic over logs exomem already
writes for other purposes.

## Capabilities

### New Capabilities

- `find-usage-activation`: opt-in usage-activation post-boost for `find` — its bounds relative
  to the existing epistemic-hierarchy (`prefer_compiled`) and supersession (`prefer_active`)
  boosts, activation transparency in `signals`, usage-snapshot freshness, the "never creates
  candidates" guarantee, and the non-goal that default `find` ranking stays usage-blind.

### Modified Capabilities

- None.

## Impact

- Code: new `src/kb_mcp/usage.py`; `src/kb_mcp/audit.py` (delegates activation math to the new
  module, docstring amendment); `src/kb_mcp/find.py` (`RankingConfig` knobs, `prefer_used`
  plumbing, post-rerank re-application, `signals` additions); `src/kb_mcp/commands.py`
  (`op_find` parameter + docstring); `src/kb_mcp/query_log.py` (`log_find_call` records
  `prefer_used`).
- Surfaces: unified `find` command registry, MCP schema, REST facade, CLI, OpenAPI output, and
  the committed MCP schema-fidelity fixture (`tests/fixtures/mcp_tool_schemas.json`).
- Tests: parity test for the audit.py→usage.py extraction, formula unit tests, keyword/hybrid
  integration tests with JSONL fixtures, never-creates-candidates test, dominance-invariant
  tests, cold-start/disabled-logging no-op tests, snapshot invalidation/reset tests, rerank-path
  application test, `ranking_config.json` roundtrip of the new knobs, registry/MCP schema
  fixture regen, query-log field test, hot-cache bypass test.
- Dependencies: none new. Reuses the JSONL logs already written by `query_log.py` and stdlib
  math (`math.log`, logistic sigmoid).
- Depends on: `improve-find-latency-token-cost` (adds the hot `find` cache this change bypasses)
  and `reduce-find-per-query-overhead` (sibling, authored in parallel; consolidates the
  post-RRF multiplier passes this change adds a 4th factor to) landing first.
