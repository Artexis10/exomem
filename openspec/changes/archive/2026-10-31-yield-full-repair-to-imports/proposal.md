## Why

A large import's apply holds the mutation boundary for 97% of each call, with
gaps of 0.2 to 0.4 s between calls. A detached full lexical repair beside it
pauses at its yields for the apply, but the default progress floor lets it
pause for only half of its time. For the rest, the repair and the apply share
one process's CPU. On 2026-10-31, in one cloud rehearsal, apply calls with a
repair running wrote 46 files against 113 without one.

In a local reproduction of that load, a full repair at the default floor
worked through 51% of each apply call that it overlapped, and the apply lost 35%
of its files per second while the repair ran. At a share of 0.9, the apply's calls kept their quiet rate, and
the apply wrote 32% to 37% more files in the same 360 s.

## What Changes

- A full lexical repair's rebuild runs on its own progress floor. It may pause
  for nine tenths of its elapsed time beyond one grace wait, not half.
- Its pauses total at most 300 s in one pass. A pass that meets no gap then
  runs the rest of its work without a pause, so it finishes within its work
  plus 300 s. The default floor allowed twice its work.
- `lexical_repair_finished` carries `parked_s` for full passes: the seconds
  that the rebuild paused at its yields for foreground work.
- `foreground_priority.PassBudget` takes an optional share and an optional
  total for one pass. Every other bulk pass keeps the default floor.

## Capabilities

### Modified Capabilities

- `live-index-freshness`: a detached full lexical repair gives most of its time
  to foreground writes, within a fixed total, and reports its pauses.

## Impact

- `src/exomem/foreground_priority.py`: the share and total for one pass, and a
  read of the current pass's waited seconds.
- `src/exomem/lexstore.py`: the repair's floor and the `parked_s` field.
- `tests/test_foreground_priority.py`: a full repair beside a held boundary,
  and a pass that reaches its total.
- A full pass lasts longer under an import. In the reproduction, 3 of 11 such
  passes declined at a replay, against 0 of 17 at the default floor. Each
  decline schedules another pass.
