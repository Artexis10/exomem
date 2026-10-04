## Why

Activation can serve stale or historical context as if it were current. Three shapes
were observed on private material and are reproduced here only with invented data:

- A page holds a current decision (chose transport B) and older candidate observations
  (considered transport A). The old observations were served under `recent_change`,
  stamped with the PARENT page's latest update time, so history read as recent. The
  explicit supersession unit was only a `role_cap` pointer.
- An unrelated project resolved on shared words and contributed a months-old
  `current_state` as though it were current.
- A page holds an older recommendation (call the admissions office) and a newer recorded
  outcome (called; accepted). Both were served with no temporal order and the agent
  repeated the completed action.

## What Changes

Currency is taken only from what the author wrote; nothing is inferred from dates,
categories or shared words.

- A served unit carries the date written in its context slot, never a date its prose
  mentions or a deadline; its page's time orders undated units and is not published.
- A unit is history only when its page is superseded or archived, or a unit on the
  same page carries a `supersedes` relation naming it. History ranks after current
  material within a role.
- An outcome retires a recommendation only through that same authored relation.
- `current_state` for an anchor resolved only by weak lexical or recall evidence is
  held back when older than 30 days, age-labelled when recent, and served labelled
  `resolved_by: lexical` when undated.
- An entity or hub serves the current-state page its own page declares in
  `current_state_page`, with the page's path so the egress guard decides it.
- Relation targets and page times stay internal; no unit class is exempt from the
  role cap; the hook labels history units.

## Impact

Packet unit entries gain an optional `history: true`; `current_state` entries gain
optional `path`, `resolved_by` and `age_days`; an undated state labels its page time
in its statement without publishing internal metadata. Unit `updated` is empty,
rather than the page's time, when the unit authors no time. Standing units now count
against the role cap. The prompt hook renders `history` and `carried history` labels.
