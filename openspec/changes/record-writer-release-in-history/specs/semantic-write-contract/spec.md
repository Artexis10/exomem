## ADDED Requirements

### Requirement: Retained Interpretations Record The Writing Release

Each compiled-history entry that the semantic writer retains for a changed
interpretation SHALL record `exomem_version`, the release of the Exomem server
that wrote the entry. A history read SHALL return that field with the revision.
Entries retained before this field existed SHALL keep their stored bytes and
SHALL remain admissible without it; the system SHALL NOT backfill them. The
system SHALL NOT record a model identity, because the server cannot know it.

#### Scenario: A correction names its writing release

- **WHEN** a governed writer changes an interpretation and retains the prior
  interpretation in compiled history
- **THEN** the retained entry records the Exomem release that wrote it
- **AND** a history read of that page returns the release with the revision

#### Scenario: An entry from before the field still reads

- **WHEN** a history read meets an entry retained before `exomem_version`
  existed
- **THEN** the entry is admitted on its existing checks and returned without
  the field
- **AND** no stored entry is rewritten to add it
