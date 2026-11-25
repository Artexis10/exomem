## ADDED Requirements

### Requirement: A Records write can state when its values took effect
`add_record`, `edit_record` and bulk upsert SHALL take an optional `effective_at`: an RFC 3339 instant or a calendar date when the written values became true. Bulk upsert SHALL take it per row and for the batch, and a row's own value SHALL win. A write that states none SHALL take its write time. Each reply, history revision and timeline span SHALL give the effective time with its basis, `stated` or `written`.

#### Scenario: A move recorded late keeps its real date
- **GIVEN** a lamp's `location` has been Shelf A since 1 March
- **WHEN** an edit on 10 March sets `location` to Shelf B with `effective_at` `2026-03-07`
- **THEN** the current location is Shelf B, the reply gives `effective_at` `2026-03-07` with basis `stated`, and the timeline shows Shelf A until 7 March and Shelf B from 7 March

#### Scenario: An unstated effective time says it was defaulted
- **WHEN** an edit sets `location` without `effective_at`
- **THEN** the reply, the history revision and the new span give the write time as the effective time with basis `written`

#### Scenario: A bulk row's own effective time wins over the batch's
- **WHEN** a bulk upsert states a batch `effective_at` and one row states its own
- **THEN** that row's changes take effect at its own time and every other row's at the batch time

### Requirement: An effective time cannot lie in the future
An `effective_at` SHALL NOT name a time after its write. An instant SHALL be at most 5 minutes after the write time. A date SHALL be at most the day after the write's UTC day, so that a writer ahead of UTC can name its own today. Any other value, and a malformed one, SHALL be refused `EFFECTIVE_AT_INVALID` before anything is written. Intended future state belongs to Planning.

#### Scenario: A future date is refused
- **WHEN** an edit written on 10 March states `effective_at` `2026-03-20`
- **THEN** the edit is refused `EFFECTIVE_AT_INVALID` and nothing is written

#### Scenario: A writer ahead of UTC names its own today
- **WHEN** an edit written at 23:30 UTC on 10 March states `effective_at` `2026-03-11`
- **THEN** the edit commits with that effective date

### Requirement: Every field of a Records row keeps a timeline
Each declared field of a store-backed Records row SHALL have a timeline of points. A point says that the field held a value from an effective time. A row's first version SHALL give every declared field a point. A later write SHALL add a point to a field only where the written value differs from the field's value at the write's effective time, or where it corrects a point. Each field's latest point SHALL hold the row's current value. No collection setting or field name SHALL change this.

#### Scenario: Fields keep separate timelines
- **WHEN** one edit changes a lamp's `location` and a later edit changes only its `status`
- **THEN** the `location` timeline has a point from the first edit only, and the `status` timeline has a point from the second edit only

#### Scenario: Restating a value records no point
- **WHEN** an edit sets `location` to the value it already holds at the edit's effective time
- **THEN** the `location` timeline gains no point

#### Scenario: The capability needs no setup
- **WHEN** a collection was created before this change with no new manifest setting, and its rows were written without effective times
- **THEN** every field's timeline reads from the stored versions, each point with basis `written`

### Requirement: An edit can record a past value without changing current state
When `edit_record` names a field with an effective time earlier than that field's latest point, the edit SHALL record a past point for the field and SHALL keep the field's current value. Two effective times SHALL be compared at the coarser of their precisions, and a tie SHALL count as later. The reply SHALL name each field's placement, `current` or `past`. `add_record` and bulk upsert SHALL refuse a past placement `EFFECTIVE_BEFORE_CURRENT`, naming each such field.

#### Scenario: A missed move is filled in
- **GIVEN** a lamp moved from Shelf A to Shelf B on 7 March, and Shelf B is current
- **WHEN** an edit sets `location` to Repair Bench with `effective_at` `2026-03-03`
- **THEN** the current location stays Shelf B, the reply names `location` as `past`, and the timeline shows Shelf A, then Repair Bench from 3 March, then Shelf B from 7 March

