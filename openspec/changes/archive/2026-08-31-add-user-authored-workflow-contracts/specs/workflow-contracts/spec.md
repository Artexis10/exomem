## ADDED Requirements

### Requirement: Contract families are code-owned and instances are user-authored

The system SHALL expose a deterministic contract-family registry whose family implementations are code-owned and versioned. A family SHALL define parsing, validation, resolution, rendering, and bounded projection for user-authored instances; vault content SHALL NOT register executable code, replace a family schema, or provide free-form instructions for execution. The first registered family SHALL be `workflow`.

#### Scenario: Vault file cannot invent executable semantics
- **WHEN** a contract file declares an unknown family, unknown schema version, executable field, or free-form instruction field
- **THEN** validation refuses the contract and no text from the file is executed or projected as an imperative instruction

#### Scenario: A future family remains an explicit product change
- **WHEN** a vault contains a directory for a family the package has not registered
- **THEN** inventory reports it as unsupported and the family gains no behaviour until code registers and tests it

### Requirement: Workflow contracts are human-owned structured Markdown

Each workflow contract SHALL be one ordinary UTF-8 Markdown file under exact `<configured kb_dirname>/_Schema/contracts/workflow/` path segments, where the existing `kb_dirname()` authority supplies the first segment. YAML frontmatter SHALL be the sole canonical value source and SHALL contain the exact v1 shape defined below. Filename and title SHALL be presentation rather than identity. New saves SHALL use the existing portable natural-title filename sanitizer and SHALL refuse collisions with another identity; safe manual renames SHALL remain valid. A delimited body block SHALL deterministically explain the canonical values in plain English, SHALL identify itself as derived, and SHALL never be read back as data. Direct edits SHALL remain inspectable; presentation drift SHALL be reported and guardably refreshable without changing frontmatter or authored Markdown outside the managed block.

#### Scenario: Contract is useful in Obsidian without an agent
- **WHEN** a person opens a companion-mode contract in an ordinary Markdown editor
- **THEN** the YAML shows its exact structured policy and the body explains the matching scope, Planning ownership, companion ownership, and Records loop in ordinary language

#### Scenario: Rename preserves contract identity
- **WHEN** a person renames the contract title or Markdown file without changing `contract_id`
- **THEN** the canonical identity remains stable and inventory reports the new presentation

#### Scenario: Body edit cannot change behaviour
- **WHEN** a person edits only the generated English block
- **THEN** resolution remains based on frontmatter, inspection reports presentation drift, and refresh can restore the derived block

#### Scenario: Refresh preserves authored rationale
- **WHEN** a person adds Markdown outside the managed presentation block and then refreshes the presentation
- **THEN** the authored Markdown is preserved byte-for-byte and only the managed block changes

#### Scenario: Configured KB name remains authoritative
- **WHEN** the configured KB directory is `Brain`
- **THEN** workflow contracts are read and written only below `Brain/_Schema/contracts/workflow/` and no `Knowledge Base/` tree is created

### Requirement: Workflow schema supports standalone and open companion declarations

A workflow contract SHALL contain exactly, in canonical order, `type`, `contract_id`, `schema_version`, `key`, `title`, `lifecycle`, `scope`, `planning`, `companions`, `capture`, and `planning_transition`. `type` SHALL be `workflow-contract`; `schema_version` SHALL be integer `1`; `contract_id` SHALL be lowercase canonical RFC 4122 UUID text; `lifecycle` SHALL be `active` or `archived`. `scope` SHALL contain exactly `projects`, `domains`, and `activities`, each a unique list of zero to 16 sorted tokens. Populated dimensions SHALL be ANDed and values within one dimension SHALL be ORed. `planning` SHALL contain exactly `mode`, whose value is `standalone` or `companion`. `companions` SHALL be empty in standalone mode and SHALL contain one to eight exact `{key, name, owns}` mappings sorted by key in companion mode; keys SHALL be unique, each `owns` SHALL contain one to 16 unique sorted ownership tokens, and ownership tokens SHALL be unique across the entire contract. Co-ownership is not represented in v1. `capture` SHALL contain exactly `durable_intent` and `observed_outcomes`, each `explicit` or `proactive`; proactive SHALL remain capped by active prominence. `planning_transition` SHALL be `explicit-only` or `propose-after-outcome`.

