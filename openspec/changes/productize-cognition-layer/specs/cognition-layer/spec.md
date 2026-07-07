# cognition-layer

## ADDED Requirements

### Requirement: Exomem exposes a simple durable-cognition model
The product documentation and agent bootstrap SHALL describe Exomem as a durable
knowledge base for sources, proof, history, decisions, records, review, and
compiled knowledge. The user-facing workflow SHALL be expressed through simple
actions — save, import/adopt, ask, prove, review, update, and connect — rather
than requiring users to understand internal page types before first value.

#### Scenario: New user reads the product model
- **WHEN** a new user reads the quickstart or bootstrap summary
- **THEN** they see that built-in AI memory is for preferences/routing and
  Exomem is for durable governed knowledge
- **AND** the first workflow examples use simple verbs instead of requiring the
  user to choose Sources, Notes, Entities, Evidence, or supersession directly

### Requirement: Existing vault adoption is scan-first and non-destructive
The system SHALL provide an adoption workflow for a vault that already contains
notes or files. The default adoption mode SHALL be read-only and SHALL report
what was found, what remains untouched, whether `Knowledge Base/` exists, likely
content clusters, candidate knowledge packs, and proposed next actions. The
workflow SHALL NOT rewrite, move, delete, add frontmatter to, or restructure
existing non-KB files by default.

#### Scenario: Existing vault scan does not mutate files
- **WHEN** adoption scan runs on a vault with existing notes, media, and no
  `Knowledge Base/`
- **THEN** the report describes the vault and candidate next actions
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Adoption distinguishes searchable from governed
- **WHEN** adoption scan runs on a vault with sibling folders and an initialized
  `Knowledge Base/`
- **THEN** the report states which content is searchable read-only input and
  which content is governed Exomem-managed knowledge

### Requirement: Adoption supports explicit safe modes
The adoption workflow SHALL expose explicit modes: scan-only, save-manifest,
copy-as-sources, and compile-selected. Scan-only SHALL be the default.
Save-manifest SHALL write only an adoption report under a governed Exomem
location. Copy-as-sources SHALL copy selected originals into `Sources/Imported`
or an equivalent governed source location with original path/hash provenance.
Compile-selected SHALL create compiled knowledge from selected sources with
source links. Any rewrite-in-place behavior, if ever supported, SHALL be
advanced-only and never the default.

#### Scenario: Copy mode preserves provenance
- **WHEN** a user selects files for copy-as-sources
- **THEN** Exomem creates source records or copies under the governed KB layer
- **AND** each copied/imported item records original path and content hash
- **AND** the original file remains unchanged

#### Scenario: Compile mode links back to sources
- **WHEN** a user compiles selected legacy notes into governed knowledge
- **THEN** the compiled page cites the imported or original source paths
- **AND** the operation logs the reason and created paths

### Requirement: Knowledge packs are extensible schema/workflow bundles
The system SHALL support declarative knowledge packs that compose durable
primitives such as sources, compiled knowledge, entities, decisions, evidence,
records, assets, projects/cases, and review state. Packs SHALL define routing
hints, optional frontmatter extensions, examples, and review checks for a
domain without requiring a new storage engine or a hard-coded top-level folder
per domain.

#### Scenario: Built-in packs are listed
- **WHEN** the system lists available knowledge packs
- **THEN** it includes built-in packs for legal/warranty, creative, technical,
  health/athletic, business, and personal records
- **AND** each pack declares its primitives, actions, and examples

#### Scenario: Custom pack validates
- **WHEN** a user or deployment adds a custom pack file
- **THEN** Exomem validates required metadata and rejects malformed packs with a
  stable error code and remediation

### Requirement: Evidence is treated as case-bound proof
The product model SHALL distinguish raw Sources from Evidence. A Source is raw
material captured into the knowledge base. Evidence is a source or artifact used
as proof for a claim, case, dispute, warranty, insurance issue, legal matter,
medical record, purchase, or other proof-bearing context. Evidence workflows
SHALL preserve provenance and SHALL NOT imply that all raw material is evidence.

#### Scenario: Receipt saved for warranty
- **WHEN** a user asks to save a receipt for a warranty case
- **THEN** Exomem treats the receipt as proof for that case, preserving source
  or artifact provenance
- **AND** retrieval can later answer "show the evidence for this case"

#### Scenario: Article saved as ordinary source
- **WHEN** a user saves an article for later research with no proof/case intent
- **THEN** Exomem captures it as a Source, not as Evidence

### Requirement: Product layer hides ontology unless requested
The agent guidance and user documentation SHALL instruct agents to route user
intent into the correct internal type while speaking in simple product language.
Advanced ontology terms MAY be shown when the user asks for implementation
details, audit output, or file paths, but they SHALL NOT be required for normal
save, ask, prove, review, update, or adopt workflows.

#### Scenario: Agent saves a durable conclusion
- **WHEN** the user reaches a durable conclusion in conversation
- **THEN** the agent can save it through the simple workflow
- **AND** the response reports the saved path without requiring the user to pick
  a page type unless the type/scope is genuinely ambiguous
