## ADDED Requirements

### Requirement: Shared Semantic Contract Boundary
One pure semantic-contract evaluator SHALL accept parsed before/after state plus operation, lifecycle, registry, resolved contracts, and review state. In-process mutation paths SHALL invoke it in `precommit` mode and block on applicable errors; watcher/reconcile SHALL invoke it in `posthoc` mode, preserve externally authored Markdown, and surface drift. Every post-commit index consumer SHALL use the same parsed result. No surface or writer SHALL implement an independent weaker parser or validator.

#### Scenario: Equivalent writes receive equivalent findings
- **WHEN** the same resulting Markdown is proposed through two different in-process write paths
- **THEN** both receive the same semantic-unit, category, relation, and schema findings

#### Scenario: In-process error prevents mutation
- **WHEN** shared contract evaluation returns an error for an in-process write
- **THEN** no target Markdown or derived index is mutated

#### Scenario: Posthoc error preserves external Markdown
- **WHEN** the watcher evaluates an externally edited file with contract errors
- **THEN** it records the same error keys without reverting, deleting, or rewriting the file

### Requirement: Contract Applicability Is Lifecycle-Specific
Active governed compiled create/replacement, active Tier-2 create/overwrite/append under compiled-memory paths, and active adoption compile SHALL receive full precommit syntax, saved-schema, and relation-disposition enforcement. An inactive governed compiled create SHALL receive structural and saved-schema precommit only, SHALL NOT join the active relation corpus, and SHALL NOT receive reviewed-none or bootstrap state. Activation of that page SHALL re-enter the full current contract before the primary update. `draft`, `planned`, `dropped`, and `archived` SHALL be inactive; `planned` is the production-log outline phase. Replacements SHALL produce active successors. Governed compiled edit/observe SHALL receive precommit evaluation with grandfathering where applicable. Other governed-KB Tier-2 documents SHALL receive structural/safety checks only. Move SHALL reevaluate only rules affected by path/project/page-type/scope changes and SHALL refresh identity/index state. Delete/trash SHALL skip departing-content validation while applying existing inbound/lifecycle guards and derived cleanup. Sources/Evidence SHALL never receive mutation-time semantic enforcement, though read-only parsing/indexing MAY expose valid units as raw-parent observations. Watcher/reconcile SHALL be posthoc and nonblocking.

#### Scenario: Inactive draft defers relation disposition
- **WHEN** a newly created compiled page has an inactive lifecycle status
- **THEN** structural and saved-schema precommit run without reviewed-none or bootstrap state
- **AND** a later transition to an active status requires the full current relation disposition before the active primary update

#### Scenario: Planned production log is inactive
- **WHEN** a production log is created in its `planned` outline phase
- **THEN** it is excluded from the active compiled corpus until it transitions to `recorded`, `edited`, `published`, or `reflected`

#### Scenario: Source observations remain read-only evidence
- **WHEN** a Source contains valid compact observation syntax
- **THEN** read-only parsing may expose it with raw-parent/provenance status but `observe_memory` and compiled-page enforcement do not mutate it

#### Scenario: Delete does not deadlock on departing debt
- **WHEN** a governed page with schema debt is deleted or trashed through an allowed lifecycle path
- **THEN** content validation does not block departure, while inbound guards and complete index cleanup still apply

### Requirement: Current Relation Review Disposition
Every newly created active governed compiled page SHALL have a current relation-review disposition satisfied by a qualifying typed inbound/outbound relation, an explicit reviewed-none decision bound to current page identity/content, or an automatic no-candidates bootstrap disposition only for a genuinely empty governed corpus. A relation qualifies only when it is authored or explicitly reviewer-accepted, its target resolves unambiguously to an eligible governed page at evaluation time, its canonical registry entry is active and scope-valid, its registry family is not `link`, `citation`, `derivation`, `evidence`, `mention`, `observation`, or `provenance`, and either (a) its origin is `markdown_relation`, `semantic_relation`, or `semantic_block`, or (b) its origin is `frontmatter` and its registered family is exactly `supersession`. The sole inactive-target exception is a canonical supersession edge to the exact governed predecessor now marked `superseded` whose `superseded_by` resolves back to the active successor. Generic wikilinks, unresolved/ambiguous forward targets, `links_to`, and provenance links MUST NOT satisfy the disposition.

