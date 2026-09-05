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

Gating that has not: `add-prominence-levels` and
`add-relation-acceptance-queue` are active changes touching the prominence
contract and the relation-queue surface this envelope composes with. This
change binds to their **merged** state; if either lands differently than its
current delta, the envelope's derivation table and the `link_acceptance`
class wiring are re-reviewed against the landed text before implementation.

## What Changes

- **New capability `delegation-envelope`.** The v1 ceilings table (six classes:
  three ranged, `hygiene_writes` and `restructure_execution` fixed,
  `disclosure` governance-owned); an envelope configuration deriving
  per-action-class dispositions from the prominence level with explicit
  per-class overrides, stored in the shared per-machine config file;
  deterministic, consent-shaped adaptation (plain-language family quieting
  stays the S6 surface; three manual-origin dismissal events in one family arm
  exactly one offer to quiet it, cleared only by an explicit family reset);
  inspection, durability and reset. Standing delegation of restructure
  execution is explicitly out of v1 — it would be an envelope cell above the
  current ceiling, and only a deliberate founder ratification may ever create
  one; its refusal is the sole specified error for that class.
- **`agent-bootstrap-contract` delta.** The agent contract teaches the
  envelope: served classes with provenance, the decider protocol, the
  founder-gate refusal — at most fifty measured lines per carrier, compact
  bootstrap within its existing byte ceiling.
- **`command-surface` delta.** The dispositions view
  (`review_memory(mode="dispositions")`) carries the envelope beside the
  family dispositions as a structurally separate block — one added
  requirement; no tool input schema changes.
- **No new sensor, no new queue, no model.** The envelope composes the S6
  disposition store and the existing confirmation surfaces; it adds authority
  arithmetic, not machinery.

## Impact

- Affected specs: `delegation-envelope` (new), `agent-bootstrap-contract`
  (one added requirement), `command-surface` (one added requirement: the
  dispositions-view envelope block).
- Affected code (implementation slice, after this change is approved):
  `src/exomem/prominence.py` (envelope derivation), a small envelope module
  reusing the shared config file, `commands.op_bootstrap` (contract lines,
  compact budget re-measured), the dispositions-view renderer, scaffold/plugin
  SKILL.md copies, tests. If the recorded dispositions-view response contract
  or packaged digest moves, the documented two-phase response-contract rollout
  applies.
- Review-state schema IS affected: adaptation counts dismissal events "from
  the records themselves", which requires family attribution on dismissal
  records, and `quiet_offered_at` needs a durable slot that exists even while
  the family's disposition is `normal` (today `normal` is represented by the
  absence of a record). Both land as one versioned schema migration under the
  attention-queue migration discipline (previous-schema files migrate on load;
  a newer schema refuses on an older runtime).
- Not affected: hooks
  (presets unchanged), governance plane (cross-boundary disclosure stays
  where it is), tool input schemas (no new commands, no new parameters —
  inspection rides `review_memory(mode="dispositions")` and `bootstrap`), and
  server-side confirmation parameters (v1 adds none; existing gates stand).
