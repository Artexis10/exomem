## Why

When Claude calls `find`, it gets a ranked list of hit **excerpts**. To actually reason
over the matches it must then fire several follow-up `get` calls to read the full notes,
chase wikilinks by hand to find context, and it has **no view of whether the surfaced
notes contradict each other**. That is many round-trips, and Claude still "only sees a
subset" of the relevant graph.

The vault already records everything needed to assemble that context deterministically:
each note's structure (lede, headline sections, heading outline), its wikilink
neighbours (inbound + outbound), recorded supersession edges, and — via the embedding
sidecar — pairwise proximity. Nothing here needs a model; it needs **assembly**.

This is direction 3 of the post-moat roadmap ("reasoning-ready context packs"), the
read-path companion to the just-shipped `attention` review surface.

## What Changes

- **New optional `pack` parameter on `find`** (default `false`). With `pack=false`,
  `find` returns today's byte-identical hit list — no contract change. With `pack=true`,
  `find` returns `{"hits": [...], "pack": {...}}`: the same hits plus one assembled
  **context pack** over the top hits.
- **New module `src/kb_mcp/context_pack.py`** with a pure `assemble_pack(vault_root,
  hits, ...)` that composes four measurement-only parts:
  - **Key claims** — per packed note, extracted **structurally**: the lede (first
    paragraph), the first line/bullets of recognized headline sections
    (Summary/Problem/Conclusion/Decision/Pattern/Hypothesis/Result/Insight/TL;DR), and
    the `##` heading outline. No generation.
  - **1-hop neighbourhood** — the inbound + outbound wikilink neighbours of the packed
    notes (reusing `find._outbound_wikilink_paths` + `vault.find_inbound_wikilinks`),
    excluding notes already packed, ranked by **co-citation** (how many packed notes
    link it), each carrying a one-sentence lede.
  - **Contradictions among the set** — (a) recorded **supersession** edges between
    notes in the set (from `status`/`superseded_by` frontmatter), and (b) **proximity
    "tension"** pairs among the packed notes whose pairwise cosine sits in the existing
    `[CONTRADICTION_FLOOR, DUP_THRESHOLD)` band (reusing
    `corpus_aware._best_cosine_per_file`), framed exactly as the contradiction queue
    does — proximity, not polarity; the reader decides.
  - **Bounds + explicit truncation** — env-overridable caps on packed hits, neighbours,
    and tension pairs; every drop is reported in a `truncation` list — never silent.
- **The polymorphic return lives only in the `op_find` leaf** (`commands.py`); the core
  `find_module.find()` keeps its exact signature and `list[Hit]` return, so the ranker
  and its 100+ tests are untouched. The registry derives the new `pack` param
  automatically from the leaf signature + docstring — no `_SPEC` edit.

It stays **pure-substrate**: the pack is deterministic extraction and graph-walking over
markdown the user already wrote, plus rank/band arithmetic over embeddings the sidecar
already computed. **No server-side LLM, no relevance judging, no
summarization-by-generation.** Assembly mutates nothing and does not touch `find`
ordering. The brain (Claude/Max) does the reasoning over the assembled context.

**Default-off + soft-fail:** `pack` defaults to `false` (existing behaviour). The
`tension` part depends on the embedding sidecar and **soft-fails to empty** when
embeddings are disabled or unimportable (`embeddings_available: false`); the claims,
neighbourhood, and recorded-supersession parts work with no embeddings, so the fast test
suite and torch-less deploys still produce a useful pack.

## Capabilities

### Added Capabilities
- `context-packs`: an optional `find(pack=true)` mode that returns an assembled,
  reasoning-ready context pack — structural key claims, the 1-hop co-citation
  neighbourhood, and recorded-supersession + proximity-tension among the top hits —
  measurement-only, bounded with explicit truncation, reachable on every surface.

## Impact

- Code: new `src/kb_mcp/context_pack.py` (`assemble_pack` + `_extract_claims`,
  `_neighborhood`, `_contradictions` helpers + constants). `op_find` in `commands.py`
  gains a `pack: bool = False` param + the `{"hits","pack"}` branch and imports the new
  module. The MCP schema-fidelity fixture (`tests/fixtures/mcp_tool_schemas.json`) is
  regenerated to include `find`'s new `pack` property.
- Behaviour: purely additive. `pack=false` (default) is byte-identical to today;
  `find()`'s core signature/return and ordering are unchanged. No other tool changes.
- Tests: new `tests/test_context_pack.py` (mostly torch-free unit tests over a synthetic
  cluster — claims, co-citation neighbourhood, supersession edges, truncation,
  embeddings-off degradation; tension-band logic with injected cosines) + an
  `op_find(pack=true)` integration test + the schema-fidelity regen.
- Deploy: the changed `find` schema requires reconnecting the claude.ai connector to pick
  up the `pack` param; REST/CLI need only a restart. No data migration.
