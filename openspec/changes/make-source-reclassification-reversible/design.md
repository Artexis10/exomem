## Context

Reclassification (`reclassify_source.py`) moves a Source through `move_file(update_wikilinks=True)`. It keeps the basename. It writes the latest prior path into the scalar `reclassified_from` with a regex over frontmatter text (`_set_scalar`, `_drop_scalar`), and nothing reads that field. `_refuse_unchosen_kind` refuses a destination kind of `other`. `move_file` refuses with `APPEND_ONLY` when the rewrite of an append-only referrer, or of the page's own links, reports a change (`move_file.py:556-578`, `:671-684`). `vault.rewrite_wikilinks_for_move` reports stem, aliased-stem and heading-stem links as changed even when its output equals its input. `_repoint_artifact` reserializes the whole frontmatter. The body-equality check runs after commit (`reclassify_source.py:560-565`).

## Goals / Non-Goals

**Goals:** move the legacy `Sources/Other` files without changing any append-only byte; make every move reversible; show every referrer outcome before a move.

**Non-Goals:** rewriting append-only content; redirecting writes; inferring a classification that was never recorded.

## Decisions

### 1. A referrer changes only when its bytes change

`rewrite_wikilinks_for_move` reports a change only when the rewritten text differs from the input. The append-only guard keeps refusing any real write to an append-only file. After this fix it refuses only path-form links, which are the links that would dangle.

### 2. Canonical frontmatter holds a reversible history

`moved_from` holds the history: a list of entries, oldest first. Each entry records the prior path, kind and domain. Phase 1 shipped the list as `reclassified_from`. That spelling stays readable as entries older than every `moved_from` entry, and no writer renames the key. A legacy scalar reads as one entry whose classification is unknown. The field names and entry keys are closed protocol tokens. `add-curation-relocation-protocol` makes every move append to the same list.

Reclassification writes this field, the classification fields and the companion pointers (`evidence_file`, `data_file`, `raw_protection.artifact_path`) by patching the source spans that the existing YAML loader reports. That replaces `_set_scalar`, `_drop_scalar`, which are regexes over a parsed language (C6), and the whole-frontmatter reserialization in `_repoint_artifact`. The transform proves body-byte equality before the transaction commits.

### 3. Revert restores the latest entry

A revert mode restores the latest entry's path, kind and domain through the same guarded transaction, and removes that entry. Restoring a recorded `other` classification is allowed; choosing `other` as a new classification stays refused. An entry with an unknown classification cannot be reverted automatically and is reported. A revert that would reach a path occupied by another page is refused.

### 4. The preview reports every referrer outcome

The preview lists the destination, the mutable referrers it would rewrite, and the append-only referrers it leaves unchanged, grouped by link form. It also lists each refusal with its cause. A restricted mover sees only referrers it can see. A refusal names no withheld page, so the answer matches a vault without withheld pages.

### 5. Phase 2 is delivered by `add-curation-relocation-protocol`

`add-curation-relocation-protocol` delivers history-aware resolution for every page with lineage, under its own requirements. It keeps every write target literal, reserves each claimed prior path, and resolves graph, audit and inbound owners through the same resolver. It widens the claims below from Sources to every page with lineage. Its history store is a derived `prior_paths` table beside `identities`, refilled from page bytes. After it ships, reclassification repoints a path link in an append-only page through the governed pointer write, like any move. Its revert restores that page from the pointer-write record. Lineage stays the safety net for every reference that the move does not write.

The constraints first written for this phase follow. The delivered design supersedes them where it differs.

- Only read-only commands resolve through history, through a dedicated read-only resolver. `resolve_visible_identifier`, `prepare_page_read` and every write path stay literal.
- A claim is admitted only from a page under `Sources/` or `Evidence/` and only for a Markdown path under `Sources/`.
- A move or creation onto a path that admitted history claims is refused, decided over the mover's view.
- Derived link owners (inbound links, graph, audit) either resolve history or state the degradation explicitly.
- Completeness is proved only when `delta_since` is complete and no changed, deleted or pending path lies under a claimant tree. A restart does not make every Sources read warm.
- The history store is chosen then, between extending `.refs.sqlite` and adding history to the shared broad `WikilinkResolver`, which is already tied to the vault checkpoint.

The delivered design had its own review in `add-curation-relocation-protocol`.

### 6. A large managed preview produces a paged approval report

Bound candidate collection and response size in the interactive request. A positive overflow starts or rejoins a read-only preview through the maintenance job runner. A current publication must still prove the candidate set. Contention or incomplete evidence yields the existing warming response.

Use the existing job receipt and a report-local cursor to report one bounded page at a time. Publish the summary and approval digest after the job proves the complete report. Admit the managed report only while the owner's authority covers every contributor. An owner-scoped rule or Access exclusion can withhold a contributor even if it does not appear in the report. Refuse managed previews under those conditions before counting references.

Bind page delivery to live owner credentials, the governing policy, the connector ceiling, and the erasure set. Preview and paged apply decide the same candidate paths before counting or planning effects. Ordinary edits mark the completed snapshot as old. When release depends on page membership, a changed or unknown snapshot needs a fresh preview. The release filter checks guarded report and continuation paths without enumerating their directories. The lifecycle verifier reuses its validated set until physical evidence changes and checks the request deadline during a refresh. Refuse the whole response if the filter changes any value. A failed or interrupted job cannot expose staged rows.

The paged approval digest binds the Source, destination, full referrer report, and pointer-write proof. The writer checks it against the current plan and rechecks the proof before saving pointer records and renaming the Source. The existing inline approval digest stays readable.

The product load target is 20,000 pages, about 100,000 units, and 120,000 edges, with a hub of 10,000 links. Use the existing request deadline and maintenance receipt response bound. Prove that a large preview leaves Records responsive and can report all 10,000 referrers over bounded pages.

## Controls and costs

- The append-only guard prevents any write to Sources and Evidence. After Decision 1 it fires only for links that would otherwise dangle. After Decision 5 a path link is repointed through the governed pointer write, so the guard holds a file back only when the source carries no `exomem_id`. When it fires, the file stays in place and the preview lists it. The operator pays one deferred file.
- Revert refusal for an unknown classification prevents inventing a classification. When it fires, the owner reclassifies explicitly. The owner pays one step.

## Migration Plan

1. Ship Phase 1. No page changes until a reclassification runs.
2. After release and installation on the personal service, preview the whole drain.
3. Apply the moves that the preview clears in reviewed batches through governed reclassification, and check references after each batch.
4. Roll back a file with revert. Move the files that the preview held back after `add-curation-relocation-protocol` ships.
