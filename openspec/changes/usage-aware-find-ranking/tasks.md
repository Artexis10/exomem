## 1. Tests First

- [ ] 1.1 Add a parity test (`audit-vs-usage.py` extraction) proving `usage._relevance_canon`,
      `usage._relevance_read_jsonl`, `usage`'s event extraction, and `usage.activation()` agree
      with the pre-extraction `audit.py` functions on the same JSONL fixtures.
- [ ] 1.2 Add formula unit tests for `usage_multiplier`: bounds (`mult` strictly in
      `(1.0, usage_boost)` when activation exists, exactly `1.0` when it doesn't), monotonicity
      (more/recent weighted events never lower `mult`), `None` activation → `1.0`, events outside
      `usage_horizon_days` are excluded, and malformed JSONL lines are skipped without raising.
- [ ] 1.3 Add a keyword/hybrid-path integration test using JSONL fixtures written directly to a
      tmp `logs/` dir (default `prefer_used=False` reproduces the current byte-identical
      baseline; `prefer_used=True` lets a heavily-used page win a near-tie it would otherwise
      lose or tie on content signals alone).
- [ ] 1.4 Add a never-creates-candidates test: a page with maximal usage activation but no
      lexical/semantic/graph match to the query stays absent from `prefer_used=True` results.
- [ ] 1.5 Add dominance-invariant tests: a superseded page at maximum usage activation
      (`superseded_penalty × usage_boost`) still ranks below its active successor at baseline;
      `compiled_boost (1.15) > usage_boost (1.10)` so usage cannot out-multiply the epistemic
      hierarchy on its own.
- [ ] 1.6 Add cold-start no-op tests: no `logs/` directory, or all three JSONL files empty/absent
      → every `usage_multiplier` call returns exactly `1.0`.
- [ ] 1.7 Add disabled-signal no-op tests: `KB_MCP_DISABLE_USAGE_BOOST`,
      `KB_MCP_DISABLE_QUERY_LOG`, and `KB_MCP_DISABLE_EMBEDDINGS` (individually) each force
      `mult == 1.0` for every hit, even with populated JSONL fixtures.
- [ ] 1.8 Add snapshot invalidation tests: a `UsageSnapshot` reflects new events after a log file
      grows (size/mtime change) and after `usage.reset_usage_cache()` is called; unchanged logs
      within the refresh TTL keep serving the memoized snapshot.
- [ ] 1.9 Add a rerank-path test: with `rerank=True` and `prefer_used=True`, a hit's
      `rerank_score` is multiplied by its `usage_multiplier` after the CrossEncoder pass, the
      same way `prefer_compiled`/`prefer_active` are re-applied today.
- [ ] 1.10 Add a `ranking_config.json` roundtrip test for the six new knobs
      (`usage_boost`, `usage_decay`, `usage_horizon_days`, `usage_w_surfaced`, `usage_w_read`,
      `usage_w_cited`), confirming `ranking_config_from_jsonable`'s existing scalar coercion
      handles them without code changes.
- [ ] 1.11 Add a query-log test: `log_find_call` records `prefer_used` in
      `logs/queries.jsonl` the same way it already records `prefer_compiled`.
- [ ] 1.12 Add a hot-cache bypass test: two identical `find` calls with `prefer_used=True` never
      hit or populate the hot cache from `improve-find-latency-token-cost`, while the same calls
      with `prefer_used=False` (the default) are unaffected.

## 2. Extract Usage Activation Module

- [ ] 2.1 Create `src/kb_mcp/usage.py`; move `_relevance_canon`, `_relevance_read_jsonl`, the
      event-extraction logic from `_stale_access_events`, and `_activation` out of `audit.py`
      into it, preserving existing behavior and docstrings.
- [ ] 2.2 Update `audit.py` to import and delegate to `usage.py` for these functions; keep
      `_check_stale_review` and the rest of the stale-review check otherwise unchanged.
- [ ] 2.3 Add `UsageSnapshot` (dict[canon_path, B], memoized on log file sizes+mtimes and current
      date, refresh TTL via `KB_MCP_USAGE_REFRESH_S`, default ~300s).
- [ ] 2.4 Add `usage_multiplier(canon_path, snapshot, config) -> float` and
      `reset_usage_cache()`.
- [ ] 2.5 Add the `KB_MCP_DISABLE_USAGE_BOOST` kill-switch, checked before any log parsing.
- [ ] 2.6 Run `tests/test_audit_stale_review.py` and the new parity test (1.1) to confirm
      byte-identical stale-review behavior post-extraction.

## 3. Usage Boost Formula and RankingConfig Knobs

- [ ] 3.1 Add `usage_boost`, `usage_decay`, `usage_horizon_days`, `usage_w_surfaced`,
      `usage_w_read`, `usage_w_cited` fields to `RankingConfig` (`find.py:42-102`), grouped in a
      labeled section mirroring the existing temporal-lane fields, with the defaults from
      design.md Decision 2/3.
- [ ] 3.2 Implement the `B = ln(Σ w_j·Δt_j^(-usage_decay))` / logistic-boost formula in
      `usage.py`, horizon-limited to `usage_horizon_days`, using find-boost weights (not the
      dormancy weights `_stale_activation_params` uses).
- [ ] 3.3 Confirm `ranking_config_from_jsonable` requires no changes for the new float fields
      (verify via test 1.10); do not add bespoke coercion code.

## 4. Find Plumbing

- [ ] 4.1 Add `prefer_used: bool = False` to `find()` and `_find_semantic()` in `find.py`,
      symmetric with `prefer_compiled`/`prefer_active`, with matching docstring language.
- [ ] 4.2 Apply the usage multiplier as a 4th post-RRF multiplicative boost alongside
      `prefer_compiled`/`prefer_active`/temporal (`find.py:915-922`), composing with (or
      following) whatever single-pass consolidation `reduce-find-per-query-overhead` has landed
      by implementation time; never feed it into
      `fusion.reciprocal_rank_fusion_weighted` and never let it introduce a path absent from the
      fused candidate list.
- [ ] 4.3 Re-apply the usage multiplier to `Hit.rerank_score` after the CrossEncoder pass
      (`find.py:1029-1044`), appended after the existing `prefer_compiled`/`prefer_active`
      re-apply blocks.
- [ ] 4.4 Extend `Hit.as_dict()` to add `activation` (raw `B`, rounded) and `usage_boost`
      (multiplier applied) to `signals` only when `prefer_used` is active and the multiplier is
      not `1.0`.
- [ ] 4.5 Bypass the hot `find` cache entirely when `prefer_used=True` (no cache read, no cache
      write); leave `prefer_used=False` cache behavior untouched.

## 5. Command Surface, Logging, and Docs

- [ ] 5.1 Add `prefer_used: bool = False` to `op_find` (`commands.py:246-395`), threaded into
      `find_module.find(...)`, with docstring language matching `prefer_compiled`/`prefer_active`
      and one new paragraph documenting the implicit feedback loop (a `get` after a `find`, or a
      citation in a later write, is how usage is observed — no dedicated feedback tool).
- [ ] 5.2 Extend `query_log.log_find_call` to accept and record `prefer_used`, matching the
      existing `prefer_compiled` field.
- [ ] 5.3 Regenerate `tests/fixtures/mcp_tool_schemas.json` for the `find` tool's new
      `prefer_used` parameter; confirm the registry (`commands.py:2032-2087`) needs no code
      changes beyond the leaf signature/docstring.
- [ ] 5.4 Amend the two `audit.py` docstrings that assert activation never touches `find` ranking
      (`_stale_activation_params`, `audit.py:1085-1086`; `_check_stale_review`,
      `audit.py:1191-1192`) to state the `prefer_used` exception explicitly.

## 6. Validation

- [ ] 6.1 Run `KB_MCP_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q` and confirm the full
      suite is green, including the disabled-signal no-op tests (1.7) that exercise this same
      env var.
- [ ] 6.2 Run `uv run ruff check`.
- [ ] 6.3 Confirm a default (`prefer_used=False`) `find` call's output is byte-identical to a
      pre-change baseline fixture.
- [ ] 6.4 Confirm no new abstraction beyond `usage.py`/`UsageSnapshot` was introduced, and no
      change was made to `fusion.reciprocal_rank_fusion_weighted` or any content-lane ranker.
