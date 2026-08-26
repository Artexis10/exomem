## ADDED Requirements

### Requirement: FTS-Independent Semantic Catalog

The system SHALL maintain exact page and semantic-unit category/kind metadata in normal SQLite tables whose schema, readiness, and queries do not depend on FTS5 or trigram availability. The catalog SHALL be complete only when its stored freshness checkpoint matches a live snapshot or atomically applied complete delta AND its semantic projection identity matches the current catalog schema version, semantic-unit parser version, core category/authoring-contract identity, and extension semantic-language registry content hash. Any mismatch SHALL be rebuildable stale state. A missing or unverifiably stale catalog MUST NOT be interpreted as an authoritative empty index.

#### Scenario: Lean SQLite still has exact metadata

- **WHEN** FTS5 probing fails but the semantic catalog is current
- **THEN** exact category/kind parent and unit candidates are returned from normal indexed tables
- **AND** content-ranking lanes may degrade independently without changing metadata completeness

#### Scenario: Category semantics change without a note edit

- **WHEN** a sidecar built before the portable core contains authored `[constraints]` and the core contract later resolves it to `constraint` without changing that note
- **THEN** semantic projection identity mismatch prevents the old catalog from being treated as complete
- **AND** after rebuild, exact `constraint` retrieval returns the parent

#### Scenario: Extension registry save invalidates category candidates

- **WHEN** the extension semantic-language registry content hash changes without editing affected notes
- **THEN** the prior catalog is rebuildable stale even if Markdown scope freshness would otherwise appear current
- **AND** exact category recall cannot cache or return an authoritative empty result from the old projection

### Requirement: Candidate-First Category And Kind Algebra

The planner SHALL represent indexed eligibility as either `complete(paths)` or `unsupported`, and a complete set MAY be empty. Exact positive category/kind equality and membership predicates SHALL provide complete seeds. `AND` SHALL narrow by any available positive complete seed and post-evaluate remaining predicates; `OR` SHALL be complete only when every branch has a complete seed; a top-level `NOT` or page-only expression SHALL be unsupported. Hydrated candidates MUST pass canonical access policy and full structured-filter evaluation.

#### Scenario: Category and page status use one safe seed

- **WHEN** a filter is `category = constraint AND page.status = active`
- **THEN** the category catalog seeds candidates and page status is post-evaluated after bounded hydration
- **AND** returned hits match the full-scan oracle

#### Scenario: Unsafe disjunction uses the oracle

- **WHEN** a filter disjoins an indexed category predicate with a page-only branch
- **THEN** the plan is `unsupported` rather than an incomplete category set
- **AND** the existing full evaluator preserves correctness

#### Scenario: Complete empty does not trigger a scan

- **WHEN** complete positive seeds intersect to an empty candidate set
- **THEN** eligibility remains `complete` with zero paths
- **AND** the request returns zero hits without a Markdown scope walk

### Requirement: Candidate Cost Tracks Candidate Count

For a complete indexed plan, the system SHALL hydrate only catalog candidate parents plus a fixed bounded scene-frame/emitted-parent allowance. Corpus size MUST NOT determine the number of Markdown parents opened.

#### Scenario: Two candidates in 2,000 and 8,000 pages cost the same

- **WHEN** 2,000-page and 8,000-page fixtures each contain the same two exact category candidates
- **THEN** both requests open a constant bounded number of parents
- **AND** their hits equal the full-scan oracle

### Requirement: Empty-Query Category Recall Avoids A Corpus Walk

When eligibility is `complete(paths)` and the normalized keyword query is empty, keyword recall SHALL iterate those paths directly. A non-empty query SHALL intersect text candidates with eligible paths before hydration.

#### Scenario: Empty query hydrates eligible parents only

- **WHEN** `find` receives an empty query and exact category filter against a complete catalog
- **THEN** it returns matching parents without a scope Markdown walk
- **AND** navigation, access-policy, and emitted-parent rules remain enforced

### Requirement: Incomplete Exact Recall Is Observable

