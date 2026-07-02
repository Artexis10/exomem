# Design - usage-aware find ranking

## Context

`find`'s hybrid ranker already applies two opt-in, default-on post-RRF multiplicative boosts
before returning hits: `prefer_compiled` (a small boost for compiled types — insight, pattern,
failure, research-note, entity — and a small penalty for raw `source`, `_type_multiplier` /
`_apply_type_boost`, `find.py:1196-1240`) and `prefer_active` (a soft demotion for
`status: superseded` tombstones, `_status_multiplier` / `_apply_status_demotion`,
`find.py:1206-1259`). Both are applied twice: once to the fused `(path, score)` list before hits
are built (`find.py:915-918`, inside `_find_semantic`), and — when the CrossEncoder reranker
ran — re-applied to `Hit.rerank_score` so the boost survives the post-rerank sort
(`find.py:1029-1044`). `RankingConfig` (`find.py:42-102`) is the existing seam for every
sweepable ranking knob: a frozen dataclass, JSON-roundtripped via `ranking_config_to_jsonable` /
`ranking_config_from_jsonable` (`find.py:119-158`), whose loader coerces unknown-typed fields by
inspecting the dataclass **default's** Python type (bool/int/float, `find.py:152-157`) — so a
new `float` field needs no coercion-code changes, only a new dataclass field.

