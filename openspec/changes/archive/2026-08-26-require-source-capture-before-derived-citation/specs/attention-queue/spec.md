## ADDED Requirements

### Requirement: Audit reports legacy unresolved source citations distinctly

`review_memory(mode="audit")` and the equivalent audit facade SHALL accept category `unresolved_source_citation`. The category SHALL inspect explicit source fields on governed compiled notes and emit deterministic bounded findings for entries that do not resolve to eligible governed captured material. A source-field failure classified here SHALL NOT also emit a generic `broken_wikilink` finding for the same page and value.

#### Scenario: Legacy unresolved source appears in requested audit

- **WHEN** a governed compiled note contains a non-empty unresolved source entry and audit requests `unresolved_source_citation`
- **THEN** audit returns a finding anchored to the derived page with the bounded supplied value, total, and capture-or-remove remediation

#### Scenario: Source debt is not duplicated as a broken link

- **WHEN** all-category audit scans a source-field wikilink that does not resolve
- **THEN** the issue appears once under `unresolved_source_citation` rather than once there and again as `broken_wikilink`

### Requirement: Source-lineage audit is read-only and not default attention

The unresolved-source category SHALL perform no capture, edit, deletion, relation inference, or review-state mutation. It SHALL be available by explicit category and in all-category audit but SHALL NOT become a default Epistemic Inbox or attention signal family in this change.

#### Scenario: Audit cannot synthesize remediation

- **WHEN** only derivative content remains for an unresolved citation
- **THEN** audit reports the gap and performs no write to Sources, Evidence, or the derived note

#### Scenario: Default attention stays unchanged

- **WHEN** `review_memory(mode="attention")` runs without categories over a vault containing unresolved source citations
- **THEN** the default queue membership is unchanged while explicit audit can still return the source-lineage findings

### Requirement: Source-lineage findings clear only from current corpus truth

Finding identity SHALL be deterministic from the derived page identity and normalized unresolved source value. A finding SHALL disappear when the value resolves to eligible captured material or when a guarded edit explicitly removes it, and SHALL remain when only a derivative copy or unrelated source is present.

#### Scenario: Original capture clears the finding

- **WHEN** the original material is captured and the derived source citation is updated to its governed reference
- **THEN** the next fresh audit no longer returns that finding

#### Scenario: Unrelated capture does not clear the finding

- **WHEN** another source with similar text is captured but the cited value remains unresolved
- **THEN** the finding identity and open audit result remain unchanged

