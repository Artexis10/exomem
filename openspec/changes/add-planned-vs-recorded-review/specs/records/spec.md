## MODIFIED Requirements

### Requirement: Neutral observed-state query views
Records query views SHALL display bounded observed values and provenance. Domain-specific interpretation SHALL require an explicit analysis or protocol layer and SHALL NOT be embedded in generic Records machinery. Planned-versus-recorded comparison SHALL live in the read-only plan-progress review rather than in Records machinery: a Records view SHALL remain the same neutral observed-state rendering whether it is read directly or read as bound Planning evidence, and Records SHALL NOT gain a comparison, progress, completion, or success semantic of its own.

#### Scenario: Three-month X3 view remains neutral
- **WHEN** a user asks for X3 progression over three months
- **THEN** the derived view shows chronological session, movement, band, and repetition values with source provenance and no unsupported performance judgment

#### Scenario: Vehicle latest-state view cites events
- **WHEN** a user asks for current mileage or next due maintenance
- **THEN** the result identifies the governing collection/query and the record item from which the latest value was derived

#### Scenario: A view read as plan evidence is the same view
- **WHEN** the same saved view is read directly through the Records command and read again as a Planning item's bound progress evidence
- **THEN** both reads run the same declared view over the same canonical snapshot and produce the same observed numbers
- **AND** the Records view definition, response shape, and neutrality are unchanged by having been used as evidence

## ADDED Requirements

### Requirement: Records serves cross-profile review as an ordinary governed reader

A planned-versus-recorded reviewer SHALL reach Records only through the existing governed read path: resolution of a fully released manifest, authorization of the named saved view, authorization of the canonical source before it is parsed, and the default-deny query envelope. Records SHALL NOT gain a review-specific query surface, filter operator, saved-view feature, bulk export, or relaxed authorization path, and SHALL NOT resolve Planning, compare intent with observation, copy plan state, or mutate either side.

A reviewer SHALL take only bounded provenance and counts from the envelope — the matched count, the returned count, the truncation flag, the view's declared aggregate, and the snapshot identifier — and SHALL NOT receive record rows, bodies, or item identities through the review. A withheld envelope SHALL yield no numbers at all rather than partial ones.

#### Scenario: Review cannot widen Records authorization
- **WHEN** governance withholds a Records collection, its canonical source, or the named saved view
- **THEN** the cross-profile review receives the same refusal an ordinary Records reader receives and obtains no rows, counts, snapshot, or existence signal

#### Scenario: Review reads counts, not records
- **WHEN** a bound saved view matches many records
- **THEN** the review reports the exact matched and returned counts, the truncation flag, and the snapshot identifier
- **AND** no record row, body, or item identity appears in the review response

#### Scenario: Records keeps no review state
- **WHEN** a plan-progress review executes bound Records views repeatedly
- **THEN** no Records manifest, canonical source, audit head, mutation receipt, or query cache is written, and the collection's next ordinary query is unaffected
