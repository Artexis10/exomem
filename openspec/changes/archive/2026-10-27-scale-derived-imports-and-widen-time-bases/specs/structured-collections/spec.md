## ADDED Requirements

### Requirement: Derived collections keep ordered item keys

A derived collection created by this release SHALL use the `ordered-v1` item key scheme,
unless its natural key holds a location-classified field. An `ordered-v1` key SHALL be an
injective, order-preserving byte encoding of the natural key, stored as bytes and spelled
publicly as lowercase unpadded base32hex, so that text order equals byte order. A
collection that existed before the upgrade, and every collection that is not derived,
SHALL keep the `hashed-v1` scheme. The store SHALL record the scheme per collection at
creation and SHALL refuse any change to it. A natural key with a location-classified
field SHALL use `hashed-v1`, and a classification that marks a natural-key field of an
`ordered-v1` collection as location SHALL be refused. A row query with no `order_by` on
an `ordered-v1` collection SHALL return the lowest natural keys first. Inspection,
`describe` and import preview SHALL report the scheme. An item-key filter whose literal is
not a valid key SHALL match nothing for equality and SHALL be refused with
`QUERY_VALUE_INVALID` for a range comparison.

#### Scenario: A large import writes little per row
- **WHEN** a new derived collection imports hundreds of thousands of rows whose natural keys arrive in ascending order
- **THEN** the bytes written to disk per row stay flat as the collection grows, instead of rising with its size

#### Scenario: An upgraded store keeps its keys
- **WHEN** a store from the previous release opens under this release
- **THEN** every existing collection reports `hashed-v1`, its item keys are unchanged, and no derived rows rebuild

#### Scenario: A location field in the natural key
- **WHEN** a derived collection is created with a natural key that holds a location-classified field
- **THEN** the collection uses `hashed-v1`, so its public keys do not spell the location

#### Scenario: Equal keys from equal values
- **WHEN** two imports of the same member produce rows with the same natural key values
- **THEN** both produce the same item key, and an integer value and the same value written as a float produce different keys

### Requirement: A stopped import reverts only its open member

When an import into a derived collection stops in the middle of a member, Exomem SHALL
restore that collection to its state before the member opened. It SHALL restore the rows
that the member changed, delete the rows that the member added, and restore the rollup
buckets that the member touched, without replaying members that the import log already
holds. The revert SHALL run in bounded steps, SHALL survive a restart, and SHALL report
its progress as `reverting` with the rows that remain. Exomem SHALL fall back to a full
rebuild from the import log when it cannot prove the revert: when the member was opened
by an older release, when the saved rows do not match the count it recorded, when the
collection's contract changed, or when a rollup is not ready.

#### Scenario: A cancel in the middle of a large collection
- **WHEN** a job is cancelled after staging a few thousand rows of a new member into a collection of millions of rows
- **THEN** the revert writes and takes time in proportion to the staged rows, and the collection's digest afterwards equals its digest before the job

#### Scenario: A member that updated earlier rows
- **WHEN** a stopped member had updated rows that earlier members imported
- **THEN** the revert restores those rows to their earlier values

#### Scenario: A restart during the revert
- **WHEN** the service restarts while a revert is in progress
- **THEN** the revert resumes and completes, and row queries report its progress until it ends

## MODIFIED Requirements

### Requirement: Imports read export members and nested JSON documents

An import source MAY be an export manifest with a member selector, a glob or a list of
paths. The job SHALL bind the manifest's SHA-256 and the ordered SHA-256s of the selected
members, SHALL prove each member's hash in a streamed pass before taking its rows, and SHALL checkpoint by member
and row. A `json-document` source SHALL take rows from a declared row path that may cross
nested arrays, with field paths relative to the row, ancestor paths from the document root
or an enclosing element, the row's index, and literals. A row path MAY end at an object,
which yields one row per enclosing element, and the row path `$` SHALL make the whole
document one row. A mapping that reads the row's index SHALL be refused when its row path
crosses no array. The reader SHALL stream, so memory
does not grow with a member's size. A job SHALL skip a member that an earlier job imported
into the same collection with the same mapping unless the caller asks to re-import it. A
field MAY declare a numeric scale.

#### Scenario: Rows from a nested array
- **WHEN** a member holds a list of devices, each with a list of samples, and the mapping declares a row path through both lists
- **THEN** each sample becomes one row that can also carry a field of its device and of the document

#### Scenario: One row per day summary
- **WHEN** a member holds a list of days, each with a summary object, and the row path ends at that object
- **THEN** each day's summary becomes one row

#### Scenario: One row per document
- **WHEN** each member is one document that describes one record and the row path is `$`
- **THEN** each member becomes one row, and a mapping that reads the row's index is refused

