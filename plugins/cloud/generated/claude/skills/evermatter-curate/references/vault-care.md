# Vault care

Care covers three jobs: learn an unfamiliar vault, review it with the audit, and restructure it through curation
or a maintenance operation. Only `Knowledge Base/` is governed and writable. Every folder beside it is read-only
input ([write scope](write-scope.md)). Each call below lists its fields in `read_instructions(topic="<operation>")`.

## Assessing or adopting a vault you did not build

Make this your first move in any unfamiliar vault.

- **Import or adoption question.** Start with `inspect_memory(operation="imports.scan")`. It runs a bounded,
  read-only scan, suggests likely knowledge packs, reports a semantic census and lists safe next actions. A scan
  never rewrites an original. The write operations stay under `Knowledge Base/`:
  `manage_memory(operation="imports.save_manifest")` saves the manifest, and `imports.copy_as_sources` copies
  chosen legacy text files as Sources with their original path and hash.
  `imports.compile_selected` returns a proposal, not a compiled note. Review the proposal, then call
  `create_note`, so the normal semantic precommit contract still applies. A whole folder, ZIP or notes export
  goes through [importing](importing.md).
- **Structural question** ("what does this vault look like", "is there junk in here"), or the need to learn the
  layout before you write. Call `inspect_memory(operation="files.overview", arguments={"path": "<folder>"})`. It
  returns one bounded report of folder structure, counts, frontmatter coverage, naming patterns and junk
  candidates, such as zero-byte files and sync-conflict duplicates. It works on any folder under the vault root,
  also outside `Knowledge Base/`, and on a vault with no Knowledge Base.

Drill down from the report with `files.overview` on the folders of interest, `search_memory(query=...,
options={"scope": "vault"})` for content, and `read_page` for single files. Link to a folder outside
`Knowledge Base/`, and never write it. Use the adoption output to decide whether to save a manifest, copy
selected originals into Sources, or compile selected material into governed notes. Do not rewrite the old vault
by default. Never read a vault file by file to answer a structural question: the report answers in one call what
hundreds of reads would cost.

## What care does not do

- It applies no retention decay and no forgetting curve. Old material stays. Mark a superseded page, and archive
  an irrelevant one into an `_archive/` subfolder. The `stale_review` check surfaces old, cold, low-inbound
  conclusions as candidates for you to judge. It never decays, down-ranks, hides or moves anything, and it never
  changes search order. Authored typed relations do inform ranking, because you wrote that connectivity.
- It scores no claim. A verdict is a judgement in words, never a number. A degree of belief that its holder
  stated is a `## Belief` unit ([page types](page-types.md)). Citation count and recency are the trust signal for
  the claim itself.
- It compiles nothing blindly after a capture. Compilation is a deliberate step at a stepping-stone, and it is
  always reported. "No silent dump" targets noise, such as transcripts and mid-flight tangents. It never licenses
  pruning detail from a note ([writing](writing.md)).
- An agent acts on the vault only inside a turn, because the user asked or because the conversation clearly
  warrants consulting or capturing. A hook reminder is a prompt, not an operation.

## Audit

`inspect_memory(operation="maintenance.audit")` runs read-only checks and proposes fixes. It never fixes
anything. [Audit checks](audit-checks.md) describes each check, its severity and the proposed fix. Fixes that
rewrite content go through `maintenance.fix` and the other maintenance operations below, or through curation.

## Maintenance operations

`maintenance.fix` rewrites wikilinks and frontmatter. `maintenance.backfill_ids` mints stable ids.
`maintenance.capture_metadata` and `maintenance.convert_originals` repair media pages and original storage.
Each of these previews first: pass `dry_run=true` through `inspect_memory`, and write through `manage_memory`.
`maintenance.reconcile` heals index-count and sidecar drift from out-of-band edits. It is idempotent and
non-destructive, and it writes unless `dry_run=true` asks for a preview. The local CLI runs these writes in one
call. A remote client runs the whole curation workflow and the maintenance writes that its surface publishes.

