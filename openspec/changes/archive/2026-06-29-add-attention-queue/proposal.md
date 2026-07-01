## Why

exomem ships three **measurement-only epistemic queues** that already work but have
**no single front door**, so they go unused in day-to-day review:

- `stale_review` — active compiled conclusions that are old AND rarely surfaced in
  `find` AND low inbound-degree (ACT-R dormancy ordered).
- `corpus_contradictions` — pairs of active conclusions whose embeddings sit in the
  `[floor, dup)` band, ordered by `cosine + W·dormancy`, capped at
  `KB_MCP_CONTRADICTION_TOP_N`.
- `unprocessed_source` — sources captured but never compiled (`ingested_into` empty),
  aged oldest-first.

Each is reachable today only by running the full `audit` (a lint/health report) and
mentally filtering. There is no "what needs my review today?" surface, so the
differentiated epistemic-hygiene layer doesn't get worked. This change builds that
front door. It is the completion of the unification line already recorded in the KB
("stale and conflict are the same phenomenon → one review queue").

## What Changes

- **New `attention` operation** (new module `src/kb_mcp/attention.py`) that calls
  `audit()` **once** with the three categories and composes their findings into **one
  ranked list** — "what in the Knowledge Base needs your attention today."
- **Cross-queue ranking by Reciprocal Rank Fusion** over each finding's intra-queue
  rank (the queues already emit in rank order), reusing the existing house utility
  `fusion.reciprocal_rank_fusion_weighted` (k=60, equal default weights). A note flagged
  by more than one queue accumulates votes and rises (multi-signal additivity).
- **Dedup by anchor path** into one item carrying a `reasons[]` list; a contradiction
  pair is preserved intact inside its reason (`related_paths=[A,B]`).
- **Capped surfacing** at `limit` (default 25) with an explicit omitted count, and it
  **folds** the contradiction queue's own upstream cap (`KB_MCP_CONTRADICTION_TOP_N`)
  into a reported `upstream_truncated` — never a silent truncation.
- **Registry entry** exposes `attention` on MCP + REST (`/api/attention`) + CLI
  (`kb attention`) from one `_SPEC` line, via the same leaf — no per-surface code.
- A reusable `scripts/dump-tool-schemas.py` regenerates the MCP schema-fidelity fixture
  (adding any command requires regenerating it; there is no regen tool today).

It stays **pure-substrate**: the ranking is a deterministic arithmetic fusion of ranks
the measurement-only checks already computed — the same class as `find`'s weighted RRF
and the contradiction queue's `cosine + W·dormancy` sort. No note content is read,
embedded, or compared at attention time; no cross-item judgment is made. The surface is
strictly read-only — it mutates nothing, never auto-supersedes, and does not touch
`find` ordering. The brain (Claude/Max) still decides what to do with each item.

Out of scope (future): per-`project`/topic scoping (the findings don't carry the note's
project field); per-category weight tuning beyond the equal default (kept a code seam);
any synthesis or judgment across the surfaced items (that is the brain's job).

## Capabilities

### Added Capabilities
- `attention-queue`: a single ranked review surface composing the three epistemic
  queues via deterministic RRF, dedup-by-anchor with multi-signal additivity, capped
  with explicit counts, measurement-only, reachable on every surface.

## Impact

- Code: new `src/kb_mcp/attention.py` (`_rank` pure ranker + `attention()` entry +
  dataclasses). One `op_attention` leaf + one `_SPEC` entry in `commands.py`. New
  `scripts/dump-tool-schemas.py`. The MCP schema-fidelity fixture
  (`tests/fixtures/mcp_tool_schemas.json`) is regenerated to include the new tool.
- Behavior: purely additive — a new read-only tool. No existing tool, the `audit`
  report, or `find` ordering changes. Default-on (it is read-only and cheap: one audit
  pass; `corpus_contradictions` already no-ops when embeddings are disabled).
- Tests: new `tests/test_attention.py` (torch-free unit tests over synthetic findings)
  + registry/surface assertions + an end-to-end determinism test.
- Deploy: a new MCP tool requires reconnecting the claude.ai connector to appear; the
  REST/CLI surfaces need only a restart. No data migration.