#### Scenario: Superseded predecessor remains a qualifying target
- **WHEN** an active successor canonically supersedes a governed predecessor and the predecessor is marked `superseded` with an exact backlink to that successor
- **THEN** the supersession edge remains qualifying after commit and during exact prepared recovery
- **AND** no other inactive target receives this exception

#### Scenario: Typed relation satisfies disposition
- **WHEN** a new compiled page has a registered canonical relation to an existing page
- **THEN** its relation disposition is satisfied and reports the typed edge

#### Scenario: Reviewed none is fingerprint-bound
- **WHEN** a reviewer records that a page has no qualifying relation candidates
- **THEN** the disposition is satisfied only while the page identity and relevant content fingerprint remain current
- **AND** a material change resurfaces relation review

#### Scenario: Empty vault bootstraps without fake edge
- **WHEN** the first compiled page is created in a genuinely empty governed corpus and no target can exist
- **THEN** the writer records an automatic bootstrap disposition without fabricating a relation or placeholder

#### Scenario: Bootstrap exception expires when a candidate can exist
- **WHEN** a second eligible compiled page is added after the first page received an automatic bootstrap disposition
- **THEN** the first page's bootstrap disposition becomes stale and enters ordinary relation review

#### Scenario: Generic links do not satisfy typed relation review
- **WHEN** a page contains only inline wikilinks, `links_to`, or source/evidence provenance
- **THEN** those connections are measured separately and the typed relation disposition remains unsatisfied

#### Scenario: Inactive or excluded-family relation does not qualify
- **WHEN** a page has only an inactive relation or a relation in the `citation`, `evidence`, or `link` family
- **THEN** the edge remains visible in its own semantics and relation review remains unsatisfied

#### Scenario: Unresolved forward target does not qualify
- **WHEN** a registered authored relation points to a missing or ambiguous target
- **THEN** the relation remains visible with its resolution finding but does not satisfy relation review

#### Scenario: Supersession frontmatter qualifies narrowly
- **WHEN** a registered active scope-valid supersession edge is authored through the supported frontmatter lifecycle field
- **THEN** it qualifies without making other frontmatter links relation-review eligible

### Requirement: Reviewed-None Creation Is Logically Atomic, Crash-Recoverable, And Content-Bound
Creation-capable governed writers SHALL support `validate_only=true`. Validation SHALL preassign a candidate page UUID and return `draft_id`, `draft_hash`, a bounded opaque `draft_token` for server-derived render inputs and ordered project auto-registration intent, deterministic relation findings/candidates, and no filesystem mutation. A reviewed-none commit SHALL repeat identical proposed content and supply the unused `draft_id`, echoed `draft_token`, `relation_disposition="reviewed_none"`, matching `relation_review_hash`, and a non-empty `relation_review_reason`. The writer SHALL recalculate the hash over candidate identity plus normalized final content/destination and reject mismatch/reuse. A bounded portable creation receipt SHALL be prepared for every full active-compiled commit before its deterministic auxiliary writes, and the primary Markdown SHALL be replaced last as the logical commit marker, so a visible page never lacks required review state. Reviewed-none/bootstrap receipts SHALL also carry their disposition; a qualifying-relation receipt SHALL be transaction/recovery state only. Portable state SHALL live in governed Markdown/frontmatter or a vault-managed governed artifact in the same creation batch, not solely in a derived database, and SHALL survive index rebuild or vault transfer. Exact recovery SHALL reconstruct the same ordered auxiliary target/content digest, include already-applied expected targets unchanged, bind every existing leaf to its exact safely read content, and reject unrelated drift rather than overwrite it. An exact existing qualifying primary without an artifact SHALL remain an idempotent already-committed case after upgrade, while a page-less no-artifact attempt has no retroactive prepared-recovery guarantee. Normal return and handled exceptions SHALL preserve the existing batch atomicity/rollback contract. Abrupt process death MAY leave page-less prepared state or temporary/backup residue; an exact unchanged draft with the same reviewed disposition/reason and auxiliary content SHALL be able to resume that prepared commit, while any non-exact reuse SHALL remain reserved for explicit audit/cleanup. This protocol SHALL NOT claim cross-file all-or-none power-loss atomicity. A qualifying typed relation or genuinely empty-corpus bootstrap SHALL not require a reviewed-none decision from the caller.