### Remote maintenance jobs

A remote write of `maintenance.fix`, `backfill_ids`, `capture_metadata`, `reconcile` or `convert_originals` runs
as a background job, preview and write alike. Only the vault owner can start, read or cancel one, and one job
runs per vault.

1. Preview with `inspect_memory`, passing `dry_run=true`. The owner gets a `run_id` at once, because a preview of
   a large vault outlasts one request. When the job has completed, its receipt's `result` carries the
   `plan_id`. A caller other than the owner, and the local CLI, get the report in one call.
2. Start the write with `manage_memory`: the same operation and flags with `dry_run=false`, that `plan_id` and a
   one-line `why`. It returns a `run_id` at once. Without both, nothing starts.
3. Follow it with `inspect_memory(operation="maintenance.job", arguments={"run_id": ...})` until the state is
   `completed`, `failed`, `cancelled` or `interrupted`. Without a `run_id`, it lists recent jobs.
4. Stop one early with `manage_memory(operation="maintenance.cancel_job", arguments={"run_id": ..., "why": ...})`.
   The job stops at its next checkpoint.
5. Read the receipt. A finished job reports the pages it committed, the pages it skipped because they changed
   after the preview, and the pages whose outcome is unknown. The page list is long: pass each returned
   `next_cursor` as `cursor` to the same call until it is `null`. A preview's entries are the items its `plan_id`
   covers.

If the vault changed after the preview, the job writes nothing and returns a fresh `plan_id`: preview again.
A second start while a job runs is refused, and its reply points to the running job. `fix` and `backfill_ids`
write in short chunks that let other writes through. `reconcile` holds writes only in short holds, one per phase
that writes. A refused write during one of those holds names the job's `run_id`; retry it.

### `maintenance.convert_originals`

An archive that predates shared original storage keeps each original as an ordinary file at its own path. This
operation stores the bytes once in the shared store and then removes the ordinary file. The original keeps its
path, and its page keeps every byte, with its references and grants. Only the owner can run it.

The preview counts what it would convert, what is already shared and what only has a stored copy. It lists each
unreadable original with a reason, and conversion leaves those unchanged. A run that stops leaves every original
readable. Run the conversion again to finish an interrupted run, or the originals it skipped because they
changed or a reader held them. If the service's launcher cannot serve shared storage yet, the operator must
update the launcher first.

### `maintenance.capture_metadata`

A photo or video filed before capture metadata was recorded has a page without the capture fields
(`capture_time`, `capture_date`, `capture_date_source`, `capture_lat`, `capture_lon`, `camera_make`,
`camera_model`, `pixel_width`, `pixel_height`). This operation reads each original that has bytes on the volume
and adds the fields its file states. A file that states no time takes the time its file name states, when a
registered name format reads one. The operation replaces only that block of the page. The extracted text, the
preserved notes and every other field stay as written. Only the owner can run it.

The preview counts the pages it would update, the pages that are current, the files that state nothing, and the
skipped pages with each reason's count. A page is skipped when its original has no bytes on the volume (the job
never fetches one back), when it holds unmarked preserved notes, or when this host has no decoder for the
original's kind. Run it again after the bytes of a skipped original return.

`capture_date_source` names where a date came from, as a key of the `capture-date-sources` vocabulary:
`camera-exif`, `matched-original`, `file-name` or `source-date`. The first three are evidence. `source-date` is a
fallback: a photo or video with no evidence date answers a `page.capture_date` filter with the day it was filed,
labelled `source-date`, and nothing is stored. Filter on `page.capture_date_source` to keep or drop those. A date
of a higher source is never replaced by a lower one, in this order: camera, matched original, file name. The
vault overlay `_Schema/capture-date-name-formats.yaml` adds file-name formats. Each needs a `pattern` that names
the groups `year`, `month`, `day`, `hour`, `minute` and `second`, a `maker`, and a one-line `description` of why
the name states a capture time.

