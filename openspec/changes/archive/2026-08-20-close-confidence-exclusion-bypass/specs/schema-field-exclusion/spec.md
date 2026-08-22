# schema-field-exclusion — delta

## ADDED Requirements

### Requirement: Governed writes refuse schema-excluded frontmatter fields

Exomem SHALL maintain one registry of schema-excluded frontmatter field names and one
refusal code for them. Every governed write path that accepts caller-authored
frontmatter — a frontmatter mapping, a whole file body, a collection manifest, or a
structured item payload — SHALL consult that registry and refuse before any bytes are
published. The comparison SHALL normalize case and surrounding whitespace. The refusal
SHALL name the offending field and state why the schema excludes it.

#### Scenario: A file body carrying an excluded field refuses

- **WHEN** a caller writes a Markdown file through the file-management surface with the
  frontmatter embedded in the body rather than supplied as a mapping
- **THEN** the write refuses with the shared excluded-field code and no bytes are
  published, whether the target is a new file or an overwrite of an existing one

#### Scenario: A collection manifest declaring an excluded field refuses

- **WHEN** a Records or Planning manifest declares a schema-excluded name in its item
  schema fields or as its Markdown-log note field, whether authored through the
  collection surface or hand-authored through the file-management surface
- **THEN** collection create and revise refuse with the shared excluded-field code, the
  refusal carries the offending field name, and the on-disk manifest is unchanged

#### Scenario: A structured item payload carrying an excluded field refuses

- **WHEN** a caller appends or updates a collection item whose supplied values carry a
  schema-excluded key
- **THEN** the write refuses before the writer lease is taken, and Planning add, update,
  and triage inherit the same refusal because they share the substrate

#### Scenario: The refusal is discoverable before authoring

- **WHEN** a client with no repository, skill, or fixture access requests the manifest
  authoring contract
- **THEN** the contract discloses the excluded field names, so the client never has to
  guess one to learn it is forbidden

### Requirement: Existing artifacts stay readable and repairable

The exclusion SHALL apply to caller-authored input only, never to stored state. Reading,
resolving, discovering, querying, and recall-candidacy evaluation SHALL be unaffected by
the presence of an excluded field in an artifact written before this contract. The
substrate SHALL retain a path to remove such a field.

#### Scenario: A pre-existing collection stays available

- **WHEN** a collection whose manifest declares an excluded field is loaded, resolved,
  discovered, queried, or evaluated for recall candidacy
- **THEN** every one of those operations behaves exactly as it did before this contract,
  and the collection is never silently dropped from recall candidacy

#### Scenario: Removing an excluded field is always permitted

- **WHEN** a caller removes an excluded field from an item through field deletion, or
  rebaselines a collection carrying one
- **THEN** the operation is permitted, because deletion and rebaseline are the
  remediation and refusing them would strand the artifact

#### Scenario: An unrelated edit does not refuse on stored values

- **WHEN** a caller updates an unrelated field of an item that already stores an
  excluded value
- **THEN** the update is evaluated against the supplied changes only, so it succeeds and
  leaves the stored excluded value untouched

### Requirement: Pre-existing violations surface for review, never block

Artifacts carrying an excluded field written before this contract SHALL surface as
review candidates through the existing frontmatter-compliance audit category at
non-blocking severity, carrying the ordered remediation. They SHALL NOT produce blocking
findings and SHALL NOT require a new audit category.

#### Scenario: A grandfathered violation is reported as a review candidate

- **WHEN** the audit runs over a vault containing a page with an excluded top-level
  frontmatter key, or a collection manifest declaring one in its item schema fields
- **THEN** each is reported under the existing frontmatter-compliance category at
  warning severity, no finding for them carries error severity, and the audit category
  registry is unchanged

#### Scenario: The remediation order is stated

- **WHEN** such a finding is reported for a collection
- **THEN** its proposed fix states that the field must be deleted from every item before
  the manifest is revised, because revision validation checks the proposed schema
  against stored record values
