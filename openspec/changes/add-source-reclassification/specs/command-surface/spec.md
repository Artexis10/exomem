## ADDED Requirements

### Requirement: A captured source's classification is correctable

The system SHALL provide one operation that changes a captured source's source kind, its subject domain, or both, resolved through the same open-vocabulary rules that govern capture.

The operation SHALL require a stated reason for the correction, following the existing precedent that a reclassifying move names why the judgement changed.

Supplying neither axis SHALL be refused rather than treated as a no-op relocation, so the operation cannot be used as an unmotivated file move.

#### Scenario: A fallback capture is corrected to a real kind

- **WHEN** a source stored under the fallback kind is reclassified to a meaningful kind with a stated reason
- **THEN** the operation succeeds
- **AND** the source's recorded kind is the canonical form of the supplied value
- **AND** the source is no longer located under the fallback

#### Scenario: A domain is corrected without touching the kind

- **WHEN** only a domain is supplied for a source that already carries a meaningful kind
- **THEN** the recorded kind is unchanged
- **AND** the recorded domain is the canonical form of the supplied value

#### Scenario: A correction with no change is refused

- **WHEN** a reclassification supplies neither a kind nor a domain
- **THEN** the operation is refused
- **AND** the source is not moved

#### Scenario: A correction without a reason is refused

- **WHEN** a reclassification supplies a new classification but no reason
- **THEN** the operation is refused naming the missing reason
- **AND** the source is unchanged

### Requirement: Reclassification changes classification metadata and nothing else

The operation SHALL leave the source's body byte-identical. It SHALL change only the classification fields and the fields recording the correction itself.

It SHALL NOT alter the source's stable identity, its capture timestamp, its recorded origin, its tags, or the list of compiled notes that have ingested it.

Frontmatter mutation on an append-only source is already established: citing a source from a compiled note appends to that source's ingested-into list. Body immutability is the property being protected, and it SHALL remain protected here.

#### Scenario: The body survives a correction unchanged

- **WHEN** a source with body content is reclassified
- **THEN** the body after the correction is byte-identical to the body before it

#### Scenario: Identity and provenance fields survive a correction

- **WHEN** a source carrying a stable identity, a capture timestamp, an origin, tags, and ingested-into entries is reclassified
- **THEN** every one of those values is unchanged

### Requirement: Reclassification relocates the source to the projection its new classification implies

The operation SHALL move the source to the location the deterministic projection derives from its corrected kind and domain, and SHALL apply the same path-safety guarantees capture applies.

When the corrected classification projects to the location the source already occupies, the operation SHALL update the metadata and report that no relocation was required.

#### Scenario: A corrected source lands at its new projected location

- **WHEN** a source is reclassified to a kind and domain that project elsewhere
- **THEN** the source file exists at the projected location
- **AND** no file remains at the previous location

#### Scenario: A correction that does not change the projection moves nothing

- **WHEN** a source is reclassified such that the projection resolves to its current location
- **THEN** the metadata is corrected
- **AND** the operation reports that no relocation occurred

#### Scenario: An unsafe corrected value never reaches a path

- **WHEN** a reclassification supplies a value that cannot normalize into a safe canonical key
- **THEN** the operation is refused
- **AND** the source is neither moved nor modified

### Requirement: Reclassification preserves every reference to the source

The operation SHALL rewrite every inbound reference to the source's previous location so that no reference dangles, including references held in other pages' frontmatter provenance lists.

The operation SHALL record the previous path on the source so a caller holding the old location can still discover where the material went.

A reclassification SHALL be atomic: either the relocation, the metadata correction, and every reference rewrite all apply, or none of them do.

#### Scenario: Inbound references follow the source

- **WHEN** a source cited by a compiled note's provenance list and linked from another page is reclassified to a new location
- **THEN** the citing note's provenance entry names the new location
- **AND** the linking page's reference names the new location
- **AND** no reference to the previous location remains

#### Scenario: The previous path stays discoverable

- **WHEN** a source is reclassified to a new location
- **THEN** the source records the location it previously occupied

#### Scenario: A failure part-way leaves nothing half-applied

- **WHEN** a reclassification fails while applying its changes
- **THEN** the source remains at its original location with its original classification
- **AND** no inbound reference has been rewritten

### Requirement: Reclassification reports what it would do before doing it

The operation SHALL offer a read-only mode that reports the corrected classification, the location the source would move to, the number of references that would be rewritten, and the evidence supporting each proposed value, without writing anything.

The read-only mode SHALL accept a caller-supplied kind and domain and preview that correction, so a caller that has read the source and decided can show the destination and affected-reference count before anything is written. Supplied values SHALL be resolved through the same rules the correction applies, so a value the correction would refuse is refused during the preview rather than after approval.

When no values are supplied, evidence SHALL be limited to what is deterministically observable about the source: its current location, its recorded origin, its title, and its existing metadata. The operation SHALL NOT infer a classification through a model call, and SHALL report that it has no proposal rather than guessing when the observable evidence supports none.

#### Scenario: A preview writes nothing

- **WHEN** a reclassification is requested in read-only mode
- **THEN** the report names the destination and the number of affected references
- **AND** the source is unchanged at its original location
- **AND** no inbound reference has been rewritten

#### Scenario: Evidence accompanies a proposed value

- **WHEN** a proposal is requested for a source whose current location already carries a domain segment
- **THEN** the proposed domain is reported together with the observation that supports it

#### Scenario: A caller previews the correction it has decided on

- **WHEN** a preview is requested with a kind the caller has judged from reading the source
- **THEN** the report names the destination that kind projects to
- **AND** the report states that the value came from the caller rather than from observed evidence
- **AND** the source is unchanged at its original location

#### Scenario: A previewed value is canonicalized, not echoed

- **WHEN** a preview is requested with a kind or domain in non-canonical form
- **THEN** the reported value is its canonical form
- **AND** the reported destination is the one that canonical value projects to

#### Scenario: An undecidable source is reported, not guessed

- **WHEN** a proposal is requested for a source whose observable evidence supports no particular kind
- **THEN** the report states that no kind is proposed
- **AND** no fallback value is presented as a proposal

### Requirement: Reclassification is explicit and never automatic

The system SHALL NOT reclassify or relocate any source as a side effect of another operation, including capture, compilation, indexing, maintenance, registry edits, and advisory detection.

The classification advisory SHALL remain advisory: it reports that debt exists and SHALL NOT act on it.

#### Scenario: Ordinary operations move nothing

- **WHEN** captures, compilations, index updates, and maintenance run over a vault containing fallback-classified sources
- **THEN** no source is relocated or reclassified

#### Scenario: A registry edit does not migrate existing material

- **WHEN** a registry entry's path segment is changed after sources were filed under the previous segment
- **THEN** those sources stay where they are
- **AND** the operation reports no automatic migration
