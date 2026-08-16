## Why

The `experiment` page type is authorable end to end — `domain`, `started`, `duration`, `n`, `hypothesis`, and now (since the epistemic loop primitives) a `concluded` status and a categorical `outcome:` — but nothing ever asks whether an experiment finished. An experiment whose window closed six months ago and whose result was never written down is indistinguishable, to every surface Exomem has, from one that started yesterday. The most expensive thing a vault records is a result; the lifecycle that produces one has no terminal check.

This is not a missing nicety. It is a **documented contract the product already promises and does not keep**, in two places:

- The shipped skill scaffold, `_Schema/references/audit-checks.md`, tells every new user that audit flags **"Unfinished experiments — experiments with `status: active` and `started` date older than the experiment's `duration` field."** No such check exists. A user acting on that documentation gets silence and reads it as "nothing is overdue".
- `audit.py`'s `stale_review` scope comment justifies excluding time-bounded records on the grounds that "time-bounded records (production-log/experiment) **have lifecycle checks**". They do not. The exclusion is correct; its stated reason is false, so the one place a maintainer would look to find the experiment lifecycle check asserts that it is somewhere else.

The result is a silent hole with a documentation-shaped lid on it. Closing it is a small, deterministic check over frontmatter the vault already records.

## What Changes

- Add an `unfinished_experiments` audit category: an experiment whose `started` date is present, whose elapsed time exceeds its declared `duration`, and which records no `outcome:`. Severity is `info` — always a review candidate, never a blocking finding — and the queue is ordered oldest-first by elapsed age.
- Register `unfinished_experiments` as a selectable `attention` category. It is **not** added to the default attention union, so this change leaves the default daily review surface untouched and a grandfathered corpus of long-dormant experiments cannot flood it on upgrade. (The sibling change `add-prediction-window-review` does widen that union, for a queue whose fields are too new to have a backlog; the reasoning for treating the two differently is in `design.md`.)
- Correct the false rationale in `audit.py`'s `stale_review` scope comment so it cites the check that now actually exists, and so it stops implying a `production-log` lifecycle check that still does not exist.
- Correct the shipped scaffold's `audit-checks.md` entry so the documented predicate is the implemented predicate — including that the trigger is a missing `outcome:`, not a `status: active` value, because a `concluded` experiment with no recorded outcome is exactly the case the check exists to catch.

An open-ended experiment (`duration: ongoing`, or any duration that is not a finite span) is never flagged. Absence of a parseable window is not evidence of an overdue one, and fabricating a deadline for an experiment that declares it has none would be a judgment, not a measurement.

**Tool-surface note.** This change adds no MCP tool, no tool argument, and edits no tool docstring. The new category reaches callers through the already-generic `categories` parameter on `review_memory(mode="attention")` and `review_memory(mode="audit")`. The pinned MCP schema fixture and the tool-surface contract are unchanged, so the ChatGPT plugin fingerprint does not move.

## Capabilities

### New Capabilities

- None. This change adds a requirement to an existing capability rather than introducing one.

### Modified Capabilities

- `attention-queue`: gains an `unfinished_experiments` review queue — a deterministic, measurement-only lifecycle check registered as a selectable attention category, with the default union explicitly unchanged.

## Impact

- Affects `src/exomem/audit.py` (category registry, one new check function, one corrected comment), `src/exomem/attention.py` (registered-category tuple), and `src/exomem/_scaffold/_Schema/references/audit-checks.md`.
- `src/exomem/note.py` needs no change: `STATUS_EXPERIMENT` already carries `concluded` and `EXPERIMENT_OUTCOME_VALUES` already aliases `semantic_units.EPISTEMIC_OUTCOMES`. The enum work this change was scoped to do landed with the epistemic loop primitives.
- Default `audit()` gains one `info` category. Default `attention()` is unaffected by this change, because the new category is registered but not default-selected. Selecting any explicit category set that omits `unfinished_experiments` reproduces prior behaviour exactly.
- Touches none of the `attention-queue` capability's existing requirements — the default union and its normative tiebreak order are left alone here, so this change is reviewable independently of the sibling change that does move them.
- Introduces no model, no new relation kind, no new page type, no new sidecar, and no ranking change. `find` ordering is untouched.
- Does **not** close the scaffold's second unbacked claim, "Unfinished production lifecycles". That check also does not exist; this change corrects the comment that wrongly implied it and records the gap as a named follow-up rather than silently widening scope.
