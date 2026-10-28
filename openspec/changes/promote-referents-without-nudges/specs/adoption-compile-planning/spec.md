## ADDED Requirements

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
