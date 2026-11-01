# adoption-studio Specification

## Purpose
Provide a durable, resumable, governed workflow for bringing an existing folder of material into the knowledge base: read-only scan, server-materialized selection, exact preview bound to plan identity, apply with write-time re-validation, and reviewable agent proposals. Originals are never modified, and interrupted, partial or stale runs are visible and recoverable.

## Requirements

### Requirement: Deterministic read-only scan and candidate inventory

`adoption_studio(action="start")` SHALL perform a read-only `overview` scan of the target subtree and record, in the run object, a compact `scan_summary`, `pack_suggestions`, and a candidate `inventory` containing every adoption-relevant file outside `Knowledge Base/` with its vault-relative path, byte size, mtime, and import form (`text` for a text-like suffix, `artifact` for any other regular file). `start` SHALL accept an optional `collection` label and SHALL default it to the scanned folder's name, or to the single top-level folder inside an `_Imports/<import_id>/` stage. A skipped hidden folder SHALL be recorded as one inventory row with its file count and reason. Ineligible candidates SHALL be retained in the inventory with a machine-readable `reason` (never silently dropped), the inventory SHALL be capped at a configurable `max_candidates` with an explicit `inventory_truncated` counter, and a stat-based `inventory_fingerprint` SHALL be computed over the sorted eligible rows. The scan SHALL write nothing outside the run object. `start` SHALL require `Knowledge Base/` to exist and refuse with `KB_NOT_INITIALIZED` unless `initialize_kb=true`, in which case it initializes the scaffold first; pure pre-init discovery remains `adopt_vault(mode="scan-only")`, unchanged.

#### Scenario: Scan snapshots an eligible-only inventory deterministically

- **WHEN** `start` runs twice against an unchanged subtree
- **THEN** both runs record the same eligible candidate rows in the same order and the same `inventory_fingerprint`
- **AND** ineligible files (for example already-governed paths or files over the artifact ceiling) appear in the inventory with a `reason` rather than being omitted
- **AND** no file outside the run object is created or modified

#### Scenario: Non-text files are eligible as artifacts

- **WHEN** `start` scans a folder holding Markdown notes, a PDF, a PNG image, an audio file and a video file
- **THEN** every one of them is an eligible inventory row
- **AND** the notes have import form `text` and the other files have import form `artifact`

#### Scenario: Start refuses an uninitialized vault unless asked to bootstrap

- **WHEN** `start` is called and `Knowledge Base/` does not exist
- **THEN** it is refused with `KB_NOT_INITIALIZED` and writes nothing
- **AND** calling `start` with `initialize_kb=true` first initializes the scaffold and then records the run

### Requirement: Durable resumable run object

An adoption run SHALL be a canonical-file-backed object stored under `Knowledge Base/_Adoption/runs/<run_id>/` as `run.json` (lifecycle, selection, plan, outcomes) and `proposals.json` (agent proposals), each written only through `vault.batch_atomic_write` so it inherits the access-tier backstop and writer-lease fence. The run SHALL be fully reconstructable after a process restart with no dependence on any rebuildable sidecar or in-memory state. `status` without a `run_id` SHALL list runs newest-first; `status` with a `run_id` SHALL return the full run document plus computed `staleness`, an `interrupted` flag, a `proposals_summary`, and product-shaped `next_actions`.

#### Scenario: A run survives a process restart

- **WHEN** a run is created, a selection is set, and the process restarts
- **THEN** `status(run_id=...)` returns the run in its persisted phase with its selection intact, reconstructed from the on-disk `run.json`

#### Scenario: Listing runs is read-only and newest-first

- **WHEN** `status` is called with no `run_id`
- **THEN** it returns compact rows (id, phase, created, counts) ordered newest-first
- **AND** no run object or vault file is modified

### Requirement: Server-materialized selection rules with per-path validation

`adoption_studio(action="select")` SHALL accept a folder-rule selection payload `{include, exclude, overrides, include_junk}` and materialize the concrete file set **server-side** so the client never enumerates thousands of files. An `include` folder rule MAY carry a `source_kind` and a `domain`; `select` SHALL resolve each through the vault's Source taxonomy registry, SHALL accept only a registered result, and SHALL refuse a rule whose key is unregistered or a near miss, with the resolver's close match, without changing the selection. Rule classification SHALL be part of the selection identity. Each materialized path SHALL record the classification of the deepest rule that selects it. Each materialized path SHALL be validated against the inventory and rejected per-path with a machine-readable code — `NOT_IN_INVENTORY`, `TOO_LARGE`, or `ALREADY_GOVERNED` — never silently dropped. Any selection change SHALL clear any existing plan and return the run to phase `selecting`. Inventory drill-in SHALL be available with offset paging so the UI can page bounded file lists.

#### Scenario: Folder rules materialize a validated file set

- **WHEN** `select` is called with `include` of a folder, an `exclude` of a subfolder, an `overrides` list, and `include_junk=false`
- **THEN** the server materializes the concrete selected paths from the depth-capped tree and records them
- **AND** any oversized, missing, or already-governed path is returned in a per-path rejection list with its code

#### Scenario: A deeper rule overrides its ancestor

