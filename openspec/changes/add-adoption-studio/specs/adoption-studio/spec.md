# adoption-studio

## ADDED Requirements

### Requirement: Deterministic read-only scan and candidate inventory

`adoption_studio(action="start")` SHALL perform a read-only `overview` scan of the target subtree and record, in the run object, a compact `scan_summary`, `pack_suggestions`, and a candidate `inventory` containing every adoption-relevant file outside `Knowledge Base/` with its vault-relative path, byte size, and mtime. Ineligible candidates SHALL be retained in the inventory with a machine-readable `reason` (never silently dropped), the inventory SHALL be capped at a configurable `max_candidates` with an explicit `inventory_truncated` counter, and a stat-based `inventory_fingerprint` SHALL be computed over the sorted eligible rows. The scan SHALL write nothing outside the run object. `start` SHALL require `Knowledge Base/` to exist and refuse with `KB_NOT_INITIALIZED` unless `initialize_kb=true`, in which case it initializes the scaffold first; pure pre-init discovery remains `adopt_vault(mode="scan-only")`, unchanged.

#### Scenario: Scan snapshots an eligible-only inventory deterministically

- **WHEN** `start` runs twice against an unchanged subtree
- **THEN** both runs record the same eligible candidate rows in the same order and the same `inventory_fingerprint`
- **AND** ineligible files (for example non-text or already-governed paths) appear in the inventory with a `reason` rather than being omitted
- **AND** no file outside the run object is created or modified

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

`adoption_studio(action="select")` SHALL accept a folder-rule selection payload `{include, exclude, overrides, include_junk}` and materialize the concrete file set **server-side** so the client never enumerates thousands of files. Each materialized path SHALL be validated against the inventory and rejected per-path with a machine-readable code — `NOT_IN_INVENTORY`, `UNSUPPORTED_IMPORT_TYPE`, or `ALREADY_GOVERNED` — never silently dropped. Any selection change SHALL clear any existing plan and return the run to phase `selecting`. Inventory drill-in SHALL be available with offset paging so the UI can page bounded file lists.

#### Scenario: Folder rules materialize a validated file set

- **WHEN** `select` is called with `include` of a folder, an `exclude` of a subfolder, an `overrides` list, and `include_junk=false`
- **THEN** the server materializes the concrete selected paths from the depth-capped tree and records them
- **AND** any unsupported, missing, or already-governed path is returned in a per-path rejection list with its code

#### Scenario: Changing the selection invalidates the plan

- **WHEN** a run in phase `planned` receives a `select` call that changes the materialized set
- **THEN** the stored plan is cleared and the phase returns to `selecting`

### Requirement: Exact-action preview bound by plan identity

`adoption_studio(action="plan")` SHALL read and sha256-hash each selected file at plan time, compute the exact deterministic target path, title, and frontmatter for each item, record any skipped items with a code, and persist a `plan` carrying a `plan_id` derived from the selection hash and the per-item hashes. `plan` SHALL write nothing outside the run object (targets are previewed, not created). The preview a caller sees SHALL be exactly what a subsequent `apply` commits, or `apply` SHALL refuse.

#### Scenario: Plan previews exact targets without writing

- **WHEN** `plan` runs over a non-empty selection
- **THEN** each item records its `original_sha256`, exact `target_path`, `title`, and `frontmatter`, and a stable `plan_id` for identical inputs
- **AND** a byte snapshot of the vault before and after `plan` is identical outside the run object

#### Scenario: Apply refuses a stale plan

- **WHEN** `apply` is called with a `plan_id` that does not equal the stored plan's id, or the current selection hash no longer matches the plan's selection hash
- **THEN** it is refused with `PLAN_STALE` and writes nothing

### Requirement: Governed apply with write-time re-validation

`adoption_studio(action="apply")` SHALL, for each planned item, re-read the original bytes and re-verify the sha256 against the value captured at `plan` immediately before writing, then commit the validated subset plus the Sources index, top index, and log update in ONE `batch_atomic_write` through the refactored `commit_import_items` path. An item whose bytes changed SHALL be excluded with per-item outcome `SOURCE_CHANGED`; a missing item SHALL be excluded with `NOT_FOUND`; a target that now exists out-of-band SHALL be re-resolved to a fresh unique target. Applied Sources SHALL carry `imported_from` and `original_sha256` provenance. The result SHALL include `verified_unchanged` and `verified_total` counts from a post-apply re-hash of the originals. A `plan`/`apply` invalidated by inventory drift SHALL surface `ADOPTION_SOURCE_CHANGED` so the client can re-scan while preserving the still-valid selection.

#### Scenario: Apply copies with provenance and leaves originals byte-identical

- **WHEN** `apply` commits a validated plan
- **THEN** each imported Source carries `imported_from` and `original_sha256`, the Sources index and log are updated in one atomic batch, and every original file is byte-identical before and after
- **AND** the result reports `verified_unchanged` equal to `verified_total`

#### Scenario: A source changed after plan is excluded, not fatal

- **WHEN** one selected original is modified between `plan` and `apply` and others are unchanged
- **THEN** the changed item records outcome `SOURCE_CHANGED`, the unchanged items are applied, and the run phase becomes `partial`

