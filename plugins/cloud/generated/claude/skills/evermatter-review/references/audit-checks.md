# Audit checks

This file holds what each audit check flags, its severity and the proposed fix. `inspect_memory(operation="maintenance.audit")`
runs the checks and never rewrites anything. The report is a proposal that you review. Nothing changes without
the user's confirmation per item or per batch. [Vault care](vault-care.md) holds the operations that apply a fix.

The default `detail="actionable"` orders current blockers first, malformed or unregistered relation work next,
and ordinary findings after that. Grandfathered relation-disposition findings are grouped into one
deterministic `legacy_backlog` with exact observed, available, omitted and upstream-truncation counts plus
bounded samples. Use `detail="full"` to enumerate the raw findings. `legacy_sample_limit` accepts integers from 0
through 50 and changes the actionable samples only, never the checks or the stored knowledge.

`list_review_items()` with no `queue` serves the default review surface. A category marked opt-in appears there
only when you pass it in `categories`, for example `list_review_items(categories=["duplicate_content"])`. The
`maintenance.audit` operation takes the same `categories`.

- **Orphans** — compiled pages with zero inbound links and zero outbound links beyond their `sources` block. Propose: link or archive.
- **Broken wikilinks** — `[[X]]` where `X` does not resolve. The audit skips wikilinks inside fenced code blocks and inline code spans. A bare name resolves against filename stems and frontmatter `title:`, so a date-prefixed source with a matching title is not flagged. A link with an explicit non-`.md` extension (`[[…/scan.pdf]]`) resolves if that file exists on disk. Findings inside append-only trees (`Sources/`, `Evidence/`), which cannot be repaired in place, are `info`, which keeps them out of the actionable `warn` set. Propose: fix the path or create a stub entity.
- **Supersession integrity** (`supersession_integrity`) — two defects in the supersession pointers you authored, neither of them thresholded or inferred. A dangling pointer is a `supersedes:` or `superseded_by:` wikilink that does not resolve to a page. It leaves a reader unable to tell "never superseded" from "superseded, link rotted". A multi-headed chain is a connected chain of more than one member that nothing supersedes, so it has no single current answer. Both are `warn`. A chain finding anchors on the chain's first member and lists the heads in `meta.heads`. Parked statuses are not excluded: a `superseded` page is where a rotted forward pointer lives. The check does not detect a page still marked `status: active` that something already points at with `superseded_by:`, because that pointer resolves. Nothing is repaired or re-pointed. Propose: fix the target path, restore the missing page, drop a pointer that describes a supersession that never happened, or decide which head is current. No threshold here is one this system chose, so the category is part of the default review surface.
- **Stale frontmatter** — required fields missing for the page type ([page types](page-types.md)). It includes a pattern with `project:` (singular) where `projects:` (plural) is the convention for cross-project patterns.
- **`index.md` / `log.md` drift** — files in folders that are not catalogued, catalogue entries that point to missing files, or `log.md` entries without a matching artifact on disk, and the reverse. The server maintains both files, so drift comes from out-of-band edits. Propose a regeneration.
- **Unprocessed sources** (`unprocessed_source`) — a `Sources/` file that no live compiled note cites. A page in `ingested_into:` clears this citation-linkage finding only when it is live (not a draft, not archived or superseded) and lists the Source in its own `sources:`. Each page that does not is a `derivative` on the finding, with its reason (`pending` for a draft, `retired`, `claim_unresolved` or `missing`). Findings are aged and triaged oldest-first: each carries `meta` (`age_days`, `age_bucket` as fresh under 30 days, aging under 90 days or stale, `captured`, the title, a bounded excerpt, the recorded grouping fields and the `derivatives`), and a stale one escalates to `warn`. Work the backlog in batches: read about ten with `list_review_items(queue="unprocessed_sources", limit=10)`, group them by topic, call `inspect_memory(operation="sources.compile", arguments={"sources": [...]})` for an optional one-note scaffold, then follow the [source compilation pass](writing.md#source-compilation-pass). An opening excerpt is not a full-source read, and one citation clears this queue, not semantic coverage. Dismissing an item lowers the visible count only. The served `outstanding` count falls when a live note cites the Source.
- **Status / location mismatch** — pages with `status: archived` not living in an `_archive/` subfolder, and the reverse.
- **Unfinished experiments** (`unfinished_experiments`) — an experiment whose `started` date is present, whose elapsed time exceeds its declared `duration`, and which records no `outcome:`. The trigger is the missing outcome, not the status: `status: concluded` says the experiment stopped, and `outcome:` says what it showed, so a concluded experiment with no outcome is still surfaced. That is the case the check most exists for. An open-ended `duration` (`ongoing`, or any value that is not a finite span) declares no window and is never flagged. Archived, superseded and draft experiments are out of rotation. A measurement-only queue at `info`, ordered oldest-first by elapsed age, with `meta` carrying `started`, `duration_days`, `elapsed_days` and `overdue_days`. Nothing is auto-concluded, archived or inferred. Propose: write up the result (`status: concluded` plus a categorical `outcome:`), extend `duration:` if it is still running, or archive it. This category is opt-in, so it never displaces the default daily queue.
- **Unfinished production lifecycles** — production-logs with `status: recorded` or earlier whose `published` field has been null for more than 60 days. Propose: update the status, fill the outcomes, or move to dropped.
- **Stale hubs / snapshots** — a separate concern from `stale_review`, which excludes hubs and snapshots on purpose. A research-note in the hub or snapshot role is expected to drift: a hub refreshes on a major capability ship, and a snapshot is point-in-time by design. No check flags them on their own cadence. Refresh a hub when a major ship warrants it, and treat a snapshot as historical.
- **Unregistered project key** — pages with a `project:` or `projects:` value that is not in `_Schema/project-keys.yaml`. It catches drift from `files.create` writes that bypass the auto-register flow. Propose: fix the value with `edit_note(action="set_fields", fields={...})`, or register the new key as [writing](writing.md#research-scope-keys) says.
- **Graph drift** (`graph_drift`) — derived `.graph.sqlite` state that is missing, stale, disabled or schema-incompatible. Propose: run `maintenance.reconcile` to rebuild or refresh the sidecar. When graph indexing is disabled, the check is a read-only no-op that reports availability instead of loading optional dependencies.
- **Embedding drift** — sidecar rows older than the file on disk, which usually means an out-of-band editor edit that bypassed Evermatter's writer hooks. Propose: run `maintenance.reconcile` (incremental, stale rows only), or `maintenance.fix` with `rebuild_embeddings=true` (full wipe and rebuild).
- **Stale review** (`stale_review`) — active compiled conclusions (research-note, insight, pattern, failure, entity) that are all three: old (frontmatter `updated` or `created` beyond `EXOMEM_STALE_AGE_DAYS`, default 365), rarely surfaced by search (appearances in `logs/queries.jsonl` `top_k` at most `EXOMEM_STALE_MAX_ACCESS`, default 1), and low in inbound links (wikilink in-degree at most `EXOMEM_STALE_MAX_INBOUND`, default 1). A measurement-only review queue at `info`. It surfaces candidates, with `meta` (`age_days`, `age_bucket`, `inbound_count`, `access_count`), for you to judge. It never decays, down-ranks, hides or moves anything, and search ordering is unchanged. All three signals come from what the Knowledge Base already records, with no new sidecar. The gate is an AND filter, not a score. `EXOMEM_DISABLE_RELEVANCE_CHECK` gates the access signal. When the query log is unavailable, that conjunct is dropped: absence is unknown, never a fabricated zero, and the gate falls back to age and low inbound links. The scope is a conclusion type in the read-write tree. It excludes readonly trees, append-only `Sources/` and `Evidence/`, superseded and archived pages, index files, and hubs and snapshots. Propose: confirm it is still true (keep), `supersede_note`, or archive.
- **Prediction window** (`prediction_window`) — a semantic unit whose authored `check_by` date has arrived or passed with nothing recorded against it. A unit is surfaced when its `check_by` is on or before today, it carries no `verdict:`, and it carries no outbound relation of kind `supports`, `contradicts`, `resolves` or `evidenced_by`. Any governed unit kind with a `check_by` counts, not only `## Prediction`: an authored due date is a due date. Clear it by recording a categorical `verdict:` on the unit (`confirmed`, `refuted`, `qualified`, `inconclusive`, `abandoned`), by attaching the evidence that settles it with one of those four relations, or by moving `check_by` out if the question is still open. The check never judges whether the prediction held: the `verdict:` is yours to write. Resolution is unit-local: only a `verdict` or a relation on the unit itself clears it, so evidence on another page does not. Record the judgement on the prediction. Pages with `status` of `superseded`, `archived`, `draft`, `dropped` or `planned` are out of rotation. A measurement-only queue at `info`, ordered most-overdue-first, with each due unit as its own review item. The unit's fingerprint identifies it, so editing a prediction resurfaces it instead of reusing an earlier decision. Unlike the unfinished-experiments queue, this one is part of the default review surface: an obligation nobody is shown is one nobody meets.
- **Question aging** (`question_aging`) — a governed question unit (a `## Open Question` block, or any unit whose resolved category is `question`) on an active page, with an authored date at least 30 days old, no `verdict:` and no answering relation of kind `supports`, `contradicts`, `resolves` or `evidenced_by` on the unit itself. The unit-local rule is the one the prediction window uses, for the same reason. The 30 days is a number this system chose, not one you declared, so a finding is a review candidate and never a defect. An open question is a legitimate long-lived state, and leaving it open is a valid answer. Measurement-only at `info`, ordered oldest-first, each unit its own review item identified by its fingerprint. The threshold is invented, so the category is opt-in: a tuning decision never displaces authored work on the daily queue. Propose: answer it with a categorical `verdict:` on the unit, attach what settles it with one of those four relations, or leave it open.
- **Note cohesion** (`note_cohesion`) — a compiled page that has reached a size step: 9 semantic units, then 14, 21, 32 and so on, each step the previous one times 1.5 rounded up. Hubs (`hub` or `snapshot` tag), Sources, `index.md` and `log.md` are never asked. The steps are a number this system chose, so a finding is a question and never a defect: the item states the counts and asks whether the page answers more than one independent question. Read the page and answer, with [writing](writing.md#answering-a-structure-suggestion). Measurement-only at `info`, one item per page. It is opt-in, and it is also served as a backlog in due-state, after dated obligations.
- **Ownership review** (`ownership_review`) — a compiled page with one project key that was asked whether it belongs in its current project and home folder. A page is asked when it is created in a project, when its project key or title changes, and when the daily rotation reaches it. The rotation asks at most a small daily number of never-asked pages (`EXOMEM_OWNERSHIP_ROTATION_DAILY` sets it, and `0` turns it off), takes projects in turn, and within a project asks the page farthest from the rest of the project first, by authored date when the project has fewer than three eligible pages. The distance orders the rotation and never excludes a page. A page that was never asked has no item. Hubs (`hub` or `snapshot` tag), Sources, `index.md`, `log.md`, pages in a parked status and pages that name several projects are never asked. A finding is a question and never a defect, and it carries no score. Read the page and answer without changing it, with [writing](writing.md#answering-a-structure-suggestion). Measurement-only at `info`, one item per page. It is listed on the default review surface after every other default queue, and also served as a backlog in due-state, after dated obligations and before note cohesion. The due-state block reports how many eligible pages were never asked and the day that was counted. To change a page's home, open `inspect_memory(operation="curation.work_item", arguments={"review_ref": ...})` with the item's `review_ref`: it assembles that page.
- **Name drift** (`name_drift`) — compiled and entity pages whose file name differs from the name their title would get today, under the vault's filename style. A collision number on a matching name is not drift, and Sources and other dated captures keep their names. One item stands for the whole vault, with the count and the call that renames the next batch. Its fingerprint changes when the set of drifted pages changes. A page with no usable title is counted and never proposed. Propose a batch to the user as one preview. `inspect_memory(operation="maintenance.name_drift")` lists the pages, one page at a time. Answer it with [writing](writing.md#answering-a-structure-suggestion).
- **Unaddressed facet claims** (`entity_facet_unaddressed`) — an entity page whose facet bullets carry claims that parse as nothing addressable: a plain prose list item outside `## Observations` that is not a unit, not a markdown relation and not wikilinks only. A claim without an anchor can take no supersession, verdict or link, so the finding names the facet heading and the item. It is `warn`. The fix converts the item in place to a compact unit `- [<section category>] <text> ^<anchor>`, and only inside a reviewed maintenance plan: preview it with `maintenance.fix` and `dry_run=true`, then run the plan you reviewed. A local apply without that plan never moves those lines.
- **Relation debt** (`relation_debt`) — active writable compiled pages with no outbound body wikilinks and no canonical note or block relations. It excludes archived and superseded pages, indexes, append-only and read-only trees, hubs and snapshots. It is an informational proposal, not a write failure. Inspect `connections.suggest_relations` or `connections.suggest_links`, then add only defensible edges under `## Relations` (or block `relations:` metadata). Never turn semantic proximity into a durable edge automatically.

## Artifact role and transient state

`artifact_role_promotion` measures reusable procedures with separately bound successful outcomes, and synthesis
across distinct visible sources in active experiments. `transient_state_review` measures current pending wording
alongside a compatible observed result for the same authored episode and subject. Both are deterministic English
cue checks: ambiguous or unsupported wording stays quiet. Read the exact evidence units before you decide on a
governed extraction or correction. Neither signal grants mutation authority.

Choose a home by role even within one topic: a repeatable successful procedure belongs in a reusable note, and an
attributed multi-source synthesis in a research note. Search existing eligible homes first. Keep exact source-unit
provenance: reference both the procedure and outcome units when you extract a method, or the synthesis unit and its
distinct sources when you compile a research note. Keep the experiment history. A title match or parent backlink does
not prove the material has a home; exact represented content does. For example, a batch with two separately
successful methods and a synthesis across two sources needs each method checked on its own evidence and the synthesis
checked independently. A protocol without reusable success, or a list of citations alone, needs no promotion. "No
taste results yet" beside an observed taste result invites a current-wording correction; "Previously no taste results
existed" is history, kept through a governed edit or an exact supersession.

Extraction and restructuring use confirmed `restructure_execution`; an already-authorized local correction needs no
extra confirmation. Observed transaction state goes to Records through the ordinary claims route, never as an automatic
append from a review signal. Apply the same role-first reasoning to visible conversation turns before you write. An
MCP-only server cannot detect or capture a tool-free turn. Explicit
results expose `meta.coverage` as complete, capped or unknown per family, including empty results. Compact
due-state omissions do not prove complete coverage. Reconciliation heals missing or stale derived support.

## Held collection view corrections

`collection_view_corrections` lists each owner edit of a store collection view that the substrate held instead of
applying: a save from a stale editor buffer (`VIEW_CONFLICT`), an edit the collection's rules refuse
(`VIEW_INVALID`), a deleted view (`VIEW_DELETED`), or a view stamped by another store (`VIEW_FOREIGN`). Each
finding names the read-only view under the collection's `Held/` directory, with `meta.held_id` and `meta.code`.
The category is part of the default review surface, and it lists only corrections you may read.

1. Read the held view and its diagnostics. `inspect_memory(operation="review.held")` lists the held items with the
   choices that resolve them.
2. A `VIEW_CONFLICT` or `VIEW_FOREIGN` correction holds bytes that the view no longer shows. Resume a Records
   `VIEW_CONFLICT` with `edit_record(collection=..., record=..., changes=..., why=..., held=<meta.held_id>)`. The
   owner's edit is rebased, so it never reverts a later change to a field the owner did not touch.
3. Resolve a held item with `manage_memory(operation="maintenance.held_resolve", arguments={"path": ..., "choice": ...,
   "item_version": ..., "why": ...})`. `choice` is `keep-mine` or `keep-store`, and `item_version` comes from
   `review.held`. `keep-store` saves the owner's file first, so no bytes are lost.
4. Discard a correction with a reason through
   `manage_memory(operation="records.discard", arguments={"collection": ..., "held": <meta.held_id>, "why": ...})`.
   A later edit of the view never resolves a `VIEW_CONFLICT` or `VIEW_FOREIGN` correction.

A `VIEW_INVALID` edit kept in place, or a `VIEW_DELETED` view, resolves when the owner saves a valid edit of the
view or when you discard it. Discarding an invalid view that is still in place renders the view from the store
again.

## Duplicate content and source URLs

Both categories are opt-in. The default audit and the default review surface skip them, and every finding is
advisory. The lexical index only proposes groups. The audit reads each member again and re-hashes its original,
so a stale index entry gives no finding.

- `duplicate_content` reports one finding per group of two or more live Sources or Evidence pages whose bytes are
  identical. It names each member (`ref`, `path`, lane, families or import mappings, capture date), the suggested
  canonical page, the bytes a fold would free, and in `meta.steps` the exact `fold-duplicate` step for each copy.
  The suggestion is the oldest filing by capture date, then path, unless a page the fold cannot rewrite (a Source,
  an Evidence page or another owner's page) cites another member. That member is then the canonical page, and
  `meta.canonical_because` says which rule applied.
- A finding offers no step when `meta.no_fold` gives a reason: the group spans lanes, the pages are raw-protected,
  two or more members are cited by pages the fold cannot rewrite, or the fold refuses a pair (a text Source beside
  a Source with an original, or an Evidence page outside its family folders). Raw-protected Evidence is never
  rewritten, because a release grant binds to each page's content hash. A step appears only when the proposal
  accepts it.
- A caller who cannot read every page sees only the members it can read and gets no step, because a fold needs
  whole-namespace authority. It learns nothing about which unreadable page cites a member: its answer equals that
  of a vault without those pages.
- `duplicate_source_url` reports one finding per group of Sources that share a canonical URL but hold different
  text. It names them and offers no fold. Link the Sources, or supersede one through the normal tools.

The report counts groups and copies by lane and states that each count was re-hashed. Before it counts, the audit
reconciles the index with the pages, and it calls the index `complete` only after that succeeds.
`stale_index_groups` counts groups whose entries no longer match their pages. `unverifiable_groups` counts groups
of raw-protected pages that lack the binding which proves their original, with the reason in
`unverifiable_because`. An index that cannot answer, such as a new or rebuilding one until its background rebuild
lands, reads as `unchecked` with coverage `unknown`, never as zero findings. Propose the steps of one finding as a
single curation plan, keep or change its canonical page, then preview and confirm the exact plan
([vault care](vault-care.md#curation)). An undo is a compensation plan.

## Stored original fixity

`original_fixity` lists each stored original that the background check found `missing` or `corrupt_no_copy`
(`error`), or `restored` (`info`). Each finding names the binding page, with `meta.original_path`, `meta.kind`,
`meta.cause`, `meta.expected_sha256` and `meta.route`, the candidate copies that may hold the expected bytes. A
`restored` original needs no action: its changed bytes are in Trash. The item stays until the check verifies that
original again, which takes up to one verification cycle. For the other two, tell the owner the cause and the
route. Never record a new hash for changed bytes.

## Path variants

`path_variants` (run by default) lists sibling names that one portable filesystem would hold as a single
entry. It reads the files an export keeps. Names equal after Unicode NFC normalisation and casefolding are
defects (`error`): the vault cannot be exported or opened on Windows or default macOS. For a group of
folders, preview the merge with `maintenance.path_variants` ([vault care](vault-care.md#maintenancepath_variants));
a group with a file in it is renamed by hand. Names equal only after NFKC folding and dropping separators
are candidates (`info`) for a human call; nothing merges them.
