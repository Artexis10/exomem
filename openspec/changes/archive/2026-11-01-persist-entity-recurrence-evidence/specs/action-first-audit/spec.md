## ADDED Requirements

### Requirement: Recurring-identity evidence persists without changing findings
The system MAY persist per-page `entity_recurrence` evidence in a derived store between sweeps. A sweep that uses the store SHALL produce findings identical to a full sweep of current canonical state for the same audience. That covers findings, fingerprints, review identity, counts, and order. The sweep SHALL re-read only pages whose file identity changed since their row was verified, and SHALL drop rows of pages that no longer exist. The sweep SHALL ask the audience's egress checks for every page before reading its row. Cross-page aggregation SHALL use only the pages that audience may see. The store SHALL add no write-time work. Any store fault SHALL fall back to a full sweep for that call. Pages that are not indexable, or that are raw-protected, SHALL store no content.

#### Scenario: A persisted read answers exactly as a full sweep
- **WHEN** an explicit `entity_recurrence` sweep runs against a warm store
- **THEN** its findings are byte-identical to a full sweep of the same vault for the same audience
- **AND** no unchanged page is read again

#### Scenario: An edit made outside Exomem counts on the next read
- **WHEN** a page is edited, renamed, or deleted by another program between two sweeps
- **THEN** the next sweep reflects the change without a reconcile

#### Scenario: A new or re-aliased Entity reframes unrelated pages without a full read
- **WHEN** an Entity is created or its aliases change
- **THEN** the next sweep recomputes anchor-dependent frames on every page from stored evidence
- **AND** reads only the pages whose files changed

#### Scenario: A restricted audience aggregates only the pages it may see
- **WHEN** a page hidden from the requesting audience shares a Source with two visible pages
- **THEN** the hidden page contributes nothing to spread, origins, contexts, counts, or order
