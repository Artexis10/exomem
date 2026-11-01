## ADDED Requirements

### Requirement: Full Lexical Repair Yields To Foreground Writes

A detached full lexical repair SHALL pause at its yields while foreground work
runs, including every hold of and wait for a mutation boundary. It SHALL pause
for most of its elapsed time beyond one grace wait, and SHALL pause for no more
than a fixed total in one pass, so that a pass that meets no gap finishes
within its work plus that total. It SHALL continue its work without waiting
for a boundary to be released. `lexical_repair_finished` SHALL report the
seconds that each full pass paused as `parked_s`.

#### Scenario: A full repair beside an import's apply parks and publishes

- **WHEN** an import's apply holds the mutation boundary for the whole of a
  detached full repair
- **THEN** the repair pauses at its yields for longer than its own work, within
  its progress floor
- **AND** publishes while the apply still holds the boundary
- **AND** reports the seconds it paused as `parked_s` in
  `lexical_repair_finished`

#### Scenario: A full repair that never meets a gap stops pausing at its ceiling

- **WHEN** foreground work runs at every yield of a full repair
- **THEN** the repair's pauses total no more than its fixed ceiling
- **AND** the repair runs the rest of its work without a pause
