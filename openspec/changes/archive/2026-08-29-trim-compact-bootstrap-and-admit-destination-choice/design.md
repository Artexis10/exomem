# Design — compact trim + destination-choice admission

## D1 — The margin, argued once

Target: after the clause is admitted, compact ≤ 60,888 bytes (61,400 − 512).
The number is not invented here: `HEADROOM_WARNING_BYTES = 512` is already the
codified line below which "the ceiling stops being a budget and becomes a
cliff", added after compact reached 70 bytes of headroom silently. Landing
under it clears the warning and gives the next contract change real budget.
The ceiling itself does not move; neither does the 0.15 saving ratio.

## D2 — Trim method (precedent-bound)

The budget test's running log records two prior trims and their rule: bytes
come back from REDUNDANCY — text repeating what the payload already says
(e.g. `advanced` entries duplicating an action's primary route) — and "no rule
left the payload, and the pins moved WITH the text rather than being loosened
around it." This change binds itself to the same rule:

1. Candidate cuts are passages whose content is stated elsewhere in the same
   payload, connective prose, and adjectives that carry no pin.
2. Never cut: a rule, a landing, a consequence, a named non-outcome, a route,
   a command name reachable nowhere else, or a teaching the canonical spec
   names.
3. Every cut is argued in a new dated entry appended to the budget test's `#:`
   log, and any content test pinned to trimmed wording moves with the text —
   pins are never deleted or loosened to make a cut fit.

Ruling (orchestrator, recorded before review): a cut passage that is shared
prose leaves every profile that carried it. That is within this method — both
precedent trims cut shared text, and rule 1's definition ("stated elsewhere in
the same payload") only ever matches shared text — provided the passage is
restated in substance elsewhere in the same payload, so no profile loses teaching.

## D3 — Clause admission

Compact gains the destination-choice teaching under the same key the full
contract uses (`destination_choice`), condensed wording allowed; the RULE must
survive condensation: destination choice happens at write time (search for an
existing focused destination or create-and-link a child) and post-write
advisories are the safety net, not the mechanism. Full's wording is untouched.

## D4 — Red-first

1. A compact-carriage test (compact payload teaches destination choice) fails
   on the untouched base — captured verbatim before any commands.py edit.
2. The existing compact byte-identical pin (from the S4 delivery) is expected
   to flip: it is replaced by the carriage pin in the same delivery, with the
   replacement named in the test file, not silently deleted.
3. The budget test with the headroom warning promoted to error
   (`-W error::UserWarning`) fails on base (24 bytes headroom) and passes
   after — the measured proof the trim happened, not just the admission.

## D5 — What the reviewer should attack

Whether any cut removed a pinned rule or a spec-named teaching (diff every cut
against the canonical specs); whether pins were loosened instead of moved;
whether the condensed clause still states both halves of the rule; whether the
saving ratio and diagnostics/full profiles are untouched; the arithmetic of
the final sizes (paste `_size` for all three profiles).
