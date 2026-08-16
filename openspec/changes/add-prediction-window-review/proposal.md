## Why

The epistemic loop primitives gave an author three things: a `prediction` unit kind, a `check_by` date meaning "check this claim by then", and a `verdict` meaning "here is how it turned out". Two of the three work end to end. The third does not close.

`check_by` exists to answer one question — *what is due?* — and nothing asks it. A prediction written in March with `check_by: 2026-08-01` sits in the vault after that date looking exactly like one written yesterday. The date is authored, typed, and filterable, and it still produces no review work when its window closes. That is the loop's whole point failing at the last step: an unchecked prediction is not knowledge, it is an outstanding obligation, and an obligation nothing surfaces is one nobody meets.

The retrieval half already landed. `unit.check_by` is a typed date field in the structured-filter registry, so an ordered due-by query is answerable today. What is missing is the *push*: a review queue that raises a due, unresolved prediction without the author having to remember to ask.

## What Changes

- Add a `prediction_window` audit category: a rich semantic unit whose authored `check_by` date has arrived or passed and which carries no evidence of having been checked. Severity is `info` — a review candidate, never a blocking finding — and the queue is ordered most-overdue-first.
- Define "not yet checked" **unit-locally**: no `verdict` metadata on the unit, and no outbound relation on the unit itself in the resolving family `{supports, contradicts, resolves, evidenced_by}`. A relation authored anywhere else on the page does not count, and neither does an inbound edge from another note.
- Use the **unit fingerprint** as the finding's signal version and as its review partition, so each due prediction is its own review item and editing a prediction resurfaces it honestly instead of silently reusing a dismissal made against different text.
- Register `prediction_window` as a selectable `attention` category. It is **not** added to the default attention union, so the default daily review surface is byte-for-byte unchanged and a grandfathered corpus of long-past `check_by` dates cannot flood it on upgrade.

The predicate is deliberately unit-local, and that is a scope decision rather than a shortcut — the reasoning, and the specific graph behaviour that forces it today, are in `design.md`.

**Tool-surface note.** This change adds no MCP tool, no tool argument, and edits no tool docstring. The new category reaches callers through the already-generic `categories` parameter on `review_memory(mode="attention")` and `review_memory(mode="audit")`. The pinned MCP schema fixture and the tool-surface contract are unchanged, so the ChatGPT plugin fingerprint does not move.

## Capabilities

### New Capabilities

- None. This change adds a requirement to an existing capability rather than introducing one.

### Modified Capabilities

- `attention-queue`: gains a `prediction_window` review queue — a deterministic, unit-local, measurement-only check registered as a selectable attention category, with the default union explicitly unchanged.

## Impact

- Affects `src/exomem/audit.py` (category registry, one new check function) and `src/exomem/attention.py` (registered-category tuple). No parser, filter, graph, or write-path change.
- The check re-parses semantic units for candidate pages during an audit pass. It is guarded by a cheap textual prefilter so a vault with no authored `check_by` rows pays effectively nothing, and it reuses the same `parse_semantic_units` call shape `relation_debt` already performs.
- Default `audit()` gains one `info` category. Default `attention()` is unchanged, because the new category is registered but not default-selected. Selecting any explicit category set that omits `prediction_window` reproduces prior behaviour exactly.
- A finding anchors on its parent page's path but partitions by unit fingerprint, so two due predictions on one page are two independent review items with independent snooze and dismissal state. This reuses the existing `meta.review_partition` mechanism that `bridge_review` established; it introduces no new review-state concept.
- Introduces no model, no new relation kind, no new unit kind, no new page type, no new sidecar, and no ranking change. `find` ordering is untouched.
- Because the predicate is unit-local, a prediction resolved only by an inbound edge from another note is still surfaced. That is a known, bounded false positive, accepted on the reasoning in `design.md` and retired by the separately-filed fragment-target refinement.
