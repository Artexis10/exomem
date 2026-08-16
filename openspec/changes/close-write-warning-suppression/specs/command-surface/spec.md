## ADDED Requirements

### Requirement: Write-path advisories are suppressed for exactly-dismissed fingerprints

Each write-path advisory — the near-duplicate warning, the contradiction-band warning, and the overlap warning — SHALL carry a stable review reference and a signal fingerprint derived from the advisory's endpoints and their content signal versions. Before emitting an advisory, the system SHALL consult the portable review state: an advisory whose exact `(review identity, fingerprint)` pair was dismissed SHALL NOT be emitted; a snoozed pair SHALL NOT be emitted before its expiry.

A material change to the counterpart endpoint SHALL produce a different fingerprint, and the advisory SHALL then be emitted again. A change to the written page itself SHALL resurface a dismissed advisory only when it changes the detected signal class for the pair. Ranking drift, unrelated writes, the triggering write's own change to the written page, and repeated identical page states SHALL NOT change the fingerprint.

Suppression SHALL be failure-isolated in the emitting direction: review state that cannot be read or parsed SHALL cause the advisory to be emitted, and SHALL NOT fail, delay, or alter the committed mutation.

#### Scenario: A dismissed duplicate warning stays quiet on the next write

- **WHEN** a near-duplicate advisory for a page pair is dismissed through triage, and a further write commits to the same page with the counterpart materially unchanged and the detected signal class unchanged
- **THEN** the committed result carries no near-duplicate advisory for that pair
- **AND** the mutation outcome, status, and path are unchanged from an emission-free write

#### Scenario: A material change resurfaces the advisory

- **WHEN** a previously dismissed advisory's counterpart page is materially edited, and a further write commits to the original page
- **THEN** the advisory is emitted again with a new fingerprint
- **AND** the earlier dismissal record does not suppress it

#### Scenario: Unreadable review state fails open to emission

- **WHEN** the portable review state cannot be read during a compiled write that would emit an advisory
- **THEN** the advisory is emitted
- **AND** the write commits with its existing terminal unchanged

#### Scenario: A declared rival pair produces no duplicate advisory

- **WHEN** a page pair carries a recorded competing-alternatives stance and a further write commits to either page
- **THEN** no near-duplicate advisory is emitted for that pair
- **AND** the suppression follows from the stance contract, not from a dismissal record

### Requirement: Write feedback and the audit report the same relation-debt predicate

The write-result feedback SHALL report relation debt using the same predicate as the audit's relation-debt category and the semantic connectivity lane: the presence of `sources:` provenance alone SHALL NOT clear the debt flag. The feedback SHALL report provenance presence as its own fact, distinct from debt, so a page with citations but no connections is described as exactly that.

#### Scenario: A cited but unconnected page reports debt consistently

- **WHEN** a compiled write commits to a page whose only outward reference is its `sources:` frontmatter, with no typed relations and no body wikilinks
- **THEN** the write-result feedback reports relation debt
- **AND** a subsequent audit reports the same page under the same debt condition
- **AND** the feedback separately reports that provenance is present
