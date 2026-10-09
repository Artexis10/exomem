## Context

Reclassification (`reclassify_source.py`) moves a Source through `move_file(update_wikilinks=True)`. It keeps the basename. It writes the latest prior path into the scalar `reclassified_from` with a regex over frontmatter text (`_set_scalar`, `_drop_scalar`), and nothing reads that field. `_refuse_unchosen_kind` refuses a destination kind of `other`. `move_file` refuses with `APPEND_ONLY` when the rewrite of an append-only referrer, or of the page's own links, reports a change (`move_file.py:556-578`, `:671-684`). `vault.rewrite_wikilinks_for_move` reports stem, aliased-stem and heading-stem links as changed even when its output equals its input. `_repoint_artifact` reserializes the whole frontmatter. The body-equality check runs after commit (`reclassify_source.py:560-565`).

## Goals / Non-Goals

**Goals:** move the legacy `Sources/Other` files without changing any append-only byte; make every move reversible; show every referrer outcome before a move.

**Non-Goals:** rewriting append-only pages; redirecting writes; inferring a classification that was never recorded.

## Decisions

### 1. A referrer changes only when its bytes change

`rewrite_wikilinks_for_move` reports a change only when the rewritten text differs from the input. The append-only guard keeps refusing any real write to an append-only file. After this fix it refuses only path-form links, which are the links that would dangle.

### 2. Canonical frontmatter holds a reversible history

`reclassified_from` becomes a list of entries, oldest first. Each entry records the prior path, kind and domain. A legacy scalar reads as one entry whose classification is unknown. The field name and entry keys are closed protocol tokens.

Reclassification writes this field, the classification fields and the companion pointers (`evidence_file`, `data_file`, `raw_protection.artifact_path`) by patching the source spans that the existing YAML loader reports. That replaces `_set_scalar`, `_drop_scalar`, which are regexes over a parsed language (C6), and the whole-frontmatter reserialization in `_repoint_artifact`. The transform proves body-byte equality before the transaction commits.

### 3. Revert restores the latest entry

A revert mode restores the latest entry's path, kind and domain through the same guarded transaction, and removes that entry. Restoring a recorded `other` classification is allowed; choosing `other` as a new classification stays refused. An entry with an unknown classification cannot be reverted automatically and is reported. A revert that would reach a path occupied by another page is refused.

### 4. The preview reports every referrer outcome

The preview lists the destination, the mutable referrers it would rewrite, and the append-only referrers it leaves unchanged, grouped by link form. It also lists each refusal with its cause. A restricted mover sees only referrers it can see. A refusal names no withheld page, so the answer matches a vault without withheld pages.

### 5. Phase 2 is conditional

The live drain preview counts the Sources whose append-only referrers name them by path. If that count is zero, the drain completes in Phase 1 and Phase 2 is withdrawn. If it is not zero, Phase 2 adds history-aware resolution under these constraints:

- Only read-only commands resolve through history, through a dedicated read-only resolver. `resolve_visible_identifier`, `prepare_page_read` and every write path stay literal.
- A claim is admitted only from a page under `Sources/` or `Evidence/` and only for a Markdown path under `Sources/`.
- A move or creation onto a path that admitted history claims is refused, decided over the mover's view.
- Derived link owners (inbound links, graph, audit) either resolve history or state the degradation explicitly.
- Completeness is proved only when `delta_since` is complete and no changed, deleted or pending path lies under a claimant tree. A restart does not make every Sources read warm.
- The history store is chosen then, between extending `.refs.sqlite` and adding history to the shared broad `WikilinkResolver`, which is already tied to the vault checkpoint.

Phase 2 requires its own design update and review before implementation.

## Controls and costs

- The append-only guard prevents any write to Sources and Evidence. After Decision 1 it fires only for links that would otherwise dangle. When it fires, the file stays in place and the preview lists it. The operator pays one deferred file.
- Revert refusal for an unknown classification prevents inventing a classification. When it fires, the owner reclassifies explicitly. The owner pays one step.

## Migration Plan

1. Ship Phase 1. No page changes until a reclassification runs.
2. After release and installation on the personal service, preview the whole drain.
3. Apply the moves that the preview clears in reviewed batches through governed reclassification, and check references after each batch.
4. Roll back a file with revert. Decide Phase 2 from the count of files that the preview held back.
