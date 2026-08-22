# Proposal: flag-superseded-plan-motivation

## Why

A Planning item may already declare `motivation`: a bounded list of
`exomem://memory/` references to the knowledge that motivates it. Planning
validates the shape, the renderer round-trips it, and the query grammar filters
on it — and then nothing reads it. The field is stored and consumed by no
product surface at all.

The consequence is the gap the epistemic programme exists to close. When the
vault supersedes a belief, every plan premised on that belief keeps executing
unexamined, because nothing connects the two. A user can ask what they planned,
and can ask what the vault now believes, but cannot ask which of their live
commitments rest on knowledge they have already replaced.

The plan-progress review is the right consumer: it already selects active
committed Planning items, already runs a governed cross-profile hop, and
already presents exact counts while refusing to adjudicate them. This change
gives it a second thing to look at.

Selecting only items that carry `progress_evidence` would make the feature
silent on every real case, because a plan may cite the belief that motivates it
without ever binding a Records view to it. The reviewed slice therefore widens
to items carrying evidence **or** motivation.

## What Changes

- Widen the plan-progress reviewed slice to an active committed item carrying
  `progress_evidence` **or** a non-empty `motivation` list, projecting the
  `motivation` column only where the manifest declares the governed array form.
- Resolve each authored reference to the page it names, and report whether the
  vault has marked that page `status: superseded`.
- Add four divergence counts alongside the existing seven — `motivation_refs`,
  `motivation_resolved`, `motivation_unresolved`, `motivation_superseded` — on
  every reviewed item, as plain non-negative integers.
- Add one batch primitive, `memory_refs.paths_for_ids_read_only`, resolving
  many identities in one sidecar query or one corpus scan, returning every path
  per identity, creating or rebuilding nothing, and never raising the
  page-counting `AMBIGUOUS_REFERENCE` message.
- Collapse every motivation refusal into one indistinguishable outcome. A
  reference the vault does not hold, one it holds twice, a malformed one, one
  whose page a governance ceiling blocks, and one whose page the access tier
  excludes all report `motivation_unavailable` with no path, title, count, or
  successor.
- Bound the work with a second budget, separate from the evidence-execution
  budget, whose verdict is computed from a counter before any target is
  consulted.

## Capabilities

### Modified Capabilities

- `planning`: the reviewed slice widens to motivation-bearing items; the
  authorization-precedes-resolution rule extends to memory references with a
  single collapsed refusal reason; the presented divergence block gains four
  counts and the response gains two motivation counters.

### Added Capabilities

- `planning`: plans premised on superseded knowledge surface for review, with
  supersession read from the target's authored status alone and the successor
  deliberately unnamed.

## Impact

- `src/exomem/plan_progress.py`: selection, projection, resolution, counts, and
  two response counters. `src/exomem/memory_refs.py`: one new read-only batch
  resolver. No new module, dependency, index, migration, or storage.
- No command wiring and no tool-surface movement: `commands.op_review_memory`
  already routes `plan-progress`, and neither
  `tests/fixtures/mcp_tool_schemas.json` nor
  `src/exomem/tool_surface_contract.json` moves.
- Two new test modules: the functional suite and a separate disclosure suite
  written after the implementation, whose strongest assertion is that two
  vaults differing only by whether an unreleased page exists produce equal
  responses.
- Deferred by decision, and filed separately: verdict-based flagging needs a
  verdict history that exists nowhere in the source, and naming a successor
  needs a released second hop. Both are stated as non-goals in `design.md`.
