## ADDED Requirements

### Requirement: An uncorrected identity conflict joins the default attention union

Attention SHALL compose the `source_identity_conflict` audit family in its default union, after `original_fixity`.
Each finding SHALL name the Source, the DOIs the document shows, the DOI its `url` claims and the metadata-only correction call.
The family SHALL report only Sources whose recorded `identity_check.state` is `conflict`, so a vault whose Sources predate the identity check sees no finding from it.
A correction receipt for the Source SHALL resolve the finding.

#### Scenario: A conflict nobody corrected reaches the owner

- **WHEN** a Source records `identity_check.state: conflict` and has no correction receipt
- **THEN** default attention lists one `source_identity_conflict` finding for it, with the correction call

#### Scenario: Sources captured before the check stay quiet

- **WHEN** a vault holds Sources with no recorded `identity_check`
- **THEN** default attention lists no `source_identity_conflict` finding for them