#### Scenario: A restart in the middle of a member
- **WHEN** the service restarts while a job is part way through a member
- **THEN** the job resumes in that member and no row is imported twice

#### Scenario: A second export of the same account
- **WHEN** a job runs over a newer export whose older members have the same SHA-256 as members already imported with the same mapping
- **THEN** the job reads only the members it has not imported

### Requirement: Import time bases declare zones, offsets and positions

A time basis MAY combine a date or an instant with a declared IANA zone, an offset field
in minutes or ISO form, and an increment from a field of seconds, of milliseconds or of an
ISO 8601 duration in weeks, days, hours, minutes and seconds, or of a position times a
declared interval. A duration with years or months SHALL be refused with
`TIME_BASIS_INVALID`. A basis with a zone and an increment SHALL declare whether the
increment counts elapsed time or wall-clock time. A local time that falls in a repeated
hour SHALL resolve by the declared fold rule, and a local time in a gap SHALL be refused
with `TIME_LOCAL_GAP` and counted without stopping the job. An unzoned time without a
declared zone, a declared offset or a declared floating placement SHALL still be refused.
A floating basis SHALL place a row by its local date and wall clock only, SHALL give it no
instant and no offset, and SHALL be refused with a zone, an offset, a clock rule or a fold
rule. A value that carries its own offset SHALL still resolve as an instant. A time
written to the minute SHALL read as second zero. A mapping MAY name a string field that
receives the row's canonical local wall clock. Preview SHALL count rows by how their time
resolved, and `describe` SHALL state that instant filters exclude floating rows. Zone
rules SHALL come from a pinned rules package whose version the job binding records.

#### Scenario: Samples across the autumn fold
- **WHEN** an unzoned sample sequence crosses the repeated hour and the mapping declares the zone with `fold: order`
- **THEN** each sample before the clock steps back takes the summer offset and each sample after it takes the winter offset

#### Scenario: A positional series on a short day
- **WHEN** a day's values are positions at a fixed interval from local midnight on the spring-forward date and the mapping declares `clock: elapsed`
- **THEN** each position maps to midnight's UTC instant plus its elapsed time, and no row falls in the gap

#### Scenario: Wall-clock times with no zone
- **WHEN** an export writes local times with no zone or offset and the mapping declares a floating basis
- **THEN** each row gets its local date and local wall clock, no instant, and an instant filter does not return it

#### Scenario: Increments in milliseconds and durations
- **WHEN** samples carry an offset from a start time in milliseconds, or as an ISO 8601 duration such as `PT1M30.5S`
- **THEN** each sample resolves to the start plus that offset, and a duration with months is refused

#### Scenario: Minute-precision times
- **WHEN** an export writes times as `2024-03-01T07:45`
- **THEN** each time reads as `07:45:00`

#### Scenario: Existing mappings keep their hash
- **WHEN** a mapping that declares none of the new time keys is compiled by this release
- **THEN** its canonical hash equals the hash the previous release computed

### Requirement: Derived import collections rebuild from their sources

A collection declared derived SHALL take rows only from imports and SHALL refuse row
edits, view edit-back and row tools with `COLLECTION_DERIVED`. A collection SHALL become
derived only at creation, in summary mode, and SHALL never be converted. Its rows SHALL
live outside the main collection store and outside the vault replica. The replica SHALL
carry its manifest, saved mappings, rollups and an import log that records, per member,
the manifest and member hashes, the row counts, a row digest, the mapping hash, and the
importer and zone-rules versions. A derived collection's canonical content is its
manifest, its import log and the preserved members the log names; its rows are a
rebuildable projection without per-row history, receipts or audit, and row-history
requests SHALL refuse with `COLLECTION_DERIVED` and name the import log. After its rows
are lost, Exomem SHALL rebuild them by replaying the logged imports from the preserved
members, SHALL derive each row's item key under the collection's recorded scheme, SHALL
check each member's row count and digest against the log, and SHALL report
the rebuild's progress instead of complete or zero results until it ends. An original
that a derived collection's log names SHALL NOT be pruned.

#### Scenario: Restore on a new machine
- **WHEN** a vault with a derived collection is restored where its rows do not exist
- **THEN** its rollups answer at once, row queries report the rebuild's progress, and the rebuilt rows match the logged counts and digests and keep their item keys

#### Scenario: A crash between the two stores
- **WHEN** the service stops after an import batch committed its rows but before the member's log entry committed
- **THEN** recovery resumes that member without duplicate rows, and the final rows match a run without the crash

#### Scenario: A logged member is missing
- **WHEN** a rebuild needs a member blob that is absent
- **THEN** the rebuild stops with `DERIVED_SOURCE_MISSING` naming the member, rollups keep answering, and row queries refuse instead of returning zero