Project selectors SHALL reuse the canonical project-key grammar `^[a-z][a-z0-9-]{0,40}$`. Contract keys, companion keys, domain selectors, and activity selectors SHALL be 1–64 ASCII bytes matching `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`. Ownership tokens SHALL be 3–128 ASCII bytes matching `^[a-z][a-z0-9_-]{0,31}(?:\.[a-z][a-z0-9_-]{0,31})+$`. Titles and companion names SHALL be NFKC-normalized, trimmed, control-free strings of 1–128 UTF-8 bytes. A file SHALL be at most 64 KiB. An exact inventory scan SHALL stop at 512 files or 8 MiB and refuse `WORKFLOW_CONTRACT_SCAN_LIMIT` without a total or partial candidate set if either bound is exceeded. A completed scan SHALL return at most 128 released summaries plus the exact released total and a projection-truncation flag. Explanations SHALL be at most 4 KiB; findings and ambiguity candidates SHALL be capped at 32 and 16. Unknown, missing, unsorted, duplicate, unnormalized, or over-limit values SHALL be invalid rather than silently repaired during reads.

Canonical writing SHALL use UTF-8 LF text, YAML `---` delimiters, the field order above, two-space indentation, and the package's pinned safe-dump settings `sort_keys=False`, `allow_unicode=True`, `default_flow_style=False`, and `width=4096`. Semantic fingerprint SHALL be SHA-256 over compact sorted-key JSON of the normalized semantic mapping and SHALL exclude path, filename, authored body, generated presentation, and exact source bytes; source hash SHALL separately cover exact file bytes. Tool keys and ownership tokens SHALL remain open vocabularies. A declaration SHALL NOT assert that the companion is installed, authenticated, reachable, or capable of any operation.

#### Scenario: Standalone is complete without an integration
- **WHEN** a valid standalone contract resolves for a life, creative, research, or software scope
- **THEN** Exomem Planning may hold the complete durable outcome-to-work-item hierarchy and no companion is required

#### Scenario: Any companion can declare bounded ownership
- **WHEN** a user authors a companion with a syntactically valid new tool key and namespaced ownership tokens
- **THEN** validation accepts the declaration without a tool-specific adapter and resolution returns the tokens as authored data

#### Scenario: Companion declaration is not capability discovery
- **WHEN** a contract names a tool unavailable on the active client
- **THEN** the resolved decision reports the declared companion separately from active capabilities and never claims the tool can be called

#### Scenario: Conflicting companion ownership refuses
- **WHEN** two companions in one contract both list `software.requirements`
- **THEN** validation refuses the contract because v1 requires one declared owner per ownership token

### Requirement: Code-owned invariants outrank every workflow contract

Every resolved workflow decision SHALL preserve the code-owned boundaries that Planning represents intended future state, Records represents observed state and event history, governance and egress policy control disclosure, and external references are opaque. A workflow contract SHALL NOT authorize access, widen governance, create a collection, mutate an external system, assert external state, infer completion, or permit elapsed time or a Record to change Planning automatically.

#### Scenario: Contract cannot override governance
- **WHEN** a companion contract matches a scope containing withheld material
- **THEN** the unreleased contract is physically absent from that caller's inventory and resolution and causes no distinct result, refusal, title, path, count, excerpt, or reference

#### Scenario: External completion does not silently close a plan
- **WHEN** a companion pointer or Record appears to indicate completed work without explicit user transition intent
- **THEN** Planning remains unchanged and the configured posture may only propose review or transition

### Requirement: Workflow resolution is deterministic and provenance-bearing

The resolver SHALL accept an exact `context` mapping allowing only `project`, `domain`, and `activity`. A missing key SHALL mean unknown, explicit null SHALL mean known absent, and a valid token SHALL mean known value. It SHALL accept at most one explicit saved-contract selection or reviewed ephemeral proposal. Resolution precedence SHALL be explicit selection/proposal, then the unique active scoped contract matching the greatest number of selector dimensions, then the unique active empty-scope default, then the immutable built-in standalone decision. File order, timestamps, title similarity, retrieval ranking, embeddings, and model judgment SHALL NOT participate.

A scoped candidate SHALL be ruled out only by a known-absent or unequal known value. If any otherwise viable active scoped candidate depends on an unknown dimension, non-explicit resolution SHALL refuse `WORKFLOW_CONTRACT_CONTEXT_INCOMPLETE` rather than choose a broader/default contract. Explicit saved or reviewed ephemeral selection MAY resolve with partial context because the user supplied the authority directly.