#### Scenario: Second disconnected page can be created after review
- **WHEN** validation for a second page returns no qualifying candidate and the commit repeats the draft with matching reviewed-none fields
- **THEN** portable review state is prepared before the page commit marker, and the returned visible page has a current disposition

#### Scenario: Interrupted prepared commit resumes exactly
- **WHEN** a process stops after portable review state or auxiliaries are replaced but before the primary page commit marker, and the caller retries the exact unchanged draft, reviewed disposition/reason, and auxiliary content
- **THEN** the writer reuses the matching prepared state, completes the primary page last, and rejects any non-exact draft from reusing that identity

#### Scenario: Changed draft cannot reuse review
- **WHEN** content, identity, or relation-relevant metadata differs after validation
- **THEN** the reviewed-none commit fails before mutation and requires a fresh validation hash

#### Scenario: Validation does not reserve a conflicting file
- **WHEN** `validate_only=true` returns a draft identity
- **THEN** no Markdown is created, and commit rejects the draft if that identity or destination became occupied

#### Scenario: Review disposition survives derived-state loss
- **WHEN** all rebuildable indexes/databases are removed and the vault is rebuilt
- **THEN** a still-current portable reviewed-none disposition is recovered from governed vault content

### Requirement: Canonical Relation Authoring And Validation
New note-level typed relations SHALL be authored as one registered lower-snake-case relation and one wikilink per bullet under `## Relations`. An empty section SHALL be structurally valid only when another current disposition satisfies relation review. Placeholder bullets such as `- (none yet)`, malformed bullets, and unregistered semantically inert labels MUST NOT satisfy the disposition.

#### Scenario: Placeholder relation is rejected
- **WHEN** a write proposes `## Relations` containing `- (none yet)`
- **THEN** the contract returns a malformed-relation error and no typed edge

#### Scenario: Empty section with reviewed-none is valid
- **WHEN** a page has an empty canonical Relations section and a current reviewed-none disposition
- **THEN** relation structure and disposition both pass

### Requirement: Existing Corpus Is Grandfathered Without Silent Worsening
Existing compiled pages lacking the new relation/category contract SHALL remain readable and editable under a migration warning policy and SHALL enter activation/review queues instead of being bulk rewritten. Each error finding SHALL have stable key `(code, governed_element_identity, resolved_rule)`. An in-process edit MAY preserve pre-existing error debt only when its after-error key set is a subset of its before-error key set and it does not invalidate a current accepted disposition. Any new error key SHALL block. Replacements SHALL be treated as new compiled conclusions.

On first capability activation, the system SHALL create one portable vault-managed activation manifest that records the current contract version and identities/source hashes of pre-existing governed compiled pages without rewriting those pages. Stable memory IDs SHALL be preferred; legacy pages SHALL use an explicitly move-unstable path+source-hash fallback. Pages first seen after that activation boundary SHALL be new pages, including out-of-band creations, and SHALL receive current-contract posthoc findings when they bypass an in-process writer.

#### Scenario: Legacy debt does not block unrelated safe edit
- **WHEN** an existing grandfathered page lacks typed relations and receives an unrelated edit that preserves its prior semantic state
- **THEN** the write succeeds with migration/review warnings

#### Scenario: Edit cannot introduce malformed observation
- **WHEN** an edit to a grandfathered page introduces malformed observation syntax
- **THEN** the in-process edit is rejected before mutation

#### Scenario: Mechanical worsening is set-based
- **WHEN** a grandfathered edit retains all old error keys but adds one new schema error key
- **THEN** the edit is rejected even if the total error count is unchanged

#### Scenario: Repair may reduce legacy debt incrementally
- **WHEN** a grandfathered edit removes one old error and adds none
- **THEN** the edit may proceed with warnings for the remaining before-existing errors

#### Scenario: Existing-vault activation is portable and nonrewriting
- **WHEN** the capability first opens a non-empty vault
- **THEN** it records existing-page baselines in one governed activation manifest, does not edit the pages, and consistently distinguishes later creations after rebuild/transfer

#### Scenario: Empty vault has no grandfathered page
- **WHEN** activation occurs with no governed compiled pages
- **THEN** the manifest records an empty baseline and the first later page uses the narrow bootstrap disposition

#### Scenario: Replacement follows current contract
- **WHEN** a legacy page is superseded by a newly written compiled conclusion
- **THEN** the successor must satisfy the current semantic and relation contract