Internal index outcomes SHALL distinguish `available`, `stale`, `unsupported`, `transient_failure`, and `fatal_failure` and carry a completeness flag. If a safe exact category/kind request lacks a complete catalog and cannot apply a complete bounded delta, `find` SHALL return a typed `RETRIEVAL_INDEX_WARMING` operation error with `complete=false`, `status` equal to `warming` or `temporarily_unavailable`, and bounded `retry_after_ms`. It MUST NOT return or cache an authoritative empty hit list.

#### Scenario: Missing catalog is warming, not no results

- **WHEN** a safe exact category request arrives before catalog readiness
- **THEN** every command surface reports `RETRIEVAL_INDEX_WARMING` with `complete=false`
- **AND** retry metadata is bounded and the response is excluded from the hot result cache

### Requirement: Transient SQLite Failure Is Recoverable

`SQLITE_BUSY`, `SQLITE_LOCKED`, `SQLITE_INTERRUPT`, and canonical busy/locked messages SHALL fail only the current sidecar operation. They MUST NOT permanently retire the store. `SQLITE_CORRUPT` and `SQLITE_NOTADB` MAY mark the disposable sidecar fatal; schema/version mismatch SHALL be rebuildable stale state. No other generic `sqlite3.Error` may set process-lifetime retirement without a proven fatal code.

#### Scenario: Lock does not poison the next query

- **WHEN** one catalog query receives `database is locked` and the lock is released
- **THEN** that call reports a transient incomplete outcome
- **AND** the next call opens a new connection and succeeds without process restart

### Requirement: Ordinary Reads Do Not Negotiate Journal Mode

Ordinary sidecar connections SHALL set bounded busy and synchronous policy without executing `PRAGMA journal_mode=WAL`. Journal-mode setup SHALL occur only during schema setup or rebuild and SHALL soft-fail. A background replacement SHALL capture a start freshness checkpoint and semantic identity, build a temporary sidecar, replay a complete delta through an exact target checkpoint, persist that checkpoint and identity, and only then publish atomically. Overflow or semantic-identity change during the build SHALL discard/retry it. Foreground requests MUST NOT VACUUM, fully rebuild, or move a large sidecar.

#### Scenario: Read path remains non-mutating at connection setup

- **WHEN** a catalog read opens a connection to a current sidecar
- **THEN** no journal-mode transition is requested
- **AND** the read can coexist with a writer under the configured bounded busy policy

#### Scenario: Edit during background rebuild is not lost

- **WHEN** a Markdown edit arrives after a background build captures its start checkpoint
- **THEN** the build replays that edit through a complete target checkpoint before publication, or retries if completeness cannot be proven
- **AND** the published catalog stores the exact target checkpoint and is never marked complete for a snapshot it does not contain

### Requirement: Bounded Foreground Delta Repair

The system SHALL repair a stale catalog in the foreground only from a complete atomic freshness delta containing at most 32 changed-plus-deleted Markdown paths. It SHALL apply the paths and store the delta's exact target checkpoint in one SQLite transaction, then retry once. Unknown, incomplete, or larger deltas MUST schedule one background repair and produce the incomplete exact-recall outcome for safe category plans.

#### Scenario: One missed writer update heals immediately

- **WHEN** freshness returns one changed path in a complete delta from the catalog checkpoint
- **THEN** the request patches that parent, commits the target checkpoint, retries once, and returns current units
- **AND** no unrelated parent is opened

#### Scenario: Unknown drift never masquerades as exact

- **WHEN** catalog freshness is stale and the delta is unknown or exceeds 32 paths
- **THEN** no foreground corpus walk or rebuild occurs
- **AND** the caller receives `complete=false` while one background repair is scheduled

### Requirement: FTS-Unavailable Category Correctness

Auto backend mode SHALL use a complete semantic catalog for exact category/kind recall when FTS5 is unavailable. If the catalog is not complete, the request SHALL report `RETRIEVAL_INDEX_WARMING`; it MUST NOT false-empty or synchronously walk the corpus.

#### Scenario: FTS absence does not erase a category hit

- **WHEN** FTS5 is unavailable, the catalog is complete, and one unit has the requested category
- **THEN** exact page-level and unit-level category retrieval returns that hit
- **AND** diagnostics identify the metadata-only backend
