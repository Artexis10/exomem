## ADDED Requirements

### Requirement: Imports read export members and nested JSON documents

An import source MAY be an export manifest with a member selector, a glob or a list of
paths. The job SHALL bind the manifest's SHA-256 and the ordered SHA-256s of the selected
members, SHALL verify each member's hash while it streams, and SHALL checkpoint by member
and row. A `json-document` source SHALL take rows from a declared row path that may cross
nested arrays, with field paths relative to the row, ancestor paths from the document root
or an enclosing element, the row's index, and literals. The reader SHALL stream, so memory
does not grow with a member's size. A job SHALL skip a member already imported into the
same collection with the same mapping unless the caller asks to re-import it. A field MAY
declare a numeric scale.

#### Scenario: Rows from a nested array
- **WHEN** a member holds a list of devices, each with a list of samples, and the mapping declares a row path through both lists
- **THEN** each sample becomes one row that can also carry a field of its device and of the document

#### Scenario: A restart in the middle of a member
- **WHEN** the service restarts while a job is part way through a member
- **THEN** the job resumes in that member and no row is imported twice

#### Scenario: A second export of the same account
- **WHEN** a job runs over a newer export whose older members have the same SHA-256 as members already imported with the same mapping
- **THEN** the job reads only the members it has not imported

### Requirement: Import time bases declare zones, offsets and positions

A time basis MAY combine a date or an instant with a declared IANA zone, an offset field
in minutes or ISO form, and an increment of seconds from a field or of a position times a
declared interval. A basis with a zone and an increment SHALL declare whether the
increment counts elapsed time or wall-clock time. A local time that falls in a repeated
hour SHALL resolve by the declared fold rule, and a local time in a gap SHALL be refused
with `TIME_LOCAL_GAP` and counted without stopping the job. An unzoned time without a
declared zone or offset SHALL still be refused. Zone rules SHALL come from a pinned rules
package whose version the job binding records.

#### Scenario: Samples across the autumn fold
- **WHEN** an unzoned sample sequence crosses the repeated hour and the mapping declares the zone with `fold: order`
- **THEN** each sample before the clock steps back takes the summer offset and each sample after it takes the winter offset

#### Scenario: A positional series on a short day
- **WHEN** a day's values are positions at a fixed interval from local midnight on the spring-forward date and the mapping declares `clock: elapsed`
- **THEN** each position maps to midnight's UTC instant plus its elapsed time, and no row falls in the gap

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
members, SHALL check each member's row count and digest against the log, and SHALL report
the rebuild's progress instead of complete or zero results until it ends. An original
that a derived collection's log names SHALL NOT be pruned.

#### Scenario: Restore on a new machine
- **WHEN** a vault with a derived collection is restored where its rows do not exist
- **THEN** its rollups answer at once, row queries report the rebuild's progress, and the rebuilt rows match the logged counts and digests

#### Scenario: A crash between the two stores
- **WHEN** the service stops after an import batch committed its rows but before the member's log entry committed
- **THEN** recovery resumes that member without duplicate rows, and the final rows match a run without the crash

#### Scenario: A logged member is missing
- **WHEN** a rebuild needs a member blob that is absent
- **THEN** the rebuild stops with `DERIVED_SOURCE_MISSING` naming the member, rollups keep answering, and row queries refuse instead of returning zero
