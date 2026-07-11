# Design: add-typed-graph-find-lane

## Context

`find()` fuses six lanes via weighted RRF (`LANE_ORDER = (vector, bm25,
keyword, clip, graph, temporal)`; fusion at `find_candidates.py:333-352`).
The graph lane today (`find_candidates.py:259-318`) seeds from the top
`graph_seed_cap` (20) vector/BM25 hits filtered for query relevance, then
expands 1-hop **outbound wikilinks** via `outbound_wikilink_paths` with a
query resolver; new targets join `graph_ranking`, in-degree is tallied.
Meanwhile the typed sidecar (`epistemic_graph.py`; `graph_nodes`/`graph_edges`
with src/dst/path indexes) already contains a superset of that information:
body wikilinks (`links_to`), canonical `## Relations` bullets, frontmatter
provenance edges, semantic-block relations, direction, and lifecycle state —
governed by the relation registry and kept fresh by dual-writes plus the file
watcher. This change re-bases the lane on the sidecar and annotates hits, per
the owner's decision to reverse the earlier "no `find` ordering change"
non-goal.

## Goals / Non-Goals

Goals: typed expansion default-on; byte-identical fallback; additive hit
annotation; hot-cache correctness; latency gate unchanged; zero
`RankingConfig` schema change (autotune-compatible).

Non-Goals: relation authoring (separate change); model-backed suggestions;
new relation vocabulary; reranker changes; changing seed selection or lane
weights; editing historical change artifacts' non-goal wording (superseded,
not rewritten).

## Decisions

### D1 — Reuse the lane slot; no new knobs
The typed expansion replaces the lane's data source, not its position or
weighting. `intent_weights_relationship` (graph ×1.8) and `graph_seed_cap`
apply as-is. `ranking_config.json` files (adopted or candidate) remain valid;
the autotune loop needs no migration. Rejected alternative: a 7th lane —
forces `intent_weights_*` length migration in every adopted config and splits
the graph signal across two lanes the tuner can fight over.

### D2 — Batch read API on the sidecar
New read method on `EpistemicGraphIndex` (working name
`neighbors_for(seeds: list[str]) -> list[Edge]`): two indexed queries
(`src IN (...)` and `dst IN (...)`), joined against `graph_nodes` to exclude
placeholder targets (`epistemic_graph.py:1146-1160` semantics) and expose
relation type + direction. No graph traversal profile is applied here — the
lane is 1-hop by construction; profiles stay a `graph_context` concern.

### D3 — Relation-family precedence, registry-derived
Expansion targets append to `graph_ranking` grouped by family precedence:
provenance+epistemic families (from the governed registry's family metadata)
first, `links_to`/unregistered last; within a family, seed order then edge
insertion order (deterministic). No numeric scoring — RRF consumes rank
positions, so precedence IS the signal. Ties with today's behavior: a pure
wikilink vault produces the same set (all `links_to`), preserving ordering.

### D4 — Fallback = the current code path, kept intact
`EpistemicGraphIndex.available` (schema+registry+extension gate,
`epistemic_graph.py:151-171`) decides per-request: available → typed
expansion; else → existing `outbound_wikilink_paths` block unchanged.
Both paths share seed selection. The legacy path is NOT deleted in this
change (fallback contract requires byte-identical output; deletion would be a
later cleanup once telemetry shows typed mode ubiquitous).

### D5 — Annotation rides the existing envelope
`CandidateBundle` already carries `graph_in_degree_by_path`; add
`graph_provenance_by_path: dict[str, GraphProvenance]` (relation_type,
direction, seed_rel) populated only in typed mode for targets NOT in the
primary set (i.e. genuinely graph-surfaced). `find_results` copies it onto the
hit envelope as an optional `graph` field. claude.ai/MCP callers see it only
when present — additive, contract-safe.

### D6 — Freshness token
`epistemic_graph` gains `cache_token(vault_root) -> tuple` returning
`(schema_version, registry_hash, generation)` where `generation` is a
monotonically-incremented `graph_meta` value bumped in `upsert_after_write` /
`delete_after_remove` / rebuild. `_freshness_key()` (find.py:219-260) appends
`(".graph.sqlite", token-or-absent-sentinel)` when the request's `graph=True`
and mode is hybrid/vector. Mirrors `EmbeddingIndex.cache_token` rationale
(mtime is a lie under WAL).

### D7 — Latency
Two indexed SQL lookups over ≤20 seeds against an 8-9MB sidecar are
sub-millisecond-to-low-ms; this REMOVES the per-seed resolver+parse work of
the wikilink path, so typed mode should be faster than today's lane. Gate
thresholds untouched; `tests/test_graph_lane_perf.py` extended to cover typed
mode.

## Risks / Trade-offs

- **Golden churn**: typed mode changes ordering where typed edges exist.
  Mitigation: goldens run in fallback mode stay untouched; a NEW golden tier
  (fixtures with authored relations + built sidecar) locks typed wins. Golden
  edits are orchestrator-owned (guarded files).
- **Cache correctness**: a missed generation bump serves stale rankings.
  Mitigation: freshness scenarios in the delta spec + equivalence test
  (incremental bump == rebuild token change), reusing the discipline from
  `test_epistemic_graph_freshness.py`.
- **Sidecar contention** (Syncthing/multi-host, #201): lane reads are
  read-only SQLite with the writer lease unaffected; WAL read snapshots make
  torn reads a non-issue. E2E covers reads-during-write.
- **Autotune drift**: the tuner may now shift graph-lane weights on real
  usage. That is the intended feedback loop; the golden floor bounds it.

## Migration Plan

Ship default-on. No config migration (D1). Existing vaults benefit as soon as
their sidecar exists (already built on the reference vault). Rollback = env
kill switch `EXOMEM_DISABLE_GRAPH_INDEX` (falls back byte-identical) — no
release rollback needed for ranking regressions reported in the field.

## Open Questions

None blocking. Post-landing candidates: retire the legacy wikilink path once
typed mode is ubiquitous; relation-family weights as tunable knobs if
evidence shows family precedence needs per-vault tuning.
