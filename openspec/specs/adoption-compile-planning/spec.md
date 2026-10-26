# adoption-compile-planning Specification

## Purpose
Plan compilation of selected legacy material from an existing vault without migrating or altering the originals, returning proposals instead of creating compiled notes. Adoption outputs carry stable `exomem://` context references alongside the existing path fields.
## Requirements
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

### Requirement: Source feedback separates custody from semantic compilation

Source descriptions, compilation proposals and ordinary compact capture/write feedback SHALL distinguish original custody, media extraction, citation linkage and semantic compilation.
Semantic compilation SHALL report `unassessed`; the server SHALL NOT infer completion from extraction, note counts, edge counts or citations.
The existing source-backlog criterion SHALL remain one live citing derivative and SHALL be described as citation linkage only.
Feedback SHALL provide bounded actionable guidance using only the existing admitted source view; full detail SHALL remain available through the existing route.
It SHALL NOT disclose withheld source names, paths, hashes, counts or metadata.
No new semantic-completion database or server-side semantic author SHALL be introduced.

#### Scenario: One citation establishes linkage only
- **WHEN** a live derivative cites a captured source and clears the existing source backlog criterion
- **THEN** feedback reports citation linkage and semantic compilation remains unassessed

#### Scenario: Extraction completion is independent
- **WHEN** media extraction completes or remains pending
- **THEN** feedback describes extraction state only and does not establish semantic completion
- **AND** pending extraction does not prohibit the agent from using an available original

#### Scenario: Withheld and absent sources disclose no difference
- **WHEN** equivalent restricted requests refer to a withheld source and an absent source
- **THEN** source feedback discloses no hidden identity, metadata or counts

### Requirement: Compilation preparation is optional and scope-honest

A compilation proposal SHALL remain an optional one-note scaffold rather than a source-wide semantic plan.
Adjacent pages SHALL remain unreviewed candidates; the generator SHALL NOT author generic default relation bullets or instruct the agent to copy candidates as accepted edges.
Opening excerpts SHALL disclose opening-only scope and the existing admitted full-source read route.
File capture and adoption SHALL honour requested compilation guidance through the existing preparation route.

#### Scenario: An attachment requests guidance
- **WHEN** an admitted file capture requests compilation guidance
- **THEN** its response provides the preparation route and unassessed semantic state instead of ending after custody feedback

#### Scenario: A proposal preserves choice
- **WHEN** a proposal includes an opening excerpt and adjacent pages
- **THEN** it labels the excerpt as opening-only, provides the full-source read route, and leaves candidate relations unauthored

### Requirement: The active agent checks source-wide meaning once

The canonical ingest workflow SHALL assign semantic judgment to the active conversation agent.
It SHALL inventory available full-source themes and central durable referents, retrieve prior conclusions, entities and filing context, and split independently useful outputs by meaning.
It SHALL preserve attribution and distinguish observations, hypotheses and measurements; it SHALL author specific grounded relations only when supported.
After the writes, it SHALL check the whole inventory once and report unread, unavailable or uncompiled portions honestly.
It SHALL NOT require semantic word lists, fixed topic or entity sets, minimum counts, mandatory edges, a second model, repeated source warnings or user curation instructions.
The existing request and engagement policy SHALL select the workflow; no special user wording SHALL be required.
Raw capture SHALL remain valid without compilation, and unassessed semantic state SHALL NOT create a new source backlog.

#### Scenario: Natural source use carries the selected compilation work
- **WHEN** source material enters natural conversation and the existing request and engagement policy select compilation
- **THEN** the workflow directs the active agent through inventory, prior-knowledge retrieval, semantic compilation and one final coverage check
- **AND** counts and a single scaffold do not stand in for semantic coverage