- **WHEN** `select` is called with `exclude` of a folder and `include` of one of its subfolders (the UI's uncheck-parent, re-check-child gesture)
- **THEN** rules apply in ascending path depth so the subfolder's files stay selected while the rest of the excluded folder is dropped
- **AND** a file-path `exclude` is the deepest rule of all and wins over its folder's rule

#### Scenario: A folder rule classifies its files from the registry

- **WHEN** `select` includes one folder with a registered `source_kind` and `domain`, and another folder with no classification
- **THEN** the first folder's paths record that kind and domain, and the second folder's paths record no classification
- **AND** a rule naming an unregistered kind is refused with a close match, and the stored selection is unchanged

#### Scenario: Changing the selection invalidates the plan

- **WHEN** a run in phase `planned` receives a `select` call that changes the materialized set or a rule's classification
- **THEN** the stored plan is cleared and the phase returns to `selecting`

### Requirement: Exact-action preview bound by plan identity

`adoption_studio(action="plan")` SHALL read and sha256-hash each selected file at plan time, compute the exact deterministic target path, title, and frontmatter for each item, record any skipped items with a code, and persist a `plan` carrying a `plan_id` derived from the selection hash and the complete planned actions: each item's destination, classification, provenance fields, rendered link projection, and original hash. Destinations SHALL be allocated before links resolve and SHALL NOT change afterwards. A text item that is not valid UTF-8 SHALL plan as an artifact. A classified item's target SHALL be in its kind's Source directory (`Sources/<Kind>/<Domain>/`); an unclassified item's target SHALL be in `Sources/Imported/`. For a text item, `plan` SHALL read its wikilinks, embeds, and local Markdown links and images with the shared parsers, and SHALL resolve each target against the planned items and earlier imports by exact collection-relative path, then note-relative path, then unique basename, then unique front-matter alias, leaving an ambiguous or missing target unresolved, and SHALL record the resolved targets, the unresolved targets, the aliases it carries, its tags, and any tag the vault's tag normalization rejects. A text item's tags SHALL be its front-matter tags followed by the inline `#tags` of its body that the shared Markdown parser reads outside code, comments and escapes; a hash inside code SHALL NOT become a tag. `plan` SHALL check each item against earlier imports by `import_collection` and `collection_path` (falling back to `imported_from` for an import that lacks them), SHALL record `existing_ref` for an item whose original an earlier import holds with the same bytes, and SHALL record the latest earlier Source as `previous_import` for an item whose bytes changed. The plan SHALL report the total bytes that `apply` will write. When the remaining request budget falls below its margin, `plan` SHALL stop, return `progress` and a `continuation`, and continue on a call that passes that cursor. `plan` SHALL write nothing outside the run object (targets are previewed, not created). The preview a caller sees SHALL be exactly what a subsequent `apply` commits, or `apply` SHALL refuse.

#### Scenario: Plan previews exact targets without writing

- **WHEN** `plan` runs over a non-empty selection
- **THEN** each item records its `original_sha256`, exact `target_path`, `title`, and `frontmatter`, and a stable `plan_id` for identical inputs
- **AND** a byte snapshot of the vault before and after `plan` is identical outside the run object

#### Scenario: Replacement cannot redirect an original read

- **WHEN** a selected original or its parent is replaced with a link during planning or apply
- **THEN** retained no-follow custody prevents the read from following that link
- **AND** a changed binding produces a coded refusal before publication instead of importing bytes from the replacement

#### Scenario: Plan resolves links without guessing

- **WHEN** a planned note links to another planned note by path, embeds a planned image by basename, links to a third note through its front-matter alias, and links to a basename that two planned notes share
- **THEN** the first three targets are recorded as resolved to those items' frozen target paths
- **AND** the shared basename is recorded as unresolved

#### Scenario: Plan reads inline tags outside code

- **WHEN** a planned note carries a front-matter tag, an inline `#tag` in its body, and a hash inside an inline code span and inside a fenced code block
- **THEN** the item's tags are the front-matter tag followed by the inline tag
- **AND** neither hash inside code is a tag

#### Scenario: Plan recognizes an earlier import

- **WHEN** a collection that an earlier run imported is staged again under a new location with the same `collection` label and one note changed
- **THEN** every unchanged item records the `existing_ref` of its earlier Source
- **AND** the changed note plans as a new item that names its earlier Source as `previous_import`

#### Scenario: Apply refuses a stale plan

- **WHEN** `apply` is called with a `plan_id` that does not equal the stored plan's id, or the current selection hash no longer matches the plan's selection hash
- **THEN** it is refused with `PLAN_STALE` and writes nothing

### Requirement: Governed apply with write-time re-validation

`adoption_studio(action="apply")` SHALL process the planned items that have no outcome yet, in plan order, in chunks bounded by item count and bytes. For each item it SHALL re-read the original bytes and re-verify the sha256 against the value captured at `plan` immediately before writing. Before each write it SHALL repeat the collection identity check under its mutation guard. Each chunk SHALL commit its text pages, streamed artifacts, outcomes, Sources index, top index and log update in one `batch_atomic_write`. Index and log updates SHALL use the actual destinations. Artifact writes SHALL use the Source artifact seam shared with capture and SHALL keep the original bytes. An extraction request SHALL follow each artifact commit. An item whose bytes changed SHALL be excluded with per-item outcome `SOURCE_CHANGED`; a missing item SHALL be excluded with `NOT_FOUND`; an item whose frozen destination another writer now occupies SHALL fail with `TARGET_OCCUPIED` and SHALL NOT move; an item with `existing_ref`, or one that the identity check finds already committed, SHALL record `already-applied` with that ref and write nothing. Applied Sources SHALL carry `imported_from`, `import_collection`, `collection_path`, and `original_sha256` provenance. A text Source SHALL carry the original text verbatim in its fenced capture, so that the captured text encodes to bytes whose sha256 equals `original_sha256`, and its resolved links in a generated `## Links` section outside that fence. When the remaining request budget falls below its margin, `apply` SHALL stop between commits and return `progress` (`applied`, `remaining`, `total`) and a `continuation`; an `apply` passing that cursor with the same `plan_id` SHALL continue, and an `apply` repeating an earlier cursor SHALL return that call's result again. The phase SHALL become `applied` only when every planned item has an outcome. The result SHALL include `verified_unchanged` and `verified_total` counts from a post-apply re-hash of the originals handled by that call. A `plan`/`apply` invalidated by inventory drift SHALL surface `ADOPTION_SOURCE_CHANGED` so the client can re-scan while preserving the still-valid selection.

#### Scenario: Apply copies with provenance and leaves originals byte-identical

- **WHEN** `apply` commits a validated plan
- **THEN** each imported Source carries `imported_from`, `import_collection`, `collection_path`, and `original_sha256`, the Sources index and log are updated with each call, and every original file is byte-identical before and after
- **AND** the result reports `verified_unchanged` equal to `verified_total`

#### Scenario: Attachments and links land together

- **WHEN** `apply` commits a plan holding a note that embeds a planned PDF and links to another planned note
- **THEN** the PDF is a Source whose artifact bytes equal the original
- **AND** the note's Source holds the original text verbatim in its fenced capture and a `## Links` section with links to the PDF's Source and the other note's Source
- **AND** an extraction request exists for the PDF

#### Scenario: A large plan finishes over several calls

- **WHEN** `apply` reaches its request budget with items remaining
- **THEN** it returns `progress` with a nonzero `remaining` and a `continuation`
- **AND** calls that pass each returned cursor commit only the remaining items, each item once, until `remaining` is zero and the phase is `applied`
- **AND** repeating an earlier cursor returns that call's result again and writes nothing

#### Scenario: A source changed after plan is excluded, not fatal

- **WHEN** one selected original is modified between `plan` and `apply` and others are unchanged
- **THEN** the changed item records outcome `SOURCE_CHANGED`, the unchanged items are applied, and the run phase becomes `partial`

### Requirement: Per-item outcomes, partial results, and failed-subset retry

The run SHALL record a per-path outcome (`applied`, `failed`, `skipped`, or `already-applied`) with a code for failures and, for `already-applied`, the ref of the Source that holds the original. When some items apply and others fail the phase SHALL be `partial`; when none apply it SHALL be `failed`. `apply(retry_failed=true, only_paths=[...])` SHALL re-plan and re-apply only the failed subset in place with fresh hashes, and re-applying an already-applied item SHALL be an idempotent `already-applied` skip. A failed text batch SHALL roll back only the items of that call, and a commit whose state is unknown SHALL be resolved by the identity check on the next call. The UI-facing retry SHALL be expressible as `scope:"failed"`.

#### Scenario: Retry re-applies only the failed subset

- **WHEN** a run is `partial` and the previously failed original is restored, then `apply` is called with `retry_failed=true`
- **THEN** only the failed subset is re-planned and applied, previously applied items are left untouched, and the phase becomes `applied`

#### Scenario: Re-applying an applied item is idempotent

- **WHEN** `apply` is re-run over a plan whose items already have `applied` outcomes
- **THEN** those items report `already-applied` and no duplicate Source is created

#### Scenario: A second run over the same collection writes nothing

- **WHEN** a new run selects, plans and applies a collection that an earlier run imported, unchanged
- **THEN** every item reports `already-applied` with its earlier Source's ref
- **AND** no Source page or artifact is created

### Requirement: Interrupted apply is visible and recoverable

The run SHALL persist phase `applying` before the first item write so a process death mid-apply is visible. `status` SHALL report `interrupted: true` when the phase is `applying` with incomplete outcomes and no call returned a budget stop, and SHALL report the remaining count and the last `continuation` when the last call returned a budget stop. A subsequent `apply` SHALL be safely re-runnable because already-applied detection and fresh write-time re-validation make re-entry safe.

#### Scenario: A crashed apply is reported and can resume

- **WHEN** the run is left in phase `applying` with incomplete outcomes
- **THEN** `status` reports `interrupted: true`
- **AND** calling `apply` again completes the remaining validated items without duplicating applied ones

#### Scenario: A budget stop is remaining work, not a crash

- **WHEN** the last `apply` returned a budget stop with items remaining
- **THEN** `status` reports the remaining count and the cursor to continue with, and does not report `interrupted: true`

### Requirement: Cancellation rules preserve applied work

`adoption_studio(action="cancel")` SHALL be allowed from `selecting`, `planned`, `partial`, `failed`, and a budget-stopped `applying` run with an open continuation. It SHALL record `{at, why}` and set phase `cancelled`. It SHALL refuse `applying` without an open continuation with `CANCEL_DURING_APPLY`. It SHALL refuse `applied` and `done` with `ALREADY_APPLIED`. Cancellation SHALL preserve copied Sources and originals.

#### Scenario: Cancel closes a pre-apply run

- **WHEN** `cancel` is called on a run in `selecting` or `planned`
- **THEN** the phase becomes `cancelled` with the recorded reason and no vault Source is created or removed

#### Scenario: Cancel is refused mid-apply and post-apply

- **WHEN** `cancel` is called while the phase is `applying` without an open continuation
- **THEN** it is refused with `CANCEL_DURING_APPLY`
- **AND** calling `cancel` after the phase is `applied` is refused with `ALREADY_APPLIED` and applied Sources survive

#### Scenario: Cancel closes a budget-stopped run

- **WHEN** `cancel` is called after an apply budget stop with an open continuation
- **THEN** the phase becomes `cancelled` with the recorded reason
- **AND** copied Sources and every original remain unchanged

### Requirement: Three-layer staleness detection

The run SHALL detect drift at three layers: a stat-level `inventory_fingerprint` for cheap `status` staleness flags (`stale_paths`), a per-source sha256 captured at `plan` and re-verified per item at `apply` (`SOURCE_CHANGED`), and per-proposal content-hash `bindings` re-verified at `apply-proposal` alongside each governed op's own compare-and-swap. Identity-only refs SHALL NOT be used as staleness guards.

#### Scenario: Status flags a stale selection

- **WHEN** a selected original is touched after `plan`
- **THEN** `status` reports that path in `stale_paths`
- **AND** the underlying originals remain unmodified

### Requirement: Bounded read-only AdoptionWorkItem

`adoption_studio(action="work-item")` SHALL return a deterministic bounded context pack — measurements plus recorded content only, with zero judgment — carrying the run ref and fingerprint, an explicit `constraints` statement that the agent cannot write to the vault and may only submit structured proposals, per-source recorded titles/hashes and capped excerpts with explicit truncation, existing-KB adjacency via the same deterministic retrieval `compile_proposal` uses, and a per-kind proposal schema summary. It SHALL be read-only and honor explicit caps (`max_sources`, `max_chars_per_source`) reported in a `limits` block.

#### Scenario: The work item is bounded, honest, and read-only

- **WHEN** `work-item` is requested with more applied sources than `max_sources`
- **THEN** it returns at most `max_sources` sources with `limits` reporting `shown`, `total`, and `truncated`, and each over-length excerpt marked `excerpt_truncated`
- **AND** the `constraints` text is present and no vault file is written

### Requirement: Structured proposal lifecycle for five kinds

`adoption_studio(action="propose")` SHALL accept proposals of kind `compilation`, `entity`, `relation`, `supersession`, or `reconciliation`, each carrying a required `why`, a kind-specific `payload`, and `bindings` (`run_fingerprint` plus per-source content hashes). Submission SHALL write only `proposals.json`. Validation SHALL be two-phase: at `propose`, full structural, schema, and registry validation — invalid proposals persist with `status:"invalid"` and findings (auditable, never appliable) while valid ones persist as `proposed` with a ref and fingerprint; at `apply-proposal`, everything is re-validated live. A proposal SHALL be applied ONLY through the existing governed leaves (`remember` for compilation, `link` for entity, governed `edit` for relation, `replace_memory` for supersession, and the resolved sub-kind for reconciliation) — `adoption_proposals` SHALL never write Markdown directly.

#### Scenario: Propose validates each kind and never touches Markdown

- **WHEN** a batch containing an unknown `relation_type`, a bad `note_type`, and a valid compilation is proposed
- **THEN** the invalid proposals persist with `status:"invalid"` and findings, the valid one persists as `proposed` with ref and fingerprint, and a full-tree Markdown byte snapshot is unchanged apart from `proposals.json`

#### Scenario: Apply-proposal routes only through governed leaves

- **WHEN** a valid `compilation` proposal is applied with a matching `expected_fingerprint` and a `why`
- **THEN** the note is created through the `remember` leaf with its `sources`, and the log entry carries the approver's `why`

#### Scenario: Apply-proposal requires fingerprint and why and refuses drift

- **WHEN** `apply-proposal` is called without `expected_fingerprint` or without `why`, or a bound source's content hash changed since submission
- **THEN** it is refused (missing required inputs, or `REVIEW_ITEM_CHANGED` on a changed binding) and writes nothing

### Requirement: Proposals surface through the existing review verbs

Adoption proposals SHALL be namespaced as `exomem://review/adoption/<id>` and SHALL never resolve to or from attention, activation, or relation items. `review_memory(mode="adoption")` SHALL list open proposals grouped per run with effective state computed from the existing review-state store; `triage_memory` SHALL accept adoption refs for `dismiss`/`snooze`/`reopen` keyed by `review_id:fingerprint`; and `review_item_context` SHALL return a bounded adoption context pack including a live binding check. A dismissed proposal whose bound content later changes SHALL resurface because its fingerprint changed.

#### Scenario: Dismissed proposal resurfaces when its binding changes

- **WHEN** an adoption proposal is dismissed via `triage_memory` and later a bound source is edited so the fingerprint changes
- **THEN** the proposal is absent from `review_memory(mode="adoption")` while unchanged and reappears open after the change

#### Scenario: Adoption refs stay isolated from other review kinds

- **WHEN** an adoption ref is passed to `triage_memory`
- **THEN** it resolves only the matching adoption proposal and never an attention, activation, or relation item

### Requirement: First-question handoff on finish

`adoption_studio(action="finish")` SHALL be allowed from `applied` and `partial`, run a deterministic recall check (keyword `find` over the largest imported text title, or the largest artifact title when no text item exists, counting a hit only on the exact target path) producing `recall_check = {query, ok, hits}`, optionally write a run manifest under `Knowledge Base/_Adoption/`, and set phase `done`. When the recall check finds the target page, `finish` SHALL return a `first_question` referencing that title with a ready-to-run search route. When the recall check is pending or failed, or the search answered without the target page, `finish` SHALL NOT return a `first_question` or its route; it SHALL return `indexing` with the recall check's state (`pending`, `failed` or `missed`) and the recall sample's sampled, found, missed, pending and failed counts. A recall check that the search answered without the target page SHALL name a reconcile route. Every route that `finish` returns, and that a later `status` returns from the stored finish, SHALL name a tool and arguments that the surface serving that call lists. A stored route that the serving surface cannot spell SHALL be returned as stored, and SHALL NOT fail the call. `finish` SHALL also return `coverage`: outcome counts by status and code; skipped hidden folders and junk counted by reason with file counts; for artifact items, extraction states counted as done, pending, engine disabled, failed, or unknown; resolved and unresolved link counts; and a recall sample of up to ten applied items spread over the collection's top-level folders in a fixed order, counting a hit only on the exact target path, reported as found over sampled. A pending or unknown extraction SHALL never be reported as done. Open proposals SHALL NOT block finish; there is no engine timer or background thread. `status`/`finish` SHALL include a `handoff` block carrying `prompt_text` and deterministic `links`.

#### Scenario: Finish proves recall and offers the first question

- **WHEN** `finish` runs on a run with applied imports
- **THEN** `recall_check.ok` is true because an imported target appears in the keyword hits, `first_question` references the imported title with a search route that the serving surface lists, and the phase becomes `done`
- **AND** any run manifest is written only under `Knowledge Base/_Adoption/`

#### Scenario: Finish reports coverage honestly

- **WHEN** `finish` runs on a run whose applied items include an image waiting for extraction and a note with one unresolved link
- **THEN** `coverage` counts the image as pending and the link as unresolved
- **AND** the recall sample reports how many sampled items it found out of how many it sampled

#### Scenario: A finished run names the tools of the surface that reads it

- **WHEN** a run finishes on one surface with a recall check that the search answered without the target page, and `status` later reads the run on a surface that spells tools differently
- **THEN** each answer names the reconcile route the way the surface serving that call spells it

#### Scenario: Finish withholds the first question while the pages are not searchable

- **WHEN** `finish` runs while the search index has not caught up with the imported pages
- **THEN** it returns no `first_question` and no route
- **AND** it returns `indexing` with the state `pending` and the recall sample's counts, and the phase becomes `done`

### Requirement: Originals are never modified

Across every action and every failure mode, adoption SHALL never rewrite, move, or delete an original file. Imports are copies into `Knowledge Base/Sources/Imported/` or, for a classified item, into its kind's Source directory, carrying provenance; an artifact import copies the original bytes. Scans, selection, and preview write nothing outside the run object; cancellation, budget stops and partial/failed apply leave already-copied Sources intact and originals untouched.

#### Scenario: Originals are byte-identical after any outcome

- **WHEN** a run proceeds through scan, select, plan, a partially-failing apply, a budget stop, a retry, and a cancel attempt
- **THEN** every original file is byte-identical to its pre-run bytes at every step
- **AND** no original is moved or deleted by any action

### Requirement: Work items include semantic-unit context packs

`adoption_studio(action="work-item")` SHALL include, for each bound source it returns, the source's semantic unit context pack (the same pack surface `context-packs` defines), bounded by the work item's existing caps. Pack inclusion SHALL be read-only and deterministic; when a source has no units, the row SHALL say so explicitly rather than omitting the field.

#### Scenario: Bound sources carry their unit packs

- **WHEN** `work-item` is called on a run with applied sources that have semantic units
- **THEN** each source row includes a `semantic_units` pack (units, kinds, bounded excerpts) alongside the raw excerpt
- **AND** the response stays within the work item's server-clamped size bounds

#### Scenario: Sources without units are explicit

- **WHEN** a bound source has no semantic units
- **THEN** the source row carries an explicit empty pack marker, never a silently missing field

### Requirement: Proposals are contract-validated at submission

`adoption_studio(action="propose")` SHALL run the semantic write contract's creation validation for `compilation` and `supersession` payloads at submission time and record the outcome on the proposal as `contract_findings`. Blocking findings SHALL NOT invalidate the proposal by themselves (relation-review findings are resolvable at apply via the reviewed-none flow); non-review blockers SHALL mark the proposal `invalid` with the findings attached.

#### Scenario: Reviewable findings ride the proposal record

- **WHEN** an agent submits a compilation whose content passes validation except a missing relation disposition
- **THEN** the proposal is recorded `proposed` with `contract_findings` naming the relation-review requirement
- **AND** the review queue item carries those findings

#### Scenario: Non-review blockers invalidate at submission

- **WHEN** a submitted compilation has a blocking contract finding that no reviewed disposition can clear
- **THEN** the proposal is recorded `invalid` with the findings attached and can never be applied

### Requirement: Reviewed-none apply flow is the governed path

`adoption_studio(action="apply-proposal")` for compilation, supersession, and reconciliation-replace SHALL commit through the two-phase governed creation: validate to bind a draft, then commit with `relation_disposition="reviewed_none"`, `relation_review_hash` bound to the validated draft, and the approver's `why` as the relation review reason — whenever the contract requires an explicit relation review. Pages committed this way SHALL resurface through the relation-debt queue.

#### Scenario: Approval rationale rides the governed write

- **WHEN** a reviewer approves a compilation proposal whose content carries no typed relation
- **THEN** the created page records a reviewed-none disposition whose review reason is the approver's `why`
- **AND** the page appears in the relation-debt queue on its normal schedule

### Requirement: Studio surfaces contract findings before approval

The Studio proposal detail SHALL render the proposal's `contract_findings` and, when approval would record a reviewed-none disposition, state that consequence in plain language before the approve control. The Studio SHALL NOT enable approval while the proposal is `invalid`.

#### Scenario: Reviewer sees findings and the reviewed-none consequence

- **WHEN** a reviewer opens a proposal whose findings require a relation review
- **THEN** the detail shows the findings and states that approving records "reviewed, no typed relation yet" and the page will resurface for relation review
- **AND** the approve control is enabled

#### Scenario: Invalid proposals cannot be approved from the Studio

- **WHEN** a reviewer opens an `invalid` proposal
- **THEN** the findings are shown and the approve control is absent or disabled

### Requirement: Cloud staged imports adopt through the local engine

On Exomem Cloud, an `adopt` import SHALL land as RAW files under the vault-relative staging directory `_Imports/<import_id>/`, outside `Knowledge Base/` so the engine treats them as legacy input. The Cloud import job SHALL unpack and validate the archive before the folder appears there, with its member, path and size checks. `adoption_studio(action="start")` SHALL accept `path` pointing at that staging directory and scan it with the identical engine used locally. A local run SHALL scan a folder inside the vault in place, and SHALL stage only a folder outside the vault, through `exomem import`, into the same layout.

#### Scenario: A staged Cloud import adopts through the local engine

- **WHEN** an `adopt` import has finished and its files are in `_Imports/<import_id>/`
- **THEN** `adoption_studio(start, path="_Imports/<import_id>")` scans the staged files through the same engine as a local run
- **AND** applying the run imports them as Sources without modifying or replacing existing notes

#### Scenario: A folder inside the vault is not staged

- **WHEN** a local run starts on a folder inside the vault and outside `Knowledge Base/`
- **THEN** the engine scans it in place and creates no `_Imports/` copy

### Requirement: Own notes adopt as governed draft notes

A `select` folder rule SHALL accept `adopt_as` with the values `source` (the default) and `note`. A `note` rule SHALL name a `note_type` that the note-type registry holds as a compiled type, and MAY name the `project` or `domain` that the type's destination needs. `select` SHALL refuse an unregistered note type, or a type the note leaf cannot place, with a coded error and no selection change. The rule SHALL be part of the selection identity. The server SHALL NOT infer whether a file is an own note from its words or name.

For an own-note item, `plan` SHALL preview the Source copy exactly as for a `source` item and SHALL also preview a note target, title and front matter. The note body SHALL be the original note body after its front matter, unchanged, followed by the generated `## Links` section that a Source carries; its title and aliases SHALL come from the parsed front matter, its title is the front matter's `title` when it has one, and its tags SHALL be the item's planned tags: the front-matter tags followed by the body's inline `#tags` outside code. The note's `sources:` SHALL cite the item's Source copy, and the note SHALL carry the item's `import_collection` and `collection_path`. A link from an own note to another own-note item SHALL resolve to that item's planned note; any other resolved link SHALL resolve as it does for Sources. The note target SHALL be part of the planned actions hashed into `plan_id`.

`apply` SHALL commit each chunk's Sources as for `source` items, then create each own-note item's note with status `draft` through the governed two-phase note creation, recording the note's outcome in the run object with its commit. The server SHALL validate each note and bind it to its own draft token one at a time, and SHALL commit the notes of a chunk that pass in canonical batches with one Recent-activity bullet and one log entry per note. A call's first batch SHALL hold as many of the chunk's notes as fit in half the time that the call has left, priced at the time per made note that the run recorded, and at least one note; when the run has recorded no price, or recorded it more than five minutes before, the first batch SHALL hold one note. After that first batch, a call that has made no note SHALL create one note at a time. Once a call has made a note, a batch SHALL hold as many of the chunk's notes as fit in the time that the call has left, priced at the dearest time per made note that the call has seen, and the run SHALL record that price for its next call. A refused note SHALL NOT lower that price. A batch that refuses before it writes SHALL fall back to creating each of its notes alone, so that each item receives the outcome that it receives alone. Before each of those notes, except on a call's first note, the call SHALL check that one more note fits in the time that it has left at the price that sized the batch. A note that does not fit SHALL keep `source-applied` and SHALL be owed to the continuation; it SHALL NOT fail. A batch that is written SHALL NOT fall back: each of its notes SHALL be created, and a note whose own step after the write failed SHALL report that error, as it does when the note is created alone. An own-note item SHALL be complete only when both its Source and its note exist. An item whose Source exists and whose note is still owed SHALL report the outcome `source-applied`, and progress SHALL count it as remaining, not as applied. The identity check SHALL find a note that an earlier run, a retry or a crashed apply created for the same collection path and record it as `already-applied` instead of creating another. An occupied note target SHALL fail the item with `TARGET_OCCUPIED`. For an item whose original changed since an earlier import, `plan` SHALL record the earlier note as `previous_note`, and `apply` SHALL create only the new Source.

Own-note creation SHALL only create pages that did not exist; it SHALL NOT edit, move or delete an existing page. The plan confirmation that `apply` binds to `plan_id` SHALL be the only confirmation the import needs.

#### Scenario: A folder of own notes becomes draft notes with lossless Sources

- **WHEN** a collection's `Journal/` folder is selected with `adopt_as: note` and a registered `note_type`, and its `Exports/` folder is selected with no rule, and `apply` runs to completion
- **THEN** each journal file has a Source whose fenced capture hashes to its `original_sha256` and a draft note whose body is the original body and whose `sources:` cites that Source
- **AND** each export file has a Source and no note
- **AND** every original is byte-identical

#### Scenario: Links between own notes point at notes

- **WHEN** an own note links to a second own note and embeds an image from the collection
- **THEN** the first note's link resolves to the second item's note and the embed resolves to the image's Source

#### Scenario: A re-run creates no second note

- **WHEN** the same collection is staged again unchanged and planned and applied
- **THEN** every own-note item records `already-applied` with its earlier Source and note, and no page is created

#### Scenario: An unregistered note type is refused

- **WHEN** `select` receives a `note` rule whose `note_type` the registry does not hold
- **THEN** it refuses with a coded error and the selection is unchanged

#### Scenario: A budget stop between notes resumes

- **WHEN** `apply` reaches its request budget after committing a chunk's Sources and some of its notes
- **THEN** it returns a continuation, and the next call creates only the remaining notes

#### Scenario: A chunk of own notes commits as one batch

- **WHEN** `apply` creates the notes of a chunk whose Sources are committed, no earlier call of the run has made a note, and the call has room for every note
- **THEN** the call times its first note alone, the other notes commit as one canonical batch, and each note has one Recent-activity bullet and one log entry
- **AND** a note whose target another writer took fails with `TARGET_OCCUPIED` and the other notes commit

#### Scenario: A continuation prices its first batch on the call before

- **WHEN** a call made and timed notes and returned a continuation, and each note costs a fixed time
- **THEN** the next call's first batch holds as many notes as fit in half its time at the recorded price, more than one
- **AND** no call runs past its budget less its delivery reserve

#### Scenario: A continuation whose notes cost more stays inside its budget

- **WHEN** a call made and timed notes, and each note of the next call costs half as much again
- **THEN** no call runs past its budget less its delivery reserve

#### Scenario: A continuation after a long pause times a note again

- **WHEN** the run recorded its price more than five minutes before the next call
- **THEN** that call's first batch holds one note

#### Scenario: A quick refusal does not price the next batch

- **WHEN** the run has recorded no price, a call's first note is refused at once, and each made note costs a fixed time
- **THEN** the call makes its next note alone before it commits a batch
- **AND** no call runs past its budget less its delivery reserve

#### Scenario: A refused batch falls back to one commit per note

- **WHEN** a chunk's batch refuses before it writes
- **THEN** the notes of the batch are created alone and every item that the fallback reaches receives the outcome that it receives alone

#### Scenario: A refused batch makes notes alone only while the call has time

- **WHEN** a chunk's batch refuses before it writes and the call has time to make only some of its notes alone
- **THEN** the call makes those notes alone, stops, and returns a continuation
- **AND** each note that it did not reach keeps `source-applied`, is not failed, and is made by a later call

#### Scenario: A failure after the batch is written

- **WHEN** a step after the batch's write fails for one note of the chunk
- **THEN** every other note is created and that note reports the error
- **AND** no note of the chunk is created a second time

#### Scenario: The write fails after its commit point

- **WHEN** the batch's write fails after its commit point
- **THEN** no note of the chunk is created a second time
- **AND** the identity check records each note of the batch as `already-applied`

### Requirement: Media apply acknowledges durable custody without joining derived work

When `EXOMEM_FAST_DURABLE_ACK` is absent, `adoption_studio(action="apply")` SHALL use the existing durable derived batches. It SHALL commit the imported bytes and sidecar, and persist an admitted extraction job before acknowledging custody. It SHALL publish governed pending visibility before returning and SHALL report unfinished derived work as pending. It SHALL NOT wait for graph and index fan-out to finish. The existing drain SHALL retain ownership of that work until convergence.

An explicit flag value other than `1` SHALL restore synchronous fan-out. The flag's global `1` behavior and other mutations' defaults SHALL remain unchanged. Bulk apply SHALL retain its request budget and continuation protocol for canonical writes.

Both chunk and individual-note pacing SHALL leave headroom for mandatory custody proof and pending publication. When the acknowledgement observation budget expires, the reply SHALL report unproven work as pending without scanning the remaining component states. It SHALL preserve observed completions, failures and diagnostics, and retain any applicable stable advisory reference. Operational checkpoints SHALL NOT synchronously drain existing graph debt during the durable acknowledgement session.

Finish SHALL distinguish confirmed recall results from incomplete or failed queries. An absent target in an incomplete query SHALL remain pending, and a failed query SHALL remain unknown. Only a complete query SHALL establish a miss. Coverage SHALL retain confirmed hits and report how many sampled queries remain unevaluated. Ordinary pending convergence SHALL NOT produce a maintenance recommendation.

#### Scenario: A media import returns while graph work is slow

- **WHEN** default apply imports admitted media and graph or index fan-out cannot finish before the reply
- **THEN** the original and copied bytes, the sidecar and extraction job are durable, and governed recall can read the pending Source
- **AND** the terminal reports derived work as pending, exact retry replays without another page or job, and the drain can complete the work

#### Scenario: An operator restores synchronous fan-out

- **WHEN** `EXOMEM_FAST_DURABLE_ACK=0` is set and a media import applies
- **THEN** the call uses the previous synchronous fan-out path

#### Scenario: An import has many draft notes and existing graph debt

- **WHEN** default apply needs a continuation to leave time for mandatory custody
- **THEN** it returns that continuation within its request budget and keeps committed items recoverable
- **AND** operational checkpoints preserve existing graph debt for the durable owner
- **AND** an expired component observation budget leaves unread required work pending and preserves exact replay

#### Scenario: A component fails before the observation budget expires

- **WHEN** acknowledgement observes a component failure and then exhausts its budget before reading every component
- **THEN** the reply retains that failure and its diagnostic, leaves unread required work pending, and excludes observed completions from unfinished work

#### Scenario: Finish checks recall before a large import has converged

- **WHEN** the pending overlay cannot hydrate every imported page and recall returns an incomplete result
- **THEN** finish reports unverified targets as pending, preserves confirmed hits, and reports unknown counts instead of zero hits or confirmed misses
- **AND** the existing background worker retains responsibility for convergence

### Requirement: A folder outside the vault imports through local staging

`adoption_studio start` SHALL refuse a `path` that is absolute, or that climbs out of the vault by `..`, with `IMPORT_PATH_OUTSIDE_VAULT`, and the message SHALL name `exomem import <folder>`. The check SHALL read the path as text and SHALL NOT resolve it on the filesystem. A path that escapes through a symbolic link inside the vault SHALL get the overview resolver's `INVALID_PATH`.

The `exomem import <folder>` command SHALL enumerate the folder without following symbolic links, and SHALL select files by these rules:

- A hidden entry (a name that starts with a dot, or the Windows hidden attribute), at any depth, SHALL be left behind unless the user passes `--include-hidden`.
- The `.gitignore` files inside the folder SHALL apply, with git's precedence, unless the user passes `--no-gitignore`. The command SHALL NOT hard-code a folder name to leave behind.
- A symbolic link or a Windows junction at any depth SHALL be left behind and never followed. A special file and an unreadable file SHALL be left behind with their own reasons.

Before it copies, the command SHALL report what it leaves behind, with the count and bytes under each reason, and SHALL report the selected file count and bytes against the free space of the vault's filesystem. It SHALL refuse before copying, with the measured bytes and the free space, when the copy plus a reserve does not fit. The limits the adoption engine applies after staging SHALL be warnings, not refusals. Otherwise it SHALL copy each selected regular file into `_Imports/<import_id>/<folder name>/`, streaming, record a manifest of relative path, size and SHA-256, re-hash every copy, and report progress while copying. Every entry left behind SHALL get a per-path code in the manifest. The command SHALL never modify the original folder, and the folder's name SHALL be the collection label. A file that changed while it was read, could not be read, or was refused by the disk SHALL be left out with a code and SHALL keep the import open: the command SHALL exit with an error and say that the same command run again retries only those files under the same import id. A file that the vault refuses because of its own name or path SHALL be left out with the vault's code and SHALL NOT keep the import open: the import SHALL finish, and the report SHALL give the count of such files. An interrupted or incomplete copy SHALL resume from its manifest under the same import id without copying a verified file again. On a resume, a staged file that the folder no longer selects SHALL be removed and recorded in the manifest, and a staged file that changed after it was verified SHALL be copied again. The command SHALL name an unfinished import made with other options and SHALL NOT resume it, and SHALL NOT let two runs write the same import at once.

When the import finishes, the command SHALL end by reporting the bytes it staged, stating that the staged folder is a second copy of the data on the disk until it is discarded, naming the discard route that exists, and printing the `adoption_studio start` path. An import that stays open SHALL also print that path, as the way to adopt what is staged so far.

#### Scenario: A local folder is staged and adopted

- **WHEN** `exomem import ~/Notes` runs for a folder outside the vault and adoption then starts on the printed path
- **THEN** the staged files' hashes equal the originals', the manifest accounts for every entry, and the collection label is the folder's name

#### Scenario: Hidden entries, ignored files and links stay behind and are reported

- **WHEN** the folder holds a hidden folder, a file that its `.gitignore` names, and a symbolic link
- **THEN** none of them is copied, the report before the copy counts each under its reason with bytes, and the manifest gives each a code

#### Scenario: A user opts back in

- **WHEN** the user passes `--include-hidden` or `--no-gitignore`
- **THEN** the entries of that reason are staged like the others, and the printed `start` path asks adoption to include hidden files when hidden entries were staged

#### Scenario: A file that cannot be copied keeps the import open

- **WHEN** a selected file changes while the command copies it
- **THEN** the file is not staged torn, the manifest gives it a code, the command exits with an error and names the rerun, and the same command run again copies only that file under the same import id

#### Scenario: A file the vault refuses does not hold the import open

- **WHEN** a selected file has a name that the vault never accepts
- **THEN** the manifest records the file as left out with the vault's code, the import finishes, the report counts the file, and the command prints the `start` path

#### Scenario: A file the disk refuses keeps the import open

- **WHEN** the disk refuses to store a selected file and the other files are staged
- **THEN** the manifest records the file as left out with a code, the command exits with an error and names the rerun, and the same command run again copies only that file under the same import id

#### Scenario: A resume removes what the folder no longer holds

- **WHEN** a file staged by an interrupted run is deleted from the folder before the rerun
- **THEN** the rerun removes the staged file, and the manifest records the removal

#### Scenario: A folder too large for the disk is refused before copying

- **WHEN** the folder's bytes exceed the vault filesystem's free space
- **THEN** the command refuses with the measured bytes and the free space, and copies nothing

#### Scenario: The command names the second copy

- **WHEN** staging finishes
- **THEN** the report gives the staged bytes, says that they duplicate the original until discarded, and names the discard route

#### Scenario: An outside path given to start is refused with the route

- **WHEN** `adoption_studio start` receives the absolute path of a folder outside the vault
- **THEN** it refuses with `IMPORT_PATH_OUTSIDE_VAULT` and names `exomem import`
