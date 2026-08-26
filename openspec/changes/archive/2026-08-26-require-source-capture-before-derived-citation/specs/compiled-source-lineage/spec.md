## ADDED Requirements

### Requirement: Every explicit compiled source citation closes before commit

For a newly created or completely replaced compiled note, every non-empty `sources` entry SHALL resolve to caller-authorized governed Source or Evidence material before publication. A patch operation SHALL enforce the same rule whenever it adds, replaces, or otherwise changes the source list. Validation SHALL occur inside writer authority against the final normalized note immediately before publication.

#### Scenario: Captured source permits derived note

- **WHEN** a compiled-note write cites a visible governed Source or Evidence page
- **THEN** source closure succeeds and the write may proceed through the remaining semantic contract

#### Scenario: Missing source refuses without partial state

- **WHEN** one explicit source entry does not resolve to eligible governed captured material
- **THEN** the write returns `UNRESOLVED_SOURCE_CITATION` and writes no note, source back-reference, index state, or committed receipt

#### Scenario: Mixed source list is all-or-nothing

- **WHEN** some cited entries resolve and at least one does not
- **THEN** the entire compiled write refuses without updating the resolved sources

### Requirement: Honest source absence remains valid

An absent `sources` field or explicit `sources: []` SHALL be a valid final state and SHALL mean that the note asserts no external captured source. The system SHALL NOT invent, require, or suggest a citation solely because a note contains a conclusion.

#### Scenario: Original conclusion has no external source

- **WHEN** a valid compiled note is authored with `sources: []`
- **THEN** source closure accepts the note without creating provenance material or a source-closure refusal, while independent non-blocking provenance guidance remains governed by its own contract

#### Scenario: Empty is distinct from broken

- **WHEN** one note has an empty source list and another names a nonexistent entry
- **THEN** only the note with the non-empty unresolved claim is refused or audited

### Requirement: External locators are provenance, not substitute citations

Remote URLs, connector message IDs, file IDs, and similar external locators SHALL NOT satisfy source closure by themselves. They SHALL be preserved as origin metadata on captured Source or Evidence material, and a derived note SHALL cite that governed material by current path or stable reference.

#### Scenario: Connector identifier alone refuses

- **WHEN** a compiled note lists an uncaptured external message or file identifier in `sources`
- **THEN** source closure refuses and directs the caller to capture the original material first

#### Scenario: Captured external material preserves origin

- **WHEN** external material is captured with its connector identifiers and a derived note cites the resulting governed page
- **THEN** the citation resolves while the remote identifiers remain provenance on the captured page rather than on the derived note's source list

### Requirement: Source closure is authorization-safe and non-disclosing

The validator SHALL authorize before exposing resolved identity. Missing, malformed, ambiguous, ineligible, and withheld entries SHALL use the same bounded public refusal where distinguishing them would disclose corpus state. The refusal SHALL include only a capped list of the caller-supplied unresolved values, the total unresolved count, and capture-then-retry remediation; it SHALL NOT return candidate paths, titles, snippets, or authorization distinctions.

#### Scenario: Hidden and missing source look the same

- **WHEN** one caller cites a withheld governed source and another cites a nonexistent source with the same supplied spelling
- **THEN** both receive the same code, message shape, remediation, and absence of target details

#### Scenario: Unresolved list is bounded

- **WHEN** a write supplies more unresolved entries than the public cap
- **THEN** the refusal returns a deterministic capped sample plus the full count and truncation state

### Requirement: Derived note and source back-references publish atomically

After closure succeeds, supported source back-references to the derived note SHALL be updated in the same guarded batch as the note. The writer SHALL recheck source versions before publication. A concurrent source move, mutation, deletion, or authorization change SHALL refuse stale and SHALL leave both derived and source files unchanged.

#### Scenario: Successful write updates both sides

- **WHEN** a new compiled note cites two eligible sources that support governed back-references
- **THEN** the note and both back-references commit under one terminal receipt with no externally visible half-state

#### Scenario: Concurrent source change refuses atomically

- **WHEN** a cited source changes after resolution but before publication
- **THEN** the derived write refuses stale and no source or note bytes are published

### Requirement: Legacy unresolved claims do not wedge unrelated edits

An edit to an existing compiled note SHALL be allowed to preserve an unchanged legacy unresolved source list when the operation does not create, replace, or change that claim. Any operation that changes the source list SHALL validate the entire final list. A complete replacement SHALL always reassert and therefore validate every explicit source.

#### Scenario: Unrelated body correction remains possible

- **WHEN** a legacy note has an unresolved source and a guarded patch changes only an unrelated body span
- **THEN** the patch may commit while the legacy source debt remains auditable

#### Scenario: Adding one source validates all final sources

- **WHEN** an edit adds a captured source but retains an older unresolved source in the final list
- **THEN** the edit refuses because the changed final source claim does not close completely

### Requirement: Missing original material is never reconstructed automatically

Remediation SHALL require capture of the original material or explicit removal of the unsupported citation. The system MUST NOT create Source or Evidence material by copying a derivative note, excerpt, summary, or working script and representing it as the missing original.

#### Scenario: Derivative excerpt cannot become its own source

- **WHEN** audit finds an unresolved original citation but only a partial derivative remains in the vault
- **THEN** no automatic remediation creates a source from that derivative and the finding remains until original capture or explicit citation removal