`recover=true` also matches each photo that states no date to an original that states one. It searches the
camera-dated images in the vault and the images in `originals_root`, a read-only folder of originals on a
personal server: an absolute path outside the vault, refused on Evermatter Cloud, and it implies `recover`. A
match is accepted only when exactly one capture time explains the photo, by picture structure and shape, and the
closest original still agrees at a finer scale. A look-alike page of another picture stays undated, with that
reason. The page then records `capture_date_matched_from`: a page id, or a path inside `originals_root`. The job
never writes, moves or copies anything in that folder. Read the preview before the write: it lists the source of
each date the photos will carry, the photos left undated, the unmatched ones with their reasons, and the planned
matches. Run it again after new originals arrive in the vault or the folder.

### `maintenance.structured_files`

This operation previews one Planning or Records collection's manifest-declared human filenames and managed
readable bodies, including the governed inbound-link rewrites. The preview is read-only. The apply needs the
exact `plan_id`, the `source_snapshot` and a `why`, and it commits atomically. Durable identity and mutable
state stay in frontmatter, never in filenames.

### `maintenance.tag_variants`

This operation lists tags that differ only by case, separator or plural. It groups them with visible page counts
and takes the most-used written form as canonical. A tie is listed and never rewritten. The preview is
read-only. Apply (`apply=true`) with the preview's `plan_id` and a one-line `why` rewrites one bounded batch of
minority variants to the canonical tag and logs a rollback record. Only the `tags` key changes. Sources,
Evidence, Records, Planning and other owned trees stay untouched. Preview again for the next batch.
`exclude_groups` keeps named groups out of preview and apply alike.

### `maintenance.collections`

This operation reports whether each file Records and Planning collection would move into the collection store
cleanly. It takes only an inventory `cursor`, is owner-only and writes nothing. Each collection reports its rows,
legacy audit status, round-trip proof, stamped items and any blocker. A stamped item carries the vault's note
identity (`exomem_id`), which the move sets aside. A blocker names its code and, for a failed proof, the check
letter. Datasets are skipped, and every copy of a duplicated collection identity is blocked.

The inventory lists every collection, in the store and in files, one page at a time: pass `next_cursor` as
`cursor` for the next page. A value that is unknown or does not apply is null. The report also covers vault setup. Without configured writer-lease coordination, a vault with collections to
migrate stays `blocked` even when every collection passes. The vault verdict is
`migratable`, `blocked` or `nothing_to_migrate`. Moving a vault is a separate offline upgrade step.

### `maintenance.export`

This operation previews the return of a migrated vault's store collections to file-canonical files. It is
owner-only and writes nothing. For each collection it reports whether the store wrote it, and any blocker: a
collection the store created, one over a file-mode bound, a value with no file form, or a view changed by hand.
The vault verdict is `exportable`, `blocked`, `export_pending` or `nothing_to_export`. Applying the preview's
`plan_id` with a `why` is the owner's offline step. The CLI-only command is
`exomem collections export --plan-id ID --why REASON`, run with the service stopped. The service refuses the
apply.

### `maintenance.cutover`

This operation previews the move of every structured collection to SQLite-only storage. It is owner-only and
writes nothing. For each collection it names the steps the apply takes: `migrate` for a collection still in
files, `convert` for one with item files, `rewrite_strategy` for a summary collection, or none. It lists every
blocker with its fix and returns a `plan_id`. Applying that `plan_id` with a `why` is the owner's offline step.
The CLI-only command is `exomem collections cutover --plan-id ID --why REASON --backup-to FILE`, run with the
service stopped. The apply takes and verifies a backup first, checks each collection's rows after its step, and
resumes after a crash when run again with a new `--backup-to` file. The preview counts, for each collection, the
items that carry a body. After the apply, read that body with `read_page` on the old item path, or with a
`query` that names the row.

### Sidecar collapse (`maintenance.fix`)

