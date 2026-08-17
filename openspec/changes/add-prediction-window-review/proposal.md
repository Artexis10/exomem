## Why

The epistemic loop primitives gave an author three things: a `prediction` unit kind, a `check_by` date meaning "check this claim by then", and a `verdict` meaning "here is how it turned out". Two of the three work end to end. The third does not close.

`check_by` exists to answer one question — *what is due?* — and nothing asks it. A prediction written in March with `check_by: 2026-08-01` sits in the vault after that date looking exactly like one written yesterday. The date is authored, typed, and filterable, and it still produces no review work when its window closes. That is the loop's whole point failing at the last step: an unchecked prediction is not knowledge, it is an outstanding obligation, and an obligation nothing surfaces is one nobody meets.

The retrieval half already landed. `unit.check_by` is a typed date field in the structured-filter registry, so an ordered due-by query is answerable today. What is missing is the *push*: a review queue that raises a due, unresolved prediction without the author having to remember to ask.

## What Changes

- Add a `prediction_window` audit category: a rich semantic unit whose authored `check_by` date has arrived or passed and which carries no evidence of having been checked. Severity is `info` — a review candidate, never a blocking finding — and the queue is ordered most-overdue-first.
- Define "not yet checked" **unit-locally**: no `verdict` metadata on the unit, and no outbound relation on the unit itself in the resolving family `{supports, contradicts, resolves, evidenced_by}`. A relation authored anywhere else on the page does not count, and neither does an inbound edge from another note.
- Use the **unit fingerprint** as the finding's signal version and as its review partition, so each due prediction is its own review item and editing a prediction resurfaces it honestly instead of silently reusing a dismissal made against different text.
- Add `prediction_window` to the **default** `attention` union, in second position — directly after `bridge_review` and ahead of `corpus_contradictions` — because the default order runs from explicitly authored commitments to inferred signals, and an authored check date is a commitment.

The predicate is deliberately unit-local, and that is a scope decision rather than a shortcut — the reasoning, and the specific graph behaviour that forces it today, are in `design.md`.

**Why this one is a default and its experiment sibling is not.** `close-experiment-lifecycle` lands the same shape of lifecycle check and deliberately stays opt-in. The distinction is backlog profile, not category kind. That check reads `started` and `duration`, which predate the package rename, so an established vault can already hold dozens of long-closed windows and admitting them all at upgrade time would displace the signal already on the daily surface. `check_by` and the `prediction` kind shipped one day before this change, so no vault can hold a grandfathered population of them. Where there is nothing grandfathered to protect against, opt-in buys nothing and costs the entire point: a due prediction that surfaces only once you already thought to ask about predictions has not closed any loop.

**Tool-surface note.** This change adds no MCP tool, no tool argument, and edits no tool docstring. The new category reaches callers through the already-generic `categories` parameter on `review_memory(mode="attention")` and `review_memory(mode="audit")`. The pinned MCP schema fixture and the tool-surface contract are unchanged, so the ChatGPT plugin fingerprint does not move.

## Capabilities

### New Capabilities

- None. This change adds and modifies requirements on an existing capability rather than introducing one.

### Modified Capabilities

- `attention-queue`: gains a `prediction_window` review queue — a deterministic, unit-local, measurement-only check — and admits it to the default review union in second tiebreak position, widening the normative default order from five queues to six.

## Impact

- Affects `src/exomem/audit.py` (category registry, one new check function) and `src/exomem/attention.py` (default union, tiebreak order, and module docstring). No parser, filter, graph, or write-path change.
- The check re-parses semantic units for candidate pages during an audit pass. It is guarded by a cheap textual prefilter so a vault with no authored `check_by` rows pays effectively nothing, and it reuses the same `parse_semantic_units` call shape `relation_debt` already performs.
- Default `audit()` gains one `info` category, and default `attention()` gains one queue. This is the one place the change is deliberately not inert on upgrade — see the backlog argument above for why that is safe here and not for its experiment sibling. Selecting any explicit category set that omits `prediction_window` reproduces prior behaviour exactly.
- Moves the normative default tiebreak order pinned by the `attention-queue` capability, so two of its existing requirements are modified rather than merely extended. `close-experiment-lifecycle` deliberately touches neither, so only this change carries a delta against them and the two are independently reviewable.
- A finding anchors on its parent page's path but partitions by unit fingerprint, so two due predictions on one page are two independent review items with independent snooze and dismissal state. This reuses the existing `meta.review_partition` mechanism that `bridge_review` established; it introduces no new review-state concept.
- Introduces no model, no new relation kind, no new unit kind, no new page type, no new sidecar, and no ranking change. `find` ordering is untouched.
- Because the predicate is unit-local, a prediction resolved only by an inbound edge from another note is still surfaced. That is a known, bounded false positive, accepted on the reasoning in `design.md` and retired by the separately-filed fragment-target refinement.
