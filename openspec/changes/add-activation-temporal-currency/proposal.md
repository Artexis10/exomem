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

- A served unit carries its own authored time; the page's time is labelled apart.
- Superseded or historical units are marked history and rank below current ones;
  explicit supersession units are served in full.
- `current_state` for an anchor resolved only by weak lexical evidence is held back when
  old or undated, and labelled with its age otherwise.
- A recommendation whose action a later outcome records is marked superseded by it, and
  the outcome is marked newer.

## Impact

Packet unit entries gain optional `history`, `newer_than` and `superseded_by_outcome`
fields; `current_state` entries gain optional `resolved_by` and `age_days`. Unit
`updated` is empty rather than the page's time when the unit authors no time.