Before non-explicit resolution, every workflow file SHALL be envelope-parsed. An unreadable/unparseable file, unsupported version, duplicate active ID/key, or incomplete scan SHALL refuse `WORKFLOW_CONTRACT_INVALID_INVENTORY` or `WORKFLOW_CONTRACT_SCAN_LIMIT` because applicability cannot be proven. Multiple active empty-scope defaults and equal-specificity winners SHALL refuse `WORKFLOW_CONTRACT_AMBIGUOUS`. Explicit lookup SHALL return `WORKFLOW_CONTRACT_NOT_FOUND`, `WORKFLOW_CONTRACT_INACTIVE`, `WORKFLOW_CONTRACT_DUPLICATE_IDENTITY`, or `WORKFLOW_CONTRACT_INVALID` for the corresponding absent, archived, duplicated, or invalid selection. None SHALL silently fall back.

The result SHALL carry normalized context, source class, canonical decision, contract identity when applicable, source path/hash, schema version, contract fingerprint, fixed-template English explanation, active-capability separation, and bounded warnings.

#### Scenario: Most specific unique scope wins
- **WHEN** a default, a domain contract, and a project-plus-activity contract match the supplied context
- **THEN** the project-plus-activity contract resolves with provenance naming its path, hash, identity, and specificity

#### Scenario: Empty vault is safely standalone
- **WHEN** no workflow contract directory or active contract exists
- **THEN** resolution succeeds with the immutable built-in standalone decision and does not create any file

#### Scenario: Equal winners refuse instead of merging
- **WHEN** two non-identical active contracts match at equal greatest specificity
- **THEN** resolution returns `WORKFLOW_CONTRACT_AMBIGUOUS` with bounded candidate identities and chooses neither contract

#### Scenario: Invalid match does not fall back silently
- **WHEN** the uniquely applicable contract is malformed or uses an unsupported schema version
- **THEN** workflow resolution reports the exact governed finding and does not silently apply a broader/default contract, while unrelated Exomem operations remain available

#### Scenario: Unknown activity cannot choose a broader authority
- **WHEN** project `alpha` is known, activity is unknown, and an otherwise viable active contract scopes project `alpha` plus activity `implementation`
- **THEN** resolution refuses `WORKFLOW_CONTRACT_CONTEXT_INCOMPLETE` and does not choose a standalone default

#### Scenario: Known-absent activity rules out an activity contract
- **WHEN** project `alpha` is known, activity is explicitly null, and a contract requires activity `implementation`
- **THEN** that contract is ruled out and ordinary precedence continues among the remaining valid candidates

#### Scenario: Unparseable file fails closed
- **WHEN** any workflow file cannot expose a trustworthy lifecycle and scope envelope
- **THEN** non-explicit resolution refuses `WORKFLOW_CONTRACT_INVALID_INVENTORY` and inventory remains available to report only authorized repair findings

#### Scenario: Explicit archived selection refuses
- **WHEN** an explicit key uniquely identifies an archived contract
- **THEN** resolution refuses `WORKFLOW_CONTRACT_INACTIVE` rather than applying it or falling back

### Requirement: Explicit session choices are ephemeral until saved

An explicit current-session choice MAY select a saved contract, the built-in standalone decision, or a reviewed valid proposal. A proposal resolution SHALL be fingerprinted and marked ephemeral and SHALL NOT write canonical state. Persisting it SHALL require the ordinary reviewed preview/save operation with a reason and current stale-write guard.

#### Scenario: One session uses a different companion
- **WHEN** a user explicitly requests a valid reviewed companion proposal for the current work only
- **THEN** resolution applies it with `source: ephemeral`, writes no contract file, and reports that the choice is not persistent

#### Scenario: Persistent wording uses the save path
- **WHEN** the user asks to make the reviewed choice the default for future matching work
- **THEN** the agent previews and saves a canonical contract rather than treating the session result as durable policy

### Requirement: Workflow contract writes are reviewed, guarded, and auditable

The schema/configuration surface SHALL support read-only inventory, inspection, validation, resolution, and save preview plus explicit guarded create/update and presentation refresh. A save SHALL require a complete reviewed proposal and non-empty reason. Updating an existing contract SHALL require its current content hash and SHALL preserve the existing `contract_id`; create SHALL refuse duplicate ID or logical key. Validation and preview SHALL write nothing. Every committed mutation SHALL use the ordinary mutation envelope, audit log, and graph/index reconciliation posture for `_Schema` writes.

