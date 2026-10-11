## ADDED Requirements

### Requirement: Knowledge Time Is Recorded At Second Granularity In UTC
Governed writes SHALL record `created`, `updated`, `captured`, and `log.md` history headings as
a timezone-aware instant truncated to the second and rendered in UTC as `YYYY-MM-DDTHH:MM:SSZ`.

Sub-second precision SHALL NOT be recorded. Milliseconds would be false precision on
hand-authored notes, and would let two values compare unequal on a difference neither the
writer nor the reader can account for.

The author's local timezone SHALL NOT be stored. Where an edit happened is a different fact
from when it happened, and a single storage standard is what makes values comparable across
machines without consulting a timezone database.

#### Scenario: A note revised twice in one afternoon is orderable
- **WHEN** a page is created and then revised twice within the same day
- **THEN** its `updated` value and each of its history headings carry distinct instants
- **AND** their order is recoverable from the recorded values alone, not from position in the file

#### Scenario: A local offset is normalized on write
- **WHEN** a write is stamped from a clock carrying a non-UTC offset
- **THEN** the recorded value is the same instant expressed in UTC with a `Z` suffix

### Requirement: Existing Date-Only Values Are Never Rewritten
Values already recorded as a bare `YYYY-MM-DD` SHALL be left byte-identical by every read,
audit, and repair path. No migration SHALL restamp them.

A date-only value denotes an unknown instant within that day. Rewriting it as midnight would
assert a precision that was never captured, which is a fabrication rather than a normalization.
Both forms are therefore permanent and every reader MUST accept either.

#### Scenario: A repair pass leaves legacy pages alone
- **WHEN** the audit-repair pass runs over a compliant page carrying date-only values
- **THEN** the page's bytes are unchanged

#### Scenario: Copy-forward preserves recorded precision
- **WHEN** a repair copies a recorded value onto a missing sibling field
- **THEN** an instant is copied as an instant and a day is copied as a day, without coarsening or inventing precision

### Requirement: Ordering Two Recorded Values Is Four-Valued
Comparing two recorded values SHALL yield `before`, `after`, `same`, or `indeterminate`.

Two values sharing a calendar day SHALL compare `indeterminate` when either lacks a time.
Distinct days SHALL always order, because a whole day precedes the next whatever the unknown
times within them. Only two known instants MAY compare `same`.

An indeterminate result SHALL be reported rather than resolved. Where a total order is required
for display, the ambiguity SHALL remain visible rather than being implied away by the sort.

#### Scenario: A day and an instant within it cannot be ordered
- **WHEN** `2026-08-05` is compared with `2026-08-05T09:00:00Z`
- **THEN** the result is `indeterminate`
- **AND** the same comparison with the arguments swapped is also `indeterminate`

#### Scenario: Whole days still order
- **WHEN** `2026-08-04` is compared with `2026-08-05T09:00:00Z`
- **THEN** the result is `before`

### Requirement: Recorded Precision Does Not Decide Searchability
A page's recorded day SHALL decide day-granular date filters regardless of how the value is
spelled in YAML. Bare dates, quoted dates, bare timestamps, and quoted timestamps all denote a
recorded day and MUST answer `recency_days`, `updated_after`, and `updated_before` identically.

A value that cannot be parsed SHALL NOT acquire an inferred day; it continues to fail date
filters rather than matching on a guess.

#### Scenario: A quoted date is searchable
- **WHEN** a page written through the frontmatter serializer carries `updated: "2026-01-15"`
- **THEN** it is returned by a date filter whose window contains 2026-01-15

#### Scenario: A timestamped page is searchable
- **WHEN** a page carries `updated: 2026-01-15T09:12:33Z`
- **THEN** it is returned by a date filter whose window contains 2026-01-15

### Requirement: Path Dates And Dedup Keys Remain Day-Granular
Note paths, draft-token render dates, index recent-activity bullets, and write-idempotency keys
SHALL use the calendar day, not the recorded instant.

The day used for a path SHALL be read from the clock as given rather than converted to UTC, so
a note written late in the evening is not filed under the previous month.

#### Scenario: A late-evening write keeps its month
- **WHEN** a note is written at 01:30 local time at a positive UTC offset, crossing into a new month locally
- **THEN** its path carries the local month
- **AND** its recorded knowledge time is the corresponding UTC instant

#### Scenario: A replayed write stays idempotent
- **WHEN** the same write is attempted twice within one day
- **THEN** the idempotency key is unchanged, because it is derived from the day rather than the instant

### Requirement: History Headings Parse At Both Precisions
The `log.md` history-heading reader SHALL accept both `[YYYY-MM-DD]` and
`[YYYY-MM-DDTHH:MM:SSZ]`.

An unmatched heading is dropped silently rather than raising, so a reader that recognizes only
one precision would empty a page's history with no error surfacing anywhere. The reader MUST
therefore accept every form the writer can emit.

#### Scenario: A mixed log reads back completely
- **WHEN** a page's log carries one date-only heading and one timestamped heading
- **THEN** both entries are returned, newest first

### Requirement: Sub-Day Retrieval Bounds Report What They Cannot Determine
`updated_after` and `updated_before` SHALL accept an instant as well as a day. `recency_days`
SHALL remain day-scoped.

When a bound carries an instant and a candidate page records only a day, the two are genuinely
unordered whenever they share that day. The page SHALL be returned and the hit SHALL be marked
indeterminate. It MUST NOT be silently dropped, which reproduces the defect this change exists
to fix, and MUST NOT be silently included, which reports a guess as a fact.

Pages whose recorded day falls wholly inside or outside the bound SHALL be decided normally,
because a whole day orders against an instant outside it.

#### Scenario: A date-only page on the boundary day is returned and marked
- **WHEN** `updated_after` is `2026-08-05T09:00:00Z` and a page records only `2026-08-05`
- **THEN** the page is returned
- **AND** its hit is marked indeterminate for that bound

#### Scenario: A date-only page outside the boundary day is decided
- **WHEN** `updated_after` is `2026-08-05T09:00:00Z` and a page records only `2026-08-04`
- **THEN** the page is excluded, and no indeterminacy is reported

#### Scenario: A timestamped page is decided exactly
- **WHEN** `updated_after` is `2026-08-05T09:00:00Z` and a page records `2026-08-05T11:00:00Z`
- **THEN** the page is returned and its hit is not marked indeterminate

### Requirement: A Frozen Draft Commits To Identical Bytes
A draft token SHALL freeze both the authored day and the authored instant at validation, and a
commit SHALL use the frozen values rather than re-reading the clock.

Committing the same token twice SHALL produce byte-identical output. Re-reading the clock at
commit would make a validated draft render differently on each attempt, which defeats replay
safety and makes any byte-identity comparison between two code paths intermittently false
rather than cleanly true.

A token that predates the frozen instant SHALL be rejected rather than completed by inferring
one, because inferring the instant reintroduces exactly the non-determinism being prevented.

#### Scenario: Validation and commit straddle midnight
- **WHEN** a draft is validated on one day and committed on the next
- **THEN** the written `created` is the authored instant frozen at validation, not the commit-time clock

#### Scenario: Two code paths writing the same change agree
- **WHEN** two governed operations that should produce the same page are compared byte-for-byte under a pinned clock
- **THEN** their output is identical, including the recorded knowledge time