### Requirement: Saved Schema Resolution And Writer Modes
Saved memory contracts SHALL cover frontmatter fields, rich kinds, categories, and typed relations and SHALL declare `validation: off|warn|strict`, default `warn`. Resolution SHALL consider all project keys on a page and operate per governed rule. Highest specificity wins in order: exact project+page type, project, page type, global. At equal highest specificity, identical rules collapse and compatible set constraints apply conjunctively; incompatible scalar values, unequal validation modes, or an empty allowed-set intersection SHALL produce an explicit named conflict before an in-process write.

#### Scenario: Warn mode returns findings and writes
- **WHEN** a proposed write violates a resolved warn-mode contract
- **THEN** the write succeeds and returns span/path-addressed findings

#### Scenario: Strict mode blocks an in-process writer
- **WHEN** a proposed in-process create, edit, replace, or observe operation violates a resolved strict contract
- **THEN** the operation fails before filesystem mutation with shared structured errors

#### Scenario: Conflicting specific contracts fail closed
- **WHEN** two incompatible contracts resolve at equal highest specificity
- **THEN** an in-process strict write fails with the conflicting contract names and remediation

#### Scenario: Multiple project contracts resolve per rule
- **WHEN** a page belongs to two projects whose equal-specificity category constraints have a non-empty compatible intersection
- **THEN** both constraints apply conjunctively for that rule while unrelated lower-specificity rules remain available

### Requirement: Out-Of-Band Edits Are Preserved And Surfaced
Watcher and reconcile paths MUST NOT delete, revert, or rewrite externally authored Markdown solely because it violates the semantic contract. They SHALL index all valid parseable units, remove stale derived rows, and surface malformed syntax, strict-schema drift, stale relation disposition, and registry findings through audit/review.

#### Scenario: Direct invalid edit survives reconcile
- **WHEN** a user creates a strict-contract violation in an editor and reconcile runs
- **THEN** the file remains unchanged, valid units are indexed, invalid fragments are excluded, and actionable findings are surfaced

#### Scenario: Fix clears drift idempotently
- **WHEN** the user repairs the Markdown and reconcile runs again
- **THEN** stale findings and stale derived rows disappear and repeated reconcile is a no-op

### Requirement: Structured Semantic Unit Mutation
The system SHALL expose `observe_memory` with `add`, `update`, `remove`, and `validate` operations for writable compiled pages. It SHALL accept parent path/reference, category, content, optional kind/tags/context, expected parent hash, and current unit reference/fingerprint where required. Compact observation SHALL be the default authoring form. Typed `relations` SHALL be rejected unless an explicit governed non-observation kind selects rich form; remediation SHALL tell the caller to select rich form or author a canonical note-level relation.

#### Scenario: Add compact observation
- **WHEN** `observe_memory(operation="add")` receives a parent, category, and content
- **THEN** it writes one canonical compact observation under `## Observations`, returns its unit reference, and refreshes derived indexes

#### Scenario: Update uses drift guards
- **WHEN** an update supplies a stale parent hash or stale anonymous unit fingerprint
- **THEN** the operation fails without changing Markdown or indexes

#### Scenario: Validate performs no write
- **WHEN** `observe_memory(operation="validate")` receives a proposed unit
- **THEN** it returns normalized unit and contract findings without modifying the vault or sidecars

#### Scenario: Compact relation input is rejected clearly
- **WHEN** `observe_memory` receives `relations` without an explicit non-observation governed kind
- **THEN** it performs no write and returns remediation for rich-unit or note-level relation authoring

#### Scenario: Immutable trees refuse observation mutation
- **WHEN** `observe_memory` targets Sources, Evidence, a read-only/excluded subtree, or a path outside the governed KB
- **THEN** it refuses with the existing write-boundary error contract

### Requirement: Index Failure Does Not Destroy Committed Markdown
If Markdown commits successfully but a derived semantic-unit sidecar update fails, the operation SHALL preserve the committed Markdown, record deterministic index drift, return degraded/index-reconcile guidance, and allow reconcile to rebuild the missing records.

#### Scenario: Sidecar failure after write is recoverable
- **WHEN** a valid Markdown write commits and the semantic-unit index update then fails
- **THEN** the Markdown remains, the response reports index drift, and reconcile restores parity without content loss