#### Scenario: Preview is exact and read-only
- **WHEN** a reviewed proposal is previewed
- **THEN** the result contains exact canonical frontmatter, target path, generated English block, and fingerprint without creating or modifying a file

#### Scenario: Direct edit makes an update stale
- **WHEN** a person changes a contract after an agent previews an update
- **THEN** guarded save refuses the old hash and preserves the person's edit

### Requirement: Contract projection is bounded and non-executable

Every runtime projection SHALL represent contract values as typed fields plus fixed-template explanatory text. User-authored display strings SHALL be bounded and quoted as data; they SHALL NOT be concatenated into an executable system prompt or allowed to replace code-owned instructions. A profile exporting `schema_memory` SHALL carry the invariant kernel, built-in fallback, at most one released default summary, at most eight released scoped summaries in key order, exact released-total/projection-truncation metadata after a complete scan, and the resolver route. A profile omitting that command SHALL carry the invariants plus `resolution_available: false`, SHALL advertise no route, and SHALL report built-in standalone only for an empty released inventory with no migration requirement; otherwise it SHALL report fixed `workflow_resolution_unavailable` and disable contract-aware proactive routing.

The product route SHALL register a workflow-specific default-deny egress path rather than rely on `schema_memory`'s metadata-only exemption. It SHALL filter files through the caller's release authority before inventory or resolution. An unreleased contract SHALL be physically absent for that caller and SHALL change neither result nor refusal. Counts, ambiguity sets, scan totals, summaries, and winners SHALL be computed only over the released set. Released winners MAY carry released identity, path, and source hash.

#### Scenario: Adversarial title remains display data
- **WHEN** a contract title contains instruction-like text within the permitted display-string grammar
- **THEN** the machine decision remains unchanged and the renderer quotes the title as data rather than emitting it as an instruction

#### Scenario: Large personalized vault keeps compact bootstrap bounded
- **WHEN** a vault contains more active workflow contracts than the compact cap
- **THEN** bootstrap returns the bounded prefix plus exact total/truncation metadata and directs the agent to inventory/resolve for detail

#### Scenario: Hidden candidate does not become an existence oracle
- **WHEN** an unreleased contract would win or tie if it were visible
- **THEN** the caller receives exactly the result computed from released contracts, with no distinct hidden-state refusal or metadata

#### Scenario: Released count does not reveal hidden files
- **WHEN** inventory contains both released and unreleased contracts
- **THEN** inventory and bootstrap totals count only released contracts and contain no hidden-count delta

### Requirement: Legacy external-execution behaviour migrates explicitly

Existing Planning and Records representations SHALL require no rewrite, and existing execution kinds SHALL remain valid under the open token syntax. The behavioural default SHALL migrate through product-owned `<kb_dirname>/_Schema/workflow-contract-migration.yaml` with exact `{schema_version: 1, review_required: <bool>}`. Before the first feature-aware scaffold refresh writes any managed file, it SHALL atomically create an absent marker with `review_required: true` when any managed scaffold sentinel existed at call entry, otherwise false for a fresh vault. It SHALL preserve an existing valid marker. A missing marker beside existing sentinels SHALL be treated as required. An unreadable, unsafe, or invalid marker SHALL refuse `WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE` and SHALL NOT be overwritten.

When the marker requires review and no active released workflow contract exists, bootstrap and inventory SHALL report `workflow_contract_migration_required`, and non-explicit fallback SHALL refuse `WORKFLOW_CONTRACT_MIGRATION_REQUIRED`. Explicit resolve selection `name: "@standalone"` SHALL unblock only that call/session; the reserved value is outside the saved-key grammar. A reviewed saved active standalone or companion contract SHALL durably satisfy the condition. A fresh marker with review false SHALL permit zero-config built-in standalone.

#### Scenario: Existing vault does not silently become standalone
- **WHEN** feature-aware scaffold refresh begins on a vault that already has a managed scaffold sentinel and no migration marker
- **THEN** it durably writes the review-required marker before replacing scaffold content, and ordinary resolution refuses until the user makes an explicit workflow choice

#### Scenario: New vault keeps the safe zero-config default
- **WHEN** feature-aware initialization starts with no managed scaffold sentinel
- **THEN** it durably writes review false and resolution returns built-in standalone without requiring a user choice

#### Scenario: Invalid migration marker fails closed
- **WHEN** the migration marker is unreadable, unsafe, or not exact schema v1
- **THEN** resolution refuses `WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE` and initialization preserves the marker for repair