`audit.py` separately already computes real usage from the JSONL access logs
`query_log.py` writes: `logs/queries.jsonl` (one row per `find`, with the surfaced `top_k`
paths), `logs/reads.jsonl` (one row per `get`), and `logs/writes.jsonl` (`cited_sources` per
`note`/`replace`). The shared canonicalizer `_relevance_canon` (`audit.py:866-872`) and JSONL
reader `_relevance_read_jsonl` (`audit.py:875-887`) feed `_stale_access_events`
(`audit.py:1106-1169`), which builds per-path weighted `(delta_days, weight)` access events
(surfaced/read/cited), and `_activation` (`audit.py:1172-1179`), which reduces those events to
the ACT-R base-level activation `B = ln(Σ w_j·Δt_j^(-d))`. This feeds `_check_stale_review`
(`audit.py:1182-...`) — a **sort-only** input to the human stale-review queue. Two docstrings
pin that boundary explicitly: `_stale_activation_params` (`audit.py:1085-1086`, "These weight
the review-queue SORT only — they never touch the stale_review gate or `find` ranking") and
`_check_stale_review` itself (`audit.py:1191-1192`, "it never decays, down-ranks, moves, or
hides anything (`find` ordering is unchanged)"). `query_log.py:38-42` gates all logging behind
`KB_MCP_DISABLE_EMBEDDINGS` (test suite) and `KB_MCP_DISABLE_QUERY_LOG` (ops opt-out), so both
logs and any downstream consumer of them can go empty by design.

`op_find` in `commands.py:246-395` is the single leaf both the MCP tool and the REST/CLI
surfaces are generated from (`commands.py:1-18`, `commands.py:2032-2087` — the declarative
`_SPEC`/`_build_commands` registry); its Python signature and docstring drive
`tests/fixtures/mcp_tool_schemas.json` byte-for-byte. `query_log.log_find_call`
(`query_log.py:58-103`) already threads `prefer_compiled` through into `logs/queries.jsonl` for
exactly this kind of audit trail.

`improve-find-latency-token-cost` adds a small in-process hot cache for repeated identical
`find` requests, keyed by every ranking-affecting parameter plus vault/sidecar freshness.
`reduce-find-per-query-overhead` (a sibling change authored in parallel) is expected to
consolidate `find`'s currently-sequential `_apply_type_boost` / `_apply_status_demotion` /
`_apply_temporal_boost` passes (`find.py:915-922`) into a single combined-multiplier pass. This
change assumes both have landed: it bypasses the hot cache rather than growing the cache key,
and it composes as a further factor in whatever the (by-then single-pass) type/status/temporal
multiplier computation looks like — or, if that consolidation has not landed by implementation
time, as a 4th sequential re-sort pass mirroring `_apply_type_boost`/`_apply_status_demotion`.

engram-memory (audited as a comparison point) ships a related idea — an activation-weighted
retrieval signal — but bakes it into ranking unconditionally, with no kill switch and no
transparency into why a given hit moved. This design borrows only the activation-boost idea,
reimplemented opt-in, bounded, and explainable; see Non-Goals for what is deliberately left
behind.

## Goals / Non-Goals

**Goals:**

- Reuse exomem's existing usage signal (find-surfaced / read / cited) as an opt-in `find`
  ranking boost without adding new logging, a new sidecar, or a new store.
- Keep the boost bounded and positive-only, and keep its ceiling below `compiled_boost`, so it
  can break ties among candidates but never overrides the epistemic hierarchy or the
  supersession demotion.
- Make the boost fully transparent: when it changes a hit's score, `signals` shows the raw
  activation and the multiplier applied.
- Keep default `find` behavior (`prefer_used=False`, the default) byte-identical to today.
- Keep cold-start (no logs yet) and logging-disabled behavior a strict no-op — every multiplier
  exactly `1.0`, never fabricated activation from absence.

**Non-Goals** (each is a decision made while auditing engram-memory's design, not an omission):

- **Explicit feedback tool** (engram's `memory_feedback`) — exomem's implicit chain (a `get`
  shortly after a `find` reads as "selected"; `cited_sources` on a write reads as "strongly
  selected") is already grounded in vault artifacts the agent produces as a side effect of doing
  its job. It needs no separate tool call, no agent cooperation, and no new surface. One
  docstring paragraph on `op_find` documents this implicit loop instead of adding a tool.
- **`PREFERRED_OVER` pairwise edges** — hidden state living outside the vault that can drift
  from it silently. Supersession frontmatter (`status: superseded` / `superseded_by`) is
  already the explicit, inspectable version of "this replaced that."
- **A Boltzmann retrieval gate or score-floor drop** — an empty/absent `find` result must always
  mean "didn't match," never a probabilistic drop. Usage evidence is allowed to reorder matches;
  it is never allowed to decide whether something is returned at all.
- **Decay-as-demotion / forgetting** — usage is never a penalty (see Decision 3): an unused page
  keeps exactly `1.0`, it never sinks below baseline. Staleness already has an owner — the
  stale-review queue (age + low inbound-link degree + low access, human-judged) — and this
  change does not fold that judgment into ranking.
- **A hot-tier / second store for frequently-used pages** — activation is read fresh from the
  JSONL logs already on disk on each snapshot refresh (see Decision 1); no duplicate index,
  cache tier, or store is introduced for "hot" pages.
- **`match_context` prose per hit** — the `signals` dict is already interpretable, and the
  compact-detail direction (`improve-find-latency-token-cost`) strips tokens from responses
  rather than adding them. A possible future `detail="explain"` mode is out of scope here.
- **A `boost_paths` per-call bias parameter** — letting a single call hand-bias its own ranking
  is a different (and more easily gamed) feature than a system-wide, evidence-derived boost.
  Not addressed by this change.
- **Default-on usage ranking** — would break "same vault, same query, same results" for every
  existing caller and would silently reverse `audit.py`'s own documented promise that activation
  never touches `find` ranking. `prefer_used` defaults to `False`; this is deliberate, not a
  placeholder for a future default flip.

## Decisions

### 1. Extract the activation math into `src/kb_mcp/usage.py`; `audit.py` delegates

Move `_relevance_canon`, `_relevance_read_jsonl`, the event-extraction logic in
`_stale_access_events`, and `_activation` out of `audit.py` into a new `src/kb_mcp/usage.py`
module. `audit.py`'s `_check_stale_review` path calls into `usage.py` for the same values it
gets today — behavior for the stale-review audit must be byte-identical, guarded by the existing
`tests/test_audit_stale_review.py` suite plus a new parity test that asserts the extracted
functions and the pre-extraction functions agree on the same JSONL fixtures.

`usage.py` additionally introduces:

- `UsageSnapshot`: a `dict[canon_path, B]` built from the three JSONL logs, memoized on
  `(log file sizes + mtimes, current date)` so a snapshot is invalidated the moment a log grows,
  shrinks, rotates, or the calendar date changes (activation depends on `today` via
  `delta_days`). Refresh TTL defaults to ~300s, overridable via `KB_MCP_USAGE_REFRESH_S`, so a
  hot `find`-heavy session doesn't re-parse three JSONL files on every call.
- `usage_multiplier(canon_path, snapshot, config) -> float`: the formula in Decision 3, `1.0`
  when there is no activation for that path or the boost is gated off.
- `reset_usage_cache()`: test hook that drops the memoized snapshot, mirroring
  `find.reset_active_ranking_cache()` and `find.clear_cache()`.
- Kill-switch `KB_MCP_DISABLE_USAGE_BOOST`, checked once at the top of `usage_multiplier`
  (and by the snapshot builder, so a disabled boost doesn't even parse the logs).

Cold start (no `logs/` directory, or all three files empty) and disabled logging
(`KB_MCP_DISABLE_QUERY_LOG` or `KB_MCP_DISABLE_EMBEDDINGS`, both already read by
`query_log.py:38-42`) are strict no-ops: `usage_multiplier` returns exactly `1.0` for every
path, not an approximation of "low usage." Absence of a signal is "unknown," never a fabricated
zero — mirroring the same convention `_stale_access_counts`/`_stale_access_events` already use
(`audit.py:1054-1077`, `1106-1169`) when they return `None` rather than an empty dict.

### 2. Signal weights for the find boost intentionally diverge from the audit's dormancy weights

The stale-review dormancy sort weights `w_surfaced=1.0, w_read=2.0, w_cited=3.0`
(`_stale_activation_params`, `audit.py:1080-1103`) — being surfaced at all counts there, because
dormancy is about "has anyone even seen this." The find-boost weights are deliberately
different:

- `usage_w_surfaced = 0.0` — being surfaced by `find` is not a choice anyone made; it is the
  output of the very ranking this boost feeds into. Counting it would create a rich-get-richer
  loop (surfaced → boosted → surfaced more often → boosted more), which is exactly the kind of
  hidden feedback state this change's Non-Goals reject in engram-memory's design. Read and cite
  events are actions an agent took *after* seeing a result; surfacing is not.
- `usage_w_read = 1.0` — a `get` shortly after a `find` is a real (if implicit) signal that the
  page was worth opening.
- `usage_w_cited = 2.0` — appearing in a write's `cited_sources` is a stronger signal (the page
  was worth citing as evidence for a new conclusion), so it outweighs a plain read.
- `usage_horizon_days = 90.0` — activation only sums events within the last 90 days. This bounds
  the cost of parsing the JSONL logs (older lines are skipped without full deserialization where
  possible) and makes log rotation *explicitly* safe: a 90-day-old queries.jsonl entry that gets
  rotated away was already outside the horizon and contributing nothing.

### 3. Formula: bounded, positive-only, below `compiled_boost`

Given the per-path weighted access events `(Δt_j, w_j)` within `usage_horizon_days`:

```
B = ln(Σ w_j · Δt_j^(-usage_decay))
```

identical in shape to `_activation` (`audit.py:1172-1179`) but computed over the find-boost
weights (Decision 2) and horizon (Decision 2), not the dormancy weights. `Δt_j` is floored at 1
day exactly as `_stale_access_events` already does, to dodge the `t^(-d)` singularity at `Δt=0`.

The multiplier:

```
mult = 1.0                                    if B is None (no events in horizon) or the
                                               boost is gated off (kill-switch / disabled
                                               logging / cold start)
mult = 1 + (usage_boost - 1) · σ(B)           otherwise, σ(x) = 1 / (1 + e^-x) (logistic)
```

`σ(B)` is strictly in `(0, 1)` for any finite `B` (and `B` is always finite when it exists,
since every weight and every `Δt^(-d)` term is strictly positive), so `mult` is strictly in
`(1.0, usage_boost)` whenever activation exists — it asymptotically approaches `usage_boost` as
activation grows without bound but never reaches or exceeds it. `mult` is **never** below `1.0`:
an unused page is left at exactly `1.0`, matching the "never a penalty" goal — "the vault doesn't
rot because you didn't query it," staleness belongs to the human stale-review queue and explicit
supersession, not to this boost.

Default `usage_boost = 1.10`, deliberately below `compiled_boost = 1.15`
(`find.py:57`/`_COMPILED_BOOST`, `find.py:1187`). Two dominance invariants fall out of that
choice and are pinned by tests:

- **Usage breaks ties, never overrides the epistemic hierarchy**: because
  `usage_boost (1.10) < compiled_boost (1.15)`, a heavily-used non-compiled page can never
  out-multiply a barely-used compiled page purely on usage. Usage is a tie-breaker layered on
  top of the epistemic hierarchy, not a competing signal that can invert it.
- **A superseded tombstone at maximum usage still loses to its active successor**:
  `superseded_penalty (0.5) × usage_boost (1.10) = 0.55`, strictly less than `1.0` — the
  baseline score of an unused, non-superseded page. A tombstone cannot claw its way back above
  its own replacement just because it happens to still get read or cited occasionally.

### 4. Plumbing: opt-in registry param, `RankingConfig` knobs, 4th post-RRF multiplier

Add `prefer_used: bool = False` to `find()` (`find.py:457-...`) and `_find_semantic()`
(`find.py:704-...`), symmetric with the existing `prefer_compiled: bool = True` /
`prefer_active: bool = True` parameters (`find.py:478-479`, `723-724`). Add matching knobs to
`RankingConfig` (`find.py:42-102`), grouped in their own labeled section the way
`temporal_boost`/`temporal_sigma_days` already are (`find.py:64-70`):

```
usage_boost: float = 1.10
usage_decay: float = 0.5
usage_horizon_days: float = 90.0
usage_w_surfaced: float = 0.0
usage_w_read: float = 1.0
usage_w_cited: float = 2.0
```

No changes are needed to `ranking_config_from_jsonable` (`find.py:124-158`) — its per-field
coercion already dispatches on the dataclass default's Python type (`isinstance(f.default, ...)`
at `find.py:152-157`), so these new `float` fields are picked up, validated, and roundtripped
through `ranking_config.json` for free, the same way every other scalar knob is.

Usage composes as a 4th multiplicative post-RRF boost alongside `prefer_compiled`,
`prefer_active`, and the temporal boost (`find.py:915-922`) — see Context for how this composes
with `reduce-find-per-query-overhead`'s expected consolidation of those three passes. It is
**evidence of value, not relevance**: it may reorder candidates the content lanes (BM25, vector,
graph, keyword, CLIP) already surfaced, and it must **never** create a candidate that those
lanes didn't surface — explicitly not a fusion lane, and not weighted into
`fusion.reciprocal_rank_fusion_weighted` (`find.py:909-911`).

When reranking runs, usage is re-applied to `Hit.rerank_score` exactly like `prefer_compiled`
and `prefer_active` already are (`find.py:1029-1044`), appended after their existing re-apply
blocks:

```python
if prefer_used:
    for h in hits:
        if h.rerank_score is not None:
            h.rerank_score *= usage.usage_multiplier(h.path, snapshot, config)
```

so a usage-favored hit the CrossEncoder demoted can still recover before the final sort, the
same rescue `prefer_compiled` already performs for compiled material the reranker under-scores.

Explainability: `Hit` gains no new stored field beyond what's needed to compute `signals` at
`as_dict()` time (mirroring `rerank_score`'s pattern at `find.py:362`, `426-427`) — when
`prefer_used` is active and a hit's multiplier is not `1.0`, `signals` gains:

```
"activation": <raw B, rounded>
"usage_boost": <multiplier applied, rounded>
```

When `prefer_used=False` (the default), none of this runs: zero new keys in `signals`, zero new
JSONL reads, byte-identical output to today.

`op_find` (`commands.py:246-395`) gains the same `prefer_used: bool = False` parameter,
documented in its docstring alongside `prefer_compiled`/`prefer_active` (`commands.py:319-331`)
so the unified command registry (`commands.py:2032-2087`) regenerates the MCP/REST/CLI/OpenAPI
surfaces and `tests/fixtures/mcp_tool_schemas.json` with it automatically — no registry code
changes needed, only the leaf signature and docstring (matching how `prefer_compiled` /
`prefer_active` were themselves added).

`op_find`'s docstring also gains one paragraph documenting the implicit feedback loop (Non-Goals
above): a `get` shortly after a `find`, or a citation in a later write, is how exomem already
observes "this result was useful" without a dedicated feedback tool.

### 5. Interaction with the hot find cache: bypass when `prefer_used=True`

`improve-find-latency-token-cost`'s hot cache keys on every ranking-affecting parameter plus
vault/sidecar freshness. Usage-activation depends on a *third* freshness axis — the JSONL access
logs — that changes on every `find`/`get`/write call, independent of vault or sidecar state.
Folding log freshness into the cache key would mean the cache key changes on almost every call
when `prefer_used=True` (a `find` call itself appends to `logs/queries.jsonl`), defeating the
cache's purpose while adding real complexity (three additional mtime/size stats per lookup).

The simpler, correct behavior: when `prefer_used=True`, `find()` bypasses the hot cache
entirely — always computes fresh, never reads from or writes into it. `prefer_used=False`
(the default) is completely unaffected: cache behavior for the common path is unchanged by this
decision.

### 6. `audit.py` docstring amendment

The two docstrings that currently assert activation "never touches `find` ranking"
(`_stale_activation_params`, `audit.py:1085-1086`) and "`find` ordering is unchanged"
(`_check_stale_review`, `audit.py:1191-1192`) become, in substance:

> ...never touches `find` ranking **by default**; the opt-in `prefer_used` `find` parameter is
> the sole, explicit exception.

so the documented promise stays accurate rather than becoming stale the moment this change
ships, while still being clear that the exception is opt-in and explicit, not a silent default
change.

## Risks / Trade-offs

- **Rich-get-richer loop via surfacing** → mitigated by `usage_w_surfaced = 0.0` (Decision 2);
  only read/cite events, which require an agent action *after* seeing a result, feed activation.
- **Usage boost silently overrides the epistemic hierarchy or supersession** →
  `usage_boost (1.10) < compiled_boost (1.15)` and `superseded_penalty × usage_boost < 1.0` are
  both pinned by dominance-invariant tests (Decision 3), not just documentation.
- **Usage boost silently changes default results for existing callers** → `prefer_used` defaults
  to `False`; a default-off integration test asserts byte-identical output to a pre-change
  baseline.
- **Stale/expensive log parsing on a hot `find`-heavy session** → the `UsageSnapshot` memoizes
  on log file size/mtime plus current date, refreshed at most every `KB_MCP_USAGE_REFRESH_S`
  (~300s default), and `usage_horizon_days` bounds how much of each JSONL file matters.
- **Usage boost surfaces a candidate the content lanes didn't find** → explicitly out of scope
  by construction: `usage_multiplier` is only ever applied to hits already built from the fused
  candidate list (`find.py:939-999`) or already-scored `Hit.rerank_score` values
  (`find.py:1026-1044`); it never contributes to `fusion.reciprocal_rank_fusion_weighted` or adds
  paths. A dedicated test asserts a heavily-used-but-irrelevant page stays absent from results
  for a query it doesn't match.
- **Cache bypass regresses latency for `prefer_used=True` callers** → accepted trade-off; callers
  opting into usage-aware ranking are asking for an explicit, evidence-based reorder, not the
  fast default path. `prefer_used=False` (the default, and the common case) keeps full cache
  benefit.

## Migration Plan

No data migration. `usage.py` is additive; `audit.py`'s public behavior for the stale-review
check does not change (guarded by the existing suite plus the new parity test). `RankingConfig`
gains fields with defaults that reproduce today's behavior when `prefer_used=False`, so an
existing `ranking_config.json` that doesn't mention the new knobs continues to load unchanged
(`ranking_config_from_jsonable`'s missing-key-falls-back-to-default path,
`find.py:124-158`). Existing MCP/REST/CLI callers that omit `prefer_used` get today's `find`
response, byte-for-byte.

## Open Questions

None for implementation. A future `detail="explain"` mode (per-hit prose explanation) is
explicitly deferred (Non-Goals); it would consume, not change, the `activation`/`usage_boost`
signals this change adds.
