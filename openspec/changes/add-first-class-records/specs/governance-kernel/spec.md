## ADDED Requirements

### Requirement: Collection and record authorization precedes reduction
Every Records discovery, read, structured query, pagination total, aggregate, profile, rendered view, export-shaped response, template return, graph/reference expansion, and mutation receipt SHALL pass the existing governance and release boundary at the correct source or item granularity. Authorization SHALL occur before any count, grouping, latest selection, min/max, sum/average, distinct set, profile, or observed-state reduction is computed.

#### Scenario: Withheld items do not affect aggregate
- **WHEN** a file-per-item collection contains both released and withheld record files
- **THEN** filtering by release decision occurs before totals or aggregates and no withheld value affects the result

#### Scenario: Withheld collection is indistinguishable from absent
- **WHEN** the collection manifest or sole canonical log/dataset is below the release floor
- **THEN** Records reads and queries use the same public missing shape as a nonexistent collection

#### Scenario: UUID discovery hides withheld duplicates
- **WHEN** UUID resolution encounters both releasable and withheld candidate manifest paths
- **THEN** each path is authorized before identity-bearing content is parsed, ambiguity is computed only among releasable candidates, and withheld candidates remain indistinguishable from absence

#### Scenario: Aggregate cannot reveal concealed rows
- **WHEN** rows that would be withheld contain an extreme value or unique category
- **THEN** count, min/max, latest, distinct, profile, progress, and pagination metadata reveal no contribution from those rows

### Requirement: Governance granularity follows canonical representation
Canonical source and template paths SHALL be governed vault-relative paths resolved without symlink escape. For one-file-per-item storage, each canonical item path SHALL be independently authorized before parsing/reduction. For a chronological log or dataset, the canonical file SHALL be the first-delivery governance boundary and all contained items SHALL share its release classification. Collections requiring mixed sensitivity SHALL use separately governed item files or collections until explicit row-level policy exists.

#### Scenario: Log is authorized as one canonical artifact
- **WHEN** a log-backed collection is queried
- **THEN** Exomem authorizes the manifest and log path before parsing any block and does not claim unsupported per-row secrecy inside that file

#### Scenario: Item files can have mixed release decisions
- **WHEN** a file-per-item collection contains paths in different governed scopes
- **THEN** only authorized item files reach filter, sort, pagination, aggregate, or view computation

### Requirement: Records egress and receipts remain content-safe
Records responses and receipts SHALL register all path-, identity-, relation-, provenance-, conflict-, history-, and count-bearing fields with the release plane. Errors and receipts SHALL not echo withheld values, template contents, plan titles, record bodies, or sensitive identifiers beyond their authorized projection.

#### Scenario: Stale refusal leaks no current item content
- **WHEN** an unauthorized or stale update is refused
- **THEN** the response provides a bounded remediation and safe hashes/identifiers only at the caller’s release level

#### Scenario: Mutation receipt names authorized affected paths
- **WHEN** a Records mutation commits
- **THEN** the terminal receipt includes only the authorized collection/item/path metadata and records disclosure outcomes through the existing receipt system
