# Honor the requested path as the evolution anchor

## Why

Track D's J1 journey exposed a silent product defect while its own
documented contract was also wrong. Evidence (2026-08-09 investigation,
ledger 4b.28):

- `review_memory --mode evolution --path <p>` **accepts and silently
  ignores `path`**: the dispatch (`src/exomem/commands.py:4652`) routes
  evolution mode to the topic-only `op_evolution(query=...)`, so the
  registry-driven CLI coerces and delivers `--path` and then drops it.
- The anchor therefore comes from find ordering of an (empty-query) topic
  search. Under day-granular `updated` stamps all chain members tied and an
  ASCII path tie-break happened to put the oldest page first — which the
  benchmark author then recorded as "`topic_anchor` = oldest path"
  (journeys.py:17), a tie-break artifact misread as a contract. PR #375's
  sub-day knowledge time changed the ordering and the anchor flipped to the
  head; PR #378 was the visible but innocent suspect (its diff is confined
  to `_find_semantic`, which an empty query never enters).
- The ledger's own stated mechanism was wrong on both halves: `get_page`
  has never followed supersession (no such logic exists in
  `get_page.py`), and `evolution_for_path` — which implements exactly the
  path-anchored behaviour, with tests — is dead code: its only callers are
  its own tests.
- The `thinking-evolution` spec is silent on anchors; the archived design
  intent (2026-06-30 design.md:47) reads "`topic_anchor`: the hit rel path
  that surfaced this chain" — an entry-point semantic.

Decision: honor the entry point explicitly. When the caller names a page,
that page is the entry point, so the anchor SHALL be the requested page;
silently ignoring an accepted argument is the defect. The timeline's
content is unaffected either way (the chain walk is bidirectional and the
anchor is a label), which is precisely why the contract must be pinned:
nothing else constrains it.

## What Changes

- `op_review_memory` evolution mode dispatches to the existing
  `evolution.evolution_for_path` when `path` is provided (query remains the
  topic route when no path is given). A missing or unresolvable page is an
  explicit error, never a silent fallback to topic search.
- Anchor semantics documented on the op/tool surface: topic route →
  `topic_anchor` is the hit that surfaced the chain; path route →
  `topic_anchor` is the requested page. `chain_id` remains the active head
  in both.
- Benchmark follow-up (after this lands): correct
  `benchmarks/membench/trackd/journeys.py:17` from "`topic_anchor` =
  oldest path" to "= requested path"; `test_j1_longitudinal_evolution_green`
  then goes green through the product fix, not through recalibration.
  Ledger 4b.28 closes citing this change.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `thinking-evolution`: additive — path-anchored evolution requirement and
  declared anchor semantics.

## Impact

- `src/exomem/commands.py` (dispatch, ~5 lines + docstring),
  `tests/test_evolution.py` / a small commands-level test (red-first: the
  current silent ignore), no schema or storage changes. `evolution_for_path`
  gains its first real caller.
- Track D J1 becomes deterministic against this contract (independent of
  find-order tie-breaks), complementing the 4b.34 clock injection.
