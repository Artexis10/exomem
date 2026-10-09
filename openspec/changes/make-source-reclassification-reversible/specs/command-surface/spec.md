## MODIFIED Requirements

### Requirement: Reclassification preserves every reference to the source

The operation SHALL rewrite every inbound reference held in a mutable page so that no reference dangles, including references held in other pages' frontmatter provenance lists. A reference counts as rewritten only when its bytes change.

The operation SHALL leave references held in append-only pages unchanged. It SHALL refuse the move when an append-only reference would need a byte change to keep resolving. A reference that resolves unchanged, such as a basename or title link, SHALL NOT cause a refusal.

The operation SHALL append the previous path, kind and domain to the source's `reclassified_from` list, and SHALL write it without changing the source's body bytes. A legacy scalar `reclassified_from` value SHALL read as one entry with an unknown classification.

A reclassification SHALL be atomic: either the relocation, the companion move, the metadata correction, the history entry, and every reference rewrite all apply, or none of them do.

#### Scenario: Inbound references follow the source

- **WHEN** a source cited by a compiled note's provenance list and linked by path from another mutable page is reclassified to a new location
- **THEN** the citing note's provenance entry names the new location
- **AND** the linking page's reference names the new location

#### Scenario: An append-only basename link does not block the move

- **WHEN** an Evidence page links a source by its basename and the source is reclassified
- **THEN** the move succeeds and the Evidence page is unchanged
- **AND** the link still resolves to the source

#### Scenario: An append-only path link blocks the move

- **WHEN** an Evidence page links a source by its full path and the source is reclassified to a new location
- **THEN** the move is refused and nothing changes

#### Scenario: The previous path stays discoverable

- **WHEN** a source is reclassified twice
- **THEN** the source records both previous locations with their kinds and domains, oldest first

#### Scenario: A failure part-way leaves nothing half-applied

- **WHEN** a reclassification fails while applying its changes
- **THEN** the source remains at its original location with its original classification and history
- **AND** no inbound reference has been rewritten

### Requirement: Reclassification reports what it would do before doing it

The operation SHALL offer a read-only mode that reports the corrected classification, the location the source would move to, the mutable references that would be rewritten, the append-only references that stay unchanged grouped by link form, any refusal with its cause, and the evidence supporting each proposed value, without writing anything. The report SHALL name only pages the caller can see.

The read-only mode SHALL accept a caller-supplied kind and domain and preview that correction, so a caller that has read the source and decided can show the destination and affected references before anything is written. Supplied values SHALL be resolved through the same rules the correction applies, so a value the correction would refuse is refused during the preview rather than after approval.

When no values are supplied, evidence SHALL be limited to what is deterministically observable about the source: its current location, its recorded origin, its title, and its existing metadata. The operation SHALL NOT infer a classification through a model call, and SHALL report that it has no proposal rather than guessing when the observable evidence supports none.

#### Scenario: A preview writes nothing

- **WHEN** a reclassification is requested in read-only mode
- **THEN** the report names the destination and the affected references
- **AND** the source is unchanged at its original location
- **AND** no inbound reference has been rewritten

#### Scenario: A preview shows a refusal before approval

- **WHEN** a preview is requested for a source that an append-only page links by full path
- **THEN** the report states that the move would be refused and why
- **AND** it names that referrer only when the caller can see it

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

## ADDED Requirements

### Requirement: Reclassification can be reverted

The operation SHALL offer a revert that restores the source's latest recorded previous path, kind and domain through the same atomic, reference-preserving relocation, and removes that history entry. Revert SHALL restore a recorded `other` classification. It SHALL refuse an entry whose classification is unknown, and a previous path that another page now occupies.

#### Scenario: A reclassification round trip restores the original

- **WHEN** a source is reclassified and then reverted
- **THEN** it is at its original location with its original kind and domain
- **AND** its body bytes and every reference resolve as before the first move

#### Scenario: A legacy history entry cannot be reverted automatically

- **WHEN** revert is requested for a source whose only history entry came from a legacy scalar
- **THEN** the revert is refused with the reason
- **AND** nothing changes