#### Scenario: One edit places two fields differently
- **WHEN** an edit with `effective_at` `2026-03-05` changes `location`, last changed on 1 March, and `status`, last changed on 8 March
- **THEN** `location` is `current` and takes the new value, `status` is `past` and keeps its current value, and the reply names both placements

#### Scenario: A date on the same day as a timed change is current
- **WHEN** a field's latest point is `2026-03-07T09:00:00Z` and a later edit states `effective_at` `2026-03-07`
- **THEN** the edit's value becomes current

#### Scenario: Bulk upsert refuses a past placement
- **WHEN** a bulk row changes `location` with an effective time before that field's latest point and `on_reject` is `abort`
- **THEN** the row is rejected `EFFECTIVE_BEFORE_CURRENT` naming `location` and its latest effective time, and nothing is written

### Requirement: A correction replaces a point and keeps what was believed
`edit_record` SHALL take an optional `corrects`: the transition id of the write that recorded a point. The new point SHALL replace that point for the fields the edit names, or for every field it set when the edit names none, and SHALL keep its effective time unless the edit states one. The replaced point SHALL leave the timeline and stay in history. Only `corrects` SHALL remove a point.

#### Scenario: A wrong place is corrected
- **GIVEN** the latest `location` point says Shelf B from 7 March
- **WHEN** an edit with `corrects` naming that point's transition sets `location` to Shelf C
- **THEN** the current location is Shelf C, the timeline shows Shelf C from 7 March and no change from Shelf B to Shelf C, and history shows Shelf B with its write time and the correction with its own

#### Scenario: A wrong date is corrected
- **WHEN** an edit with `corrects` naming the move to Shelf B states `effective_at` `2026-03-09` and names no values
- **THEN** the timeline shows Shelf A until 9 March and Shelf B from 9 March, and history keeps the 7 March point as replaced

#### Scenario: A past value is corrected
- **GIVEN** the past `location` point says Repair Bench from 3 March, and Shelf B is current
- **WHEN** an edit with `corrects` naming that point sets `location` to Paint Room
- **THEN** the timeline shows Paint Room from 3 March, and the current location stays Shelf B

### Requirement: A correction cannot reorder a field's latest point
A correction that would change which point is a field's latest, other than by replacing that point, SHALL be refused `TIMELINE_ORDER_CONFLICT`, naming the field and the other point. A `corrects` that names no point of the edited fields of that row SHALL be refused `TIMELINE_POINT_NOT_FOUND`. Neither refusal SHALL write anything.

#### Scenario: A correction cannot reorder the latest point
- **WHEN** a correction would move the 7 March point before the 1 March point of the same field
- **THEN** the edit is refused `TIMELINE_ORDER_CONFLICT` naming the field and the other point, and nothing is written

#### Scenario: A correction names a point that does not exist
- **WHEN** `corrects` names a transition that recorded no point for the edited fields of that row
- **THEN** the edit is refused `TIMELINE_POINT_NOT_FOUND` and nothing is written

