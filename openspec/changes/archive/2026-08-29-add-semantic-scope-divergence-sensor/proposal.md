# Add the semantic scope-divergence sensor (container health, unit-vector v1)

## Why

The shipped scope-divergence advisory (`structure_promotion.py`) is deliberately
lexical: it compares a page's declared identity vocabulary against the terms of its
durable units, at commit time, from state already in memory. That design is right for
the write path and it caught the France-farm shape — but it is blind exactly where
divergence hides behind shared vocabulary. The 2026-08-29 dogfood false negative is
the type case: a narrow licence-administration note absorbed stopping-physics and
course-critique analysis, every unit sharing the parent domain's words
(driving-licence, lõppaste, Liikluslab), and no advisory fired before the user noticed
the overload — the third structural-promotion miss recorded on the dogfood note, and
the first that no lexical rule can close.

The falsification family agrees. Pre-registered f20 `structural_emergence` requires
the promotion signal to bind to categories, anchors, and disjoint link neighbourhoods,
**never to vocabulary**, and to survive the synonym-swap fixture. A detector that
reads only terms cannot pass that even in principle.

The no-nudge architecture (S4, container/scope health) already settled the shape: the
corpus's semantic-unit vectors exist (`semantic_unit_vectors` in the embedding sidecar, maintained by
`EmbeddingIndex.upsert_semantic_units`, embedded from raw unit text; this change
adds the missing corpus-level read accessor over that table — design D1), so
semantic dispersion is measurable with **no new sidecar, no write-time embedding, and
no model call** — a deterministic geometric read over state the pipeline already
maintains.

## What Changes

- **A semantic evidence path beside the lexical one.** For an eligible page, group its
  units' existing vectors deterministically; a group is divergent when it coheres
  internally, separates from the page's own identity centroid, reaches the existing
  child-note mass gate, and the page still retains its original scope. The v1 gate
  composition (recurrence · mass · retained scope · declared-identity exclusion) is
  preserved; only the evidence source changes.
- **Placement: the audit sweep, not the write path.** The write path stays v1-lexical
  and untouched — commit time reads only in-memory state by v1's own constitution.
  The semantic path runs where vectors already live: a new audit category
  `scope_divergence_semantic` that feeds the existing review/attention machinery as
  fingerprint-bound review items, under S2 suppression, S6 family dispositions and
  origin tags. No new delivery channel.
- **Extractive labels only.** A divergent group is labelled by the top recurring terms
  of its own units — deterministic, no generation. Labels are how the advisory speaks
  and how resolution routes.
- **Resolution stays state-change-only.** A group whose label terms are covered by
  eligible existing destinations (the resolve-by-state-change rule, reused verbatim)
  produces nothing; no dismissal memory, no cooldown, deleting the destinations brings
  the advice back.
- **One new reason code** (`semantic_cluster_diverges`) carried in the v1-compatible
  advisory shape.
- **Agent contract: destination choice is a pre-write responsibility.** The
  FULL bootstrap contract adds the clause the 2026-08-29 case demands (compact
  carriage deferred to the queued compact-bootstrap trim — design D8): when a coherent
  durable thread emerges in conversation, the agent searches for or creates a focused
  destination and links it, rather than letting the current page accumulate structural
  debt until a detector fires.

## Non-goals

No automatic restructuring; no destination proposal beyond the existing routing check;
no new embeddings or models; no NLI; no change to prominence or the delegation
envelope (S7); no bench-constant changes (f20's frozen budgets are calibration-owned).

## Impact

- Affected specs: `action-first-audit` (new category), `agent-bootstrap-contract`
  (teaching clause). `attention-queue` admission and the review-state family
  registry follow derivationally — both assemble from the category registries
  rather than restating names — so no delta is filed for them.
- Affected code: new `structure_promotion_semantic.py` (or a sibling section in
  `structure_promotion.py`), `audit.py` category registration, `review_state.py`
  family registration, bootstrap contract text.
- Falsification: f20 `structural_emergence` (registered, withheld); the 2026-08-29
  case becomes the acceptance fixture; the four frequency/length-matched twins —
  including the legitimately heterogeneous log page, the natural FP hazard for a
  vector detector — must stay quiet.
