# Add the delegation envelope

## Why

Prominence is today the only dial a user has over how much Exomem does on its
own: one level (`off`/`light`/`balanced`/`maximal`) moving recall, capture and
narration together (`src/exomem/prominence.py`). The no-nudge programme has
since shipped the pieces around it: per-family signal dispositions with reason
codes, origin tags and a first-surfaced ledger (S6, `attention-queue` spec
"Signal families carry a durable disposition"), due-state counters on every
carrier (S1), and a sensor set that emits into the review plane rather than
acting (S4/S5). What is missing is the layer the no-nudge architecture report
(§8, KB `^authority-effects-matrix` note) specifies between prominence and the
dispositions: **per-action-class authority under hard ceilings that prominence
cannot exceed**.

Without it, the product has two failure directions. Upward: a future capability
(curation lane S8, standing sweeps) could inherit `maximal` prominence as
permission to act, which the report's review round showed becomes an envelope
that grants and revokes the same authority in adjacent paragraphs. Downward: a
user who wants less has only prominence (which silences everything, including
recall they still want) or per-family quieting (which is reactive, one family
at a time, and unknown to most users). The envelope resolves both with a small,
deterministic model: ceilings are hard product law; the envelope chooses a
disposition *below* the ceiling per action class; prominence only sets the
defaults.

Gating that has cleared: the authority-and-effects matrix is ratified
(2026-08-30) as the constitution's restated model clause; S1 and S6 are merged.
This slice is S7 of the programme (report §17).

## What Changes

- **New capability `delegation-envelope`.** The v1 ceilings table; an envelope
  configuration deriving per-action-class dispositions from the prominence
  level with explicit per-class overrides; deterministic, consent-shaped
  adaptation (plain-language family quieting stays the S6 surface; three
  dismissals of one family prompt exactly one offer to quiet it); inspection,
  durability and reset. Standing delegation of restructure execution is
  explicitly out of v1 — it would be an envelope cell above the current
  ceiling, and only a deliberate founder ratification may ever create one.
- **`agent-bootstrap-contract` delta.** The agent contract teaches the
  envelope: how the decider reads ceilings and dispositions before acting or
  asking, and how due-state counters are read under it — bounded to a compact
  budget (target ≤ ~50 lines across all carriers, measured in tasks).
- **No new sensor, no new queue, no model.** The envelope composes the S6
  disposition store and the existing confirmation surfaces; it adds authority
  arithmetic, not machinery.

## Impact

- Affected specs: `delegation-envelope` (new), `agent-bootstrap-contract`
  (one added requirement).
- Affected code (implementation slice, after this change is approved):
  `src/exomem/prominence.py` (envelope derivation), a small envelope module
  reusing the shared config file, `commands.op_bootstrap` (contract lines,
  compact budget re-measured), scaffold/plugin SKILL.md copies, tests.
- Not affected: review-state schema (dispositions are reused as-is), hooks
  (presets unchanged), governance plane (cross-boundary disclosure stays
  where it is), tool surface (no new commands — inspection rides
  `review_memory(mode="dispositions")` and `bootstrap`).