`maintenance.fix` also collapses media sidecars that accumulated nested copies of themselves (audit category
`duplicated_sidecar`). Report that category alone with
`inspect_memory(operation="maintenance.audit", arguments={"categories": ["duplicated_sidecar"]})`. The collapse
keeps the longest surviving `## Extracted text`. For a sidecar whose top-level block a re-render blanked, that is
the one buried in a nested copy. It refuses any rewrite that would leave less transcript than it found.
Frontmatter stays untouched, so a still-`pending` sidecar is re-extracted normally, and the recovered text is
only the fallback.

## Curation

Curation is the governed multi-step lane. The agent authors a closed typed plan from explicit context, or
`curation.relocate` derives one. Evermatter validates and fingerprints the plan and records one exact-plan
approval. Each apply or resume then runs the plan's steps, one write hold per step, until the run ends or the
request's time is spent. Other writes land between steps.

1. Read the context with `inspect_memory(operation="curation.work_item")`. It takes `refs`, `paths` or a
   `review_ref`, and `hydration_recheck`, a same-identity continuation ordinal from 1 to 8. The eighth is
   closure-only and cannot bind another plan.
2. Propose with `manage_memory(operation="curation.propose", arguments={"plan": ...})`, or derive with
   `curation.relocate`. The reply names the plan fingerprint.
3. Preview with `curation.preview`, passing the `run_id`, and show the user the previewed plan. Ask for exact
   confirmation of that fingerprint.
4. Apply with `curation.apply`, passing the plan's fingerprint as `expected_plan_fingerprint`. `curation.status`
   reads a run.
5. A request that stops early reports its progress. Call `curation.resume` to continue the same approval.
   A refusal after committed steps reports the run as stopped beside its progress, not as an error. Follow the
   reply's `next_action`: resume after a busy vault, otherwise read the run's partial or blocked answer.
6. To undo a whole run, call `curation.propose_compensation`, preview it, and apply it with
   `curation.apply_compensation` after separate confirmation.

`curation.relocate` moves a set of pages into one project, renames them to their titles, or both, in one reviewed
plan. Pass `pages` (refs or vault paths) or one `review_ref`, and `to_project` to change project. `from_project`
names the key to replace when a page carries several. `folder` names where the notes of a new key live.
Evermatter derives every step:

- a move, or an in-place field edit, of each page;
- an edit of the project fields of each Source that a moved page cites;
- closing `repoint-links` steps that rewrite links to the old paths, a few referring pages per step.

A cited Source gains the new key. It loses the old key only when no page outside the set still cites it under that
key. A page without a typed folder, such as an entity, stays in its own folder: it takes its title-named path
there and changes its fields. The reply is a grouped summary with counts and samples, never the whole plan. Show
it as the preview, then approve its fingerprint with `curation.apply`. A proposal that runs short of the request's
time reports its progress: make the same `curation.relocate` call again, as `next_action` says, until it seals.
Each call keeps the steps that the earlier ones prepared. A `review_ref` batch is sized to what one plan holds,
and the reply counts the pages left for a later batch. A page the lane cannot change is listed with its code, and
the other pages still seal. A project key that the run registered stays registered after an undo.

Page steps cannot target raw Sources or Evidence, Planning, Records, workflow contracts, schema or admin state, or
trash internals. `move`, `delete` and `supersede` keep refusing Sources and Evidence. Two fold steps are the only
way the lane retires one of their pages. Each moves bytes to the trash only after it checks again that identical
bytes are held elsewhere.

- `fold-duplicate` folds one copy into a canonical page that holds identical bytes in the same lane. The canonical
  page gains the copy's families, import mappings and back-references. The copy is marked superseded, and every
  page that cited it now cites the canonical page. It refuses a copy across lanes, a raw-protected page and bytes
  that differ. The curation work item holds the exact step schema.
- `fold-recap-revisions` moves the superseded recap files of one episode into the history of its live page. Use it
  for an episode that still has many of them from the earlier layout.

A compensation plan restores the pages and bytes of either fold. Typed `append-record` and `update-record` steps
support Records collections that use `markdown-items` or `sqlite`. Store collections need their serving service.
If it is unavailable, a prepared run stays resumable. Planning steps remain unsupported.
