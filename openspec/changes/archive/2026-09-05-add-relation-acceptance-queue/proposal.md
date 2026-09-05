# Proposal: add-relation-acceptance-queue

## Why

Graph quality is "constrained more by missing authored relationships than by
missing relation vocabulary" (`docs/comparison-basic-memory-graph.md`), and
the activation review (#198) measures that debt but offers no efficient way to
pay it down: accepting a suggested relation today is a one-at-a-time Studio
modal (`prepareRelationProposal` → `submitRelation`), and there is no
persistent record of rejected suggestions, so the same candidates resurface
forever. A batched, fingerprint-guarded accept/reject queue turns relation
authoring from artisanal to routine — while preserving the propose-only
guarantee (deterministic candidates, human decision, governed write path).

## What Changes

- New read-only queue: `review_memory(mode="relation-queue")` returns
  deterministic `suggest_relations` candidates batched across the
  activation-eligible corpus, each with stable identity
  (`exomem://review/relation/<id>`) and a signal fingerprint; candidates
  already authored as edges, targeting placeholders, or previously rejected
  (unexpired fingerprint) are filtered out.
- Reject: `triage_memory(action="dismiss"|"snooze")` on relation-queue refs —
  reusing the existing fingerprint-bound review-state store, so a rejected
  candidate resurfaces only when its underlying signal materially changes.
- Accept: new `connect_memory(operation="accept-relation")` — validates the
  candidate's fingerprint and the target page's `expected_hash`, then performs
  the SAME canonical write the Studio does today (`edit_memory`
  heading-append of `- relation_type [[Target]]` under `## Relations`).
  One governed server-side step; no client-orchestrated two-phase write.
- Studio: a batched relation-queue panel (grouped by page, accept/reject per
  candidate, existing `guardedWrite` fingerprint handling), replacing nothing
  — the one-at-a-time proposal modal remains.
- Queue read emits coverage counters aligned with activation denominators so
  progress against relation debt is measurable.

## Capabilities

### New Capabilities
- `relation-acceptance-queue`: batched, fingerprint-guarded accept/reject
  review over deterministic relation suggestions, with a governed
  server-side accept write.

### Modified Capabilities

(none — the queue is namespaced apart from the daily attention queue,
following the activation-isolation precedent from #198; `command-surface`
requirements are registry-level and gain the new operations automatically.)

## Impact

- Code: new `src/exomem/relation_queue.py` (queue assembly; reads via the
  existing `suggest_relations` API — `src/exomem/epistemic_graph.py` is NOT
  edited, to stay disjoint from the parallel find-lane change);
  `src/exomem/commands.py` (review_memory mode, connect_memory operation,
  triage routing); `src/exomem/review_state.py` reuse (no schema change
  expected); `src/exomem/studio/app.v1.js` (queue panel).
- Tests: queue determinism/filtering, fingerprint expiry semantics, accept
  write parity with the Studio path, drift-guard rejection, non-mutation of
  the read path, Studio governed-flow test extension.
- Explicitly NOT in scope: model-generated candidates, auto-accept, bulk
  accept-all, editing `epistemic_graph.py`, ranking changes.
