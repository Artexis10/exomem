## ADDED Requirements

### Requirement: Compile-selected adoption plans selected material without migrating originals
The system SHALL implement `adopt(mode="compile-selected")` as an explicit selected-path workflow that preserves every original file outside `Knowledge Base/`, copies importable legacy text files into governed Sources with provenance when needed, and returns compilation proposals without creating compiled notes.

#### Scenario: Legacy file is planned through a governed source copy
- **WHEN** a user runs `adopt(mode="compile-selected")` with an importable legacy markdown file outside `Knowledge Base/`
- **THEN** the original file remains byte-identical
- **AND** the system creates a governed `Knowledge Base/Sources/Imported/` copy with original path, SHA-256, and byte-size provenance
- **AND** the response includes a compile plan whose suggested sources point at the governed source copy
- **AND** no `Knowledge Base/Notes/` compiled page is created

#### Scenario: Missing selection is rejected
- **WHEN** a user runs `adopt(mode="compile-selected")` without `selected_paths`
- **THEN** the system fails with a structured missing-selection error
- **AND** no files are written

#### Scenario: Unsupported selections are skipped safely
- **WHEN** selected paths include unsupported file types or non-source governed files
- **THEN** those selections are reported in `skipped` with structured reasons
- **AND** importable selections still proceed
- **AND** no original file is modified, moved, or deleted

### Requirement: Adoption outputs expose stable context references
The system SHALL include stable `exomem://` context references in adoption outputs for original files, copied sources, saved manifests, and compile proposals while preserving existing path fields for tool compatibility.

#### Scenario: Compile plan returns refs for review
- **WHEN** `adopt(mode="compile-selected")` returns a compile plan
- **THEN** each planned item exposes an original ref such as `exomem://vault/<path>` when an original exists
- **AND** each governed source exposes a source ref such as `exomem://source/<Knowledge Base source path>`
- **AND** the plan exposes a proposal ref that can be cited in manifest/review text

#### Scenario: Existing mode outputs remain compatible
- **WHEN** a user runs existing modes such as `save-manifest` or `copy-as-sources`
- **THEN** existing path fields remain present and unchanged
- **AND** new ref metadata is additive
