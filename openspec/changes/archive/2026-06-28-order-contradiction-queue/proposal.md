## Why

The `audit(categories=["corpus_contradictions"])` sweep is correct but
*unusable as a queue*: on the live vault it surfaces ~877 deduped band pairs
(active compiled conclusions whose embeddings sit in `[floor, dup_threshold)`),
emitted in flat cosine-descending order. The list is dominated by same-family
architecture-note adjacency (many `Notes/Research/<X>/*-architecture` pairs that
are *expected* to be close), so the genuinely worth-reviewing pairs — a close
pair where one note is **dormant** (old, never re-surfaced; the "did I forget I
already concluded the opposite?" case) — drown in the noise. Nobody works an
877-row flat list.

This change makes the queue usable by **ordering** and **capping** the surfaced
review set — without changing what is measured. It stays strictly
**measure-only**: it never auto-supersedes, never mutates a note, and never
touches `find` ranking. It only decides *which review candidates surface first*
and *how many* surface by default.

## What Changes

- **Per-pair review priority + sort.** After the existing sweep collects the
  in-band pairs, compute a review priority per pair from (a) the pair's
  **cosine** (closer → higher) and (b) the **ACT-R dormancy** of the pair's two
  notes (reusing the `stale_review` activation machinery — `_stale_access_events`
  + `_activation` + `_stale_activation_params`). Sort findings by priority
  descending so the most-actionable pairs surface first.
- **Same-family demotion.** When both notes of a pair live in the same
  `Notes/Research/<X>/` subfolder (the architecture-cluster noise), mark the pair
  with a `meta.same_family` flag and sort all such pairs *after* the
  cross-family pairs, regardless of priority.
- **Top-N cap with an explicit count.** Surface only the top
  `KB_MCP_CONTRADICTION_TOP_N` pairs (default 40). When more are in band, append
  ONE explicit summary finding (`"<N> more lower-priority/same-family pairs not
  shown"`) — never a silent truncation. Setting the env to `0` disables the cap
  (include all; no summary finding).

Out of scope: any change to the band edges or eligibility (unchanged), any
mutation/auto-supersede, any effect on `find` ordering, a new sidecar (the sort
reuses signals the KB already records).

## Capabilities

### New Capabilities
- `contradiction-queue`: an ordered, capped, measure-only review queue over the
  existing `corpus_contradictions` sweep — priority by cosine + ACT-R dormancy,
  same-family adjacency demoted, default top-N with an explicit omitted count.

## Impact

- Code: `src/kb_mcp/audit.py` (`_check_corpus_contradictions` gains the priority
  sort, same-family demotion, and cap; reuses the existing ACT-R helpers; new
  `_contradiction_top_n` / `_contradiction_w_dormancy` env readers and a
  `_contradiction_family` helper). `src/kb_mcp/commands.py` audit docstring note.
- Tests: `tests/test_audit_contradiction_order.py` (priority order, same-family
  demotion, cap + count, no mutation), all torch-free via a patched
  `all_vectors`.
- Behavior: zero change to what is *measured* (same eligible set, same band,
  same dedup). No `find`-ranking change, no writes. `KB_MCP_DISABLE_EMBEDDINGS`
  no-op behavior is preserved (the category still short-circuits to `[]`).
- Env knobs: `KB_MCP_CONTRADICTION_TOP_N` (default 40; `0` = uncapped),
  `KB_MCP_CONTRADICTION_W_DORMANCY` (default 0.5). The dormancy reuses the
  existing `KB_MCP_STALE_DECAY` / `_W_SURFACED` / `_W_READ` / `_W_CITED` knobs.