### Requirement: Per-item outcomes, partial results, and failed-subset retry

The run SHALL record a per-path outcome (`applied`, `failed`, `skipped`, or `already-applied`) with a code for failures. When some items apply and others fail the phase SHALL be `partial`; when none apply it SHALL be `failed`. `apply(retry_failed=true, only_paths=[...])` SHALL re-plan and re-apply only the failed subset in place with fresh hashes, and re-applying an already-applied item SHALL be an idempotent `already-applied` skip. The UI-facing retry SHALL be expressible as `scope:"failed"`.

#### Scenario: Retry re-applies only the failed subset

- **WHEN** a run is `partial` and the previously failed original is restored, then `apply` is called with `retry_failed=true`
- **THEN** only the failed subset is re-planned and applied, previously applied items are left untouched, and the phase becomes `applied`

#### Scenario: Re-applying an applied item is idempotent

- **WHEN** `apply` is re-run over a plan whose items already have `applied` outcomes
- **THEN** those items report `already-applied` and no duplicate Source is created

### Requirement: Interrupted apply is visible and recoverable

The run SHALL persist phase `applying` before the first item write so a process death mid-apply is visible. `status` SHALL report `interrupted: true` when the phase is `applying` with incomplete outcomes, and a subsequent `apply` SHALL be safely re-runnable because already-applied detection and fresh write-time re-validation make re-entry safe.

#### Scenario: A crashed apply is reported and can resume

- **WHEN** the run is left in phase `applying` with incomplete outcomes
- **THEN** `status` reports `interrupted: true`
- **AND** calling `apply` again completes the remaining validated items without duplicating applied ones

### Requirement: Cancellation rules preserve applied work

`adoption_studio(action="cancel")` SHALL be allowed from `selecting`, `planned`, `partial`, and `failed`, recording `{at, why}` and setting phase `cancelled`. It SHALL be refused during `applying` with `CANCEL_DURING_APPLY` and after `applied`/`done` with `ALREADY_APPLIED`. Cancellation SHALL never delete applied Sources — governance is append-only and originals were never touched.

#### Scenario: Cancel closes a pre-apply run

- **WHEN** `cancel` is called on a run in `selecting` or `planned`
- **THEN** the phase becomes `cancelled` with the recorded reason and no vault Source is created or removed

#### Scenario: Cancel is refused mid-apply and post-apply

- **WHEN** `cancel` is called while the phase is `applying`
- **THEN** it is refused with `CANCEL_DURING_APPLY`
- **AND** calling `cancel` after the phase is `applied` is refused with `ALREADY_APPLIED` and applied Sources survive

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

`adoption_studio(action="finish")` SHALL be allowed from `applied` and `partial`, run a deterministic recall check (keyword `find` over the largest imported title) producing `recall_check = {query, ok, hits}`, compute a `first_question` referencing that title with a ready-to-run `ask_memory` route, optionally write a run manifest under `Knowledge Base/_Adoption/`, and set phase `done`. Open proposals SHALL NOT block finish; there is no engine timer or background thread. `status`/`finish` SHALL include a `handoff` block carrying `prompt_text` and deterministic `links`.

#### Scenario: Finish proves recall and offers the first question

- **WHEN** `finish` runs on a run with applied imports
- **THEN** `recall_check.ok` is true because an imported target appears in the keyword hits, `first_question` references the imported title with an `ask_memory` route, and the phase becomes `done`
- **AND** any run manifest is written only under `Knowledge Base/_Adoption/`

### Requirement: Hosted staging intake for uploads and archives

In hosted mode, adoption material (uploaded files and ZIP archives) SHALL land as RAW files under the vault-relative per-run staging directory `_Staging/adoption/<run_id>/`, outside `Knowledge Base/` so the engine treats them as legacy input. ZIP archives SHALL be expanded cell-side with zip-slip protection (every extracted path confined under the staging directory) and enforced entry-count and total-size caps. `adoption_studio(action="start")` SHALL accept `path` pointing at that staging directory and scan it with the identical engine used locally; local runs SHALL never use staging.

#### Scenario: A ZIP is expanded safely into per-run staging

- **WHEN** an uploaded ZIP whose entries include a traversal path (for example `../escape.md`) is expanded cell-side
- **THEN** every extracted file is confined under `_Staging/adoption/<run_id>/`, the traversal entry is rejected, and entry-count and size caps are enforced
- **AND** `adoption_studio(start, path="_Staging/adoption/<run_id>")` scans the staged files through the same engine as a local run

### Requirement: Originals are never modified

Across every action and every failure mode, adoption SHALL never rewrite, move, or delete an original file. Imports are copies into `Knowledge Base/Sources/Imported/` carrying provenance; scans, selection, and preview write nothing outside the run object; cancellation and partial/failed apply leave already-copied Sources intact and originals untouched.

#### Scenario: Originals are byte-identical after any outcome

- **WHEN** a run proceeds through scan, select, plan, a partially-failing apply, a retry, and a cancel attempt
- **THEN** every original file is byte-identical to its pre-run bytes at every step
- **AND** no original is moved or deleted by any action
