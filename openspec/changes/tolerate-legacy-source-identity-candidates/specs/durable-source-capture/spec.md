## MODIFIED Requirements

### Requirement: Canonical Sources decide identity uniqueness

Identity lookup and creation SHALL share the existing mutation guard.
Canonical Source and Evidence pages SHALL be the authoritative lookup, regardless of search, graph or sidecar state.
Unreadable or malformed candidate pages MUST return `SOURCE_IDENTITY_UNAVAILABLE`, because an incomplete scan cannot prove absence.
Canonical receipts MUST be validated regardless of Markdown filename, including `index.md` and the root Sources catalog path.
After bound originals are classified, readable frontmatter-free legacy pages outside the reserved root Sources catalog SHALL be excluded from receipt candidates.
Capture SHALL report each excluded legacy page for curation through its warnings, including when it reuses an existing capture.
The maintained root Sources catalog SHALL remain excluded without a curation warning.
If that reserved path lacks either maintained section header, lookup MUST return `SOURCE_IDENTITY_UNAVAILABLE` before capture can rewrite it.
Stale catalog rows MUST NOT determine identity uniqueness or absence.
Catalog recognition does not establish ownership or distinguish a former receipt whose metadata and bytes were replaced with a valid catalog.
Legacy Sources MUST NOT be backfilled or deduplicated implicitly.

#### Scenario: Concurrent requests use different invocation keys
- **WHEN** two callers concurrently capture the same identity and inputs with different invocation keys
- **THEN** exactly one Source is created and the other call returns it as reused

#### Scenario: Derived indexes lag
- **WHEN** a matching Source exists but its derived indexes have not caught up
- **THEN** reconciliation returns the canonical Source reference without another capture

#### Scenario: Canonical lookup is incomplete
- **WHEN** a canonical Source candidate cannot be read or parsed
- **THEN** capture returns the typed unavailable error without claiming absence or writing

#### Scenario: A Source is promoted into Evidence
- **WHEN** a captured Source is explicitly promoted and its original capture is repeated after replay expires
- **THEN** capture returns the same stable reference at the promoted path without reclassification
- **AND** a promoted page named `index.md` remains authoritative

#### Scenario: A Markdown original forges receipt and companion metadata
- **WHEN** a bound Markdown original contains capture identity or a declaration that targets another canonical page
- **THEN** its metadata cannot provide receipt truth or suppress that canonical page
- **AND** lookup gives the same result in either enumeration order

#### Scenario: Legacy originals coexist with durable receipts
- **WHEN** one readable frontmatter-free legacy page exists beside valid receipt pages
- **THEN** identity-enabled capture creates or reuses its correct Source and reports the legacy page for curation
- **AND** the legacy page remains unchanged