### Requirement: Row history is readable by natural key
`records.history` SHALL select a row by exactly one of `item_key`, `item_id` (the row id a query returns) or `natural_key` (an object of the collection's natural-key field values). `natural_key` SHALL find the current row that holds that key, or else the retracted row whose identity derives from it. A caller who cannot read the row or any natural-key field SHALL receive `RECORD_NOT_FOUND`, identical to the refusal for a key that names no row.

#### Scenario: A person's key finds the history
- **WHEN** a caller asks for the history of `natural_key` `{"asset_tag": "L-17"}`
- **THEN** the reply is the history of the row whose asset tag is L-17

#### Scenario: A withheld key part reads as absent
- **WHEN** a caller who cannot read one natural-key field asks for history by `natural_key`
- **THEN** the refusal is identical to the refusal for a natural key that names no row

#### Scenario: A retracted row is found by its key
- **WHEN** an owner asks for the history of a retracted row by its natural key
- **THEN** the reply shows its versions and its retraction

### Requirement: Row history reports effective time and corrections
On a store-backed collection, each revision that `records.history` returns SHALL report its effective time and basis, each changed field's placement, the transition it corrects, and each later transition that replaced one of its points with that transition's write time. A write that changed only past points SHALL appear as a revision with its `why`.

#### Scenario: History shows what was believed when
- **WHEN** a move to Shelf B on 7 March is later corrected to Shelf C
- **THEN** the revision for Shelf B names the correction's transition and write time as its replacement, and the correction's revision names the transition it corrects

### Requirement: query_records reads one field's timeline
`query_records` SHALL take an optional `timeline`: `field`, a declared scalar field path; an optional `value` predicate with the filter operators; optionally `at`, an instant or a date, or a window of `from`, `to` or both; and an optional `changes` flag. `filters` SHALL select rows by their current values, as without `timeline`. The reply SHALL list the field's spans, ordered by effective start and then row, newest first when `descending` is true, in pages bound to the collection snapshot.

#### Scenario: One row's values over time
- **WHEN** a caller queries with a filter on `asset_tag` equal to L-17 and `timeline` `{"field": "location"}`
- **THEN** the reply lists every `location` span of that row, each with its value, effective start and end, basis and write time

#### Scenario: Rows that held a value on a day
- **WHEN** a caller queries with `timeline` `{"field": "location", "value": {"op": "eq", "value": "Shelf A"}, "at": "2026-03-01"}`
- **THEN** the reply lists each row whose `location` was Shelf A at any moment of that UTC day, and no other row

#### Scenario: Rows that changed the field in a range
- **WHEN** a caller queries with `timeline` `{"field": "location", "from": "2026-03-01", "to": "2026-03-31", "changes": true}`
- **THEN** the reply lists each `location` change that took effect in March, with the previous value

#### Scenario: A retracted row has no spans
- **WHEN** a row is retracted after it held Shelf A on 1 March
- **THEN** a timeline query for Shelf A on that day does not return it, as every other query treats it as absent

#### Scenario: A sort is refused
- **WHEN** a timeline query also names `sort_by` or `aggregate`
- **THEN** the query is refused `TIMELINE_ARGUMENT_INVALID` naming the argument

### Requirement: Timeline spans state their window honestly
A span SHALL carry the row `id`, the value, the previous span's value unless it is the row's first span, `effective_from`, `effective_to` (null while current), the basis and the write time. With `at`, the reply SHALL return the spans that contain the instant, or that overlap the UTC day of a date. With `from` and `to`, it SHALL return the spans that overlap the inclusive window, or with `changes` those that start in it. The reply SHALL echo the window as instants.

#### Scenario: A date names a whole day
- **WHEN** a lamp moved from Shelf A to Shelf B at 14:00 on 1 March and a caller asks for `at` `2026-03-01`
- **THEN** the reply returns both spans and echoes the window from `2026-03-01T00:00:00Z` to `2026-03-02T00:00:00Z`

#### Scenario: Two moves on one date both stay visible
- **WHEN** two edits on 1 March state `effective_at` `2026-03-01`, first Shelf B and then Shelf C
- **THEN** the timeline keeps a zero-length Shelf B span and a Shelf C span, and a change query for March lists both changes

### Requirement: Timeline reads follow field and row governance
A timeline on a field withheld from the caller SHALL be refused `QUERY_FIELD_UNKNOWN`, identical to the refusal for a field the collection does not declare. A withheld row SHALL contribute no span, count or continuation state. A span's transition id, `why` and sources SHALL go only to a caller who reads the whole row. A caller who reads the field but not the whole row SHALL receive the value and the effective and write times.

#### Scenario: A withheld field reads as undeclared
- **WHEN** a caller who cannot read `location` asks for its timeline
- **THEN** the refusal is identical to the refusal for a field named `no_such_field`

#### Scenario: A field reader without the whole row
- **WHEN** a caller who reads `location` but not every field asks for its timeline
- **THEN** each span carries its value and its effective and write times, and no transition id, `why` or source

#### Scenario: A withheld row leaves no trace
- **WHEN** governance withholds one row from the caller
- **THEN** no span of that row is returned, and the totals and continuation are those of the other rows alone

### Requirement: Timeline indexes build on demand and never pass as complete
A selection whose rows hold more versions than the documented direct bound SHALL be answered from a per-field span index. The first such query on a field SHALL start the build and SHALL be refused `TIMELINE_WARMING` with a retry hint and progress, never answered with an empty or partial list. A smaller selection SHALL be answered directly from the versions by the same rule. Writes SHALL keep a built index current. A store copy SHALL rebuild the index rather than trust a copied one.

#### Scenario: The first large query warms
- **WHEN** a caller asks for a `location` timeline on a 100,000-row collection whose `location` index has not been built
- **THEN** the query is refused `TIMELINE_WARMING` with a retry hint and progress, and a later query after the build returns the spans

#### Scenario: The index answers as the versions do
- **WHEN** the same timeline query runs once from the built index and once directly from the versions
- **THEN** both return the same spans, totals and order

#### Scenario: A small collection answers at once
- **WHEN** a caller asks for a timeline on a 200-row collection with no built index
- **THEN** the reply returns the spans without a warming refusal

#### Scenario: A restored copy rebuilds
- **WHEN** a store is restored from a snapshot or replica
- **THEN** its span indexes start empty, and a large timeline query warms rather than reads a copied index

### Requirement: Timeline facts are part of verified history
Stated effective times, past points and corrections SHALL be append-only, written in the same transaction as their write, and covered by that transaction's event hash, so that history verification from the first transaction detects a changed or removed fact. Store schema 21 SHALL add them. A defaulted effective time SHALL NOT be stored, so history written before schema 21 reads with basis `written`.

#### Scenario: A changed fact fails verification
- **WHEN** a stored effective time of an earlier write is changed outside the writer
- **THEN** history verification from the first transaction fails at that write's transaction

#### Scenario: The migration keeps history
- **WHEN** a store at schema 20 migrates to schema 21
- **THEN** every row, version and transaction is unchanged, history verifies from the first transaction, and every existing point reads with basis `written`

### Requirement: Timeline arguments are limited to store-backed collections
`effective_at`, `corrects` and `timeline` SHALL be refused `TIMELINE_UNAVAILABLE` on a file-canonical, dataset or derived collection, after the collection resolves under the caller's authority. Import SHALL take no effective time in this change, so imported rows take their write time. Export to files SHALL name a collection that holds a stated timeline fact as unexportable, because file mode cannot hold the fact.

#### Scenario: A file-canonical collection
- **WHEN** an owner edits a row of a file-canonical collection with `effective_at`
- **THEN** the edit is refused `TIMELINE_UNAVAILABLE` and nothing is written

#### Scenario: Export does not drop a stated fact
- **WHEN** an export preview covers a collection that holds a past point
- **THEN** the preview names that collection as unexportable with the reason

### Requirement: Timeline work meets the Records load target
The timeline bounds SHALL hold at up to 100,000 rows with 5 to 20 versions per row, under the shipped query profiles, once the queried field's index is built:

| Operation | Bound |
|---|---|
| Timeline query with a limit of 100 or less | p95 ≤ 300 ms at 5,000 rows; ≤ 1 s at 100,000 rows |
| Edit by id with `effective_at` or `corrects` | p95 ≤ 300 ms |
| One index build batch's boundary hold | p99 ≤ 0.5 s |

The existing Records bounds SHALL hold with one field's index built.

#### Scenario: The timeline bounds are measured
- **WHEN** this change is delivered
- **THEN** the Records benchmark runs the timeline cases on generated fixtures of 5,000 and 100,000 rows, and its recorded results show each timeline bound and each existing Records bound before and after the change

### Requirement: Describe teaches effective time, corrections and timelines
`record_memory(action="describe")` SHALL document `effective_at` and its basis, placements, `corrects`, the `timeline` argument and its reply, and their refusal codes. Its example SHALL show a lamp that moves from Shelf A to Shelf B, is recorded late and is corrected, with invented field names and no personal or product identity.

#### Scenario: A generic client follows the timeline from describe alone
- **WHEN** a client reads only the `describe` reply and then records a late move, corrects it, and queries the field's timeline
- **THEN** each call succeeds without a guessed argument, code or field name
