## MODIFIED Requirements

### Requirement: Retained Interpretations Record The Writing Release

The parent of a compiled-history entry SHALL be a page other than a Source or
Evidence page that the semantic writer changes, or an episode page whose
previous text the episode recorder or the recap fold step retains. No other
Source or Evidence page SHALL have an entry.

Each compiled-history entry that the semantic writer retains for a changed
interpretation, and each entry that the episode recorder or the recap fold step
retains for an episode page, SHALL record `exomem_version`, the release of the Exomem server
that planned the transition, carried in the transition token so that an exact
retry re-plans identical history bytes. A history read SHALL return that field with the revision.
Entries retained before this field existed SHALL keep their stored bytes and
SHALL remain admissible without it; the system SHALL NOT backfill them. The
system SHALL NOT record a model identity, because the server cannot know it.

#### Scenario: A correction names its writing release

- **WHEN** a governed writer changes an interpretation and retains the prior
  interpretation in compiled history
- **THEN** the retained entry records the Exomem release that planned the change
- **AND** a history read of that page returns the release with the revision

#### Scenario: An entry from before the field still reads

- **WHEN** a history read meets an entry retained before `exomem_version`
  existed
- **THEN** the entry is admitted on its existing checks and returned without
  the field
- **AND** no stored entry is rewritten to add it

#### Scenario: A recap revision retains the previous episode text

- **WHEN** the episode recorder revises an episode page in place
- **THEN** one entry holds the page's previous bytes and records the Exomem release that wrote it
- **AND** a history read of the episode page returns the entry with that release

#### Scenario: Another Source page has no entry

- **WHEN** any writer changes a Source or Evidence page that is not an episode page
- **THEN** no compiled-history entry is created for that page
