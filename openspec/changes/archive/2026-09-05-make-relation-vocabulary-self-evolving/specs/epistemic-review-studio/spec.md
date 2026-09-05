## ADDED Requirements

### Requirement: Relation worklist preserves bounded server semantics

The Studio relation worklist SHALL preserve the server-provided group and candidate order, source-path hints, source content hashes, stable refs, fingerprints, coverage, and truncation. It MUST NOT recompute relation candidates or activation coverage in the browser. A graph `warming`, `pending`, or `unavailable` response SHALL render as a distinct retryable state rather than an empty completed queue or an indefinite loading indicator.

#### Scenario: Relation queue warming is visible
- **WHEN** the Studio requests the relation worklist while the graph is not current
- **THEN** it renders the server's state and retry guidance and leaves prior review decisions untouched
- **AND** it does not issue per-page suggestion calls as a fallback

#### Scenario: Truncated queue remains honest
- **WHEN** the server caps candidate pages or evidence
- **THEN** the Studio displays the returned coverage and truncation indicators
- **AND** it does not label the shown prefix as the complete vault backlog

### Requirement: Studio relation decisions echo source hints

The Studio SHALL include the selected item's source path, fingerprint, and required content hash when accepting a relation candidate, and SHALL include the source path and fingerprint when triaging it. It SHALL update the worklist only after a successful governed response and SHALL render stale-source or stale-fingerprint refusals as refresh-required states.

#### Scenario: Accept uses bounded revalidation inputs
- **WHEN** a user confirms one relation candidate
- **THEN** the Studio sends the stable ref, source path, source content hash, fingerprint, and audit reason to the governed accept operation
- **AND** a drift refusal preserves the unmodified visible item until refresh
