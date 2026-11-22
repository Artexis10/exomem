## ADDED Requirements

### Requirement: Verified Source windows remain readable before an incomplete part

When a declared extraction part is missing, unreadable or unbound, the Source read SHALL serve a requested window within the contiguous verified prefix. The window SHALL set `incomplete` to `true` and `total_chars` to `null`. At the end of that prefix, `next_offset` SHALL name the first unmeasured position. A request at or beyond that position SHALL report `PART_MISSING` while the gap remains. The read SHALL use only text bound to the Source.

#### Scenario: A later gap follows a verified search passage

- **WHEN** search returns a passage from a verified part before a missing or unbound continuation
- **THEN** the reported offset opens the quoted passage
- **AND** the read reports incomplete coverage with an unknown total
- **AND** following the final verified window reaches `PART_MISSING`
