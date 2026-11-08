## Why

A vault can hold the same bytes many times. Nothing prevents it, reports it or removes it.

A read-only measurement of one personal vault (release 0.165.0) found:

- 79 groups of identical binary originals, with 121 extra copies. 67 groups span folders.
- 7 pairs of Evidence pages with identical extracted text.
- 56 groups of Sources that share a URL, with 76 extra Sources.
- 5,151 recap files for 861 episodes. 4,290 are superseded revisions, and 339 titles repeat.
- 44 citations of superseded recap revisions, from 32 compiled pages to 36 revisions.
- No byte-identical extra Markdown page outside the trash.

Three gaps cause this:

- Exact-byte checks cover two narrow cases. Evidence preservation checks one destination folder. Import checks one original path.
- `capture_source` checks nothing. No audit reports duplicates.
- The curation lane cannot retire a Source or an Evidence page. Each recap revision is a new file.

## What Changes

- Add the capability `duplicate-handling`. A duplicate is identical bytes: the original file bytes, or the captured text of a Source. Nothing near counts.
- Add a derived, rebuildable hash index. Every hit is re-hashed before use, so the index is never authority. The duplicate check runs under the vault mutation boundary.
- `capture_source` reuses an earlier Source with identical text and returns `duplicate_of`. It writes a new Source for a repeated URL with different text and returns a `same_url` advisory.
- `preserve_evidence` and `preserve_artifacts` store identical bytes once in the whole vault. One Evidence page lists every evidence family. The `already_stored` outcome stays compatible.
- Import stores identical originals once, within one tree and against the vault. It records every source path on the page. A re-run writes nothing.
- The audit reports `duplicate_content` and `duplicate_source_url`.
- Two curation step kinds, `fold-duplicate` and `fold-recap-revisions`, retire copies in reviewed batches. Compensation undoes each batch from its binding manifest.
- A repeat recap revises the episode's one page in place. The recorder first keeps the previous text as a create-only compiled-history entry. History is never rotated. Only the recorder may revise a Source in place.
- A reviewed migration moves existing superseded revisions into history. It keeps every revision that a live page references as a file.
- The follow-ups of the earlier artifact hand-over change (#2537) are in scope. The duplicate check moves under the writer guard. The index replaces the per-adoption scan of `Sources/<kind>`.

## Capabilities

### New Capabilities

- `duplicate-handling`: exact-byte identity, the derived hash index, the duplicate audit categories, and the fold rules.

### Modified Capabilities

- `durable-source-capture`: reuse by payload with `duplicate_of`, and the `same_url` advisory.
- `client-artifact-preservation`: store-once across the vault, the family list, and the index for adoption hand-overs.
- `adoption-studio`: store-once within a tree and against the vault, `also_imported_from`, and idempotent re-runs.
- `governed-curation-lane`: the two fold step kinds, their manifests, and their compensation.
- `agent-bootstrap-contract`: one page per episode, history, the recorder exception, coverage after revision, and the layout migration.

## Impact

- Code: `add.py`, `episode_memory.py`, `episode_capture.py`, `episode_coverage.py`, `compiled_history.py`, `preserve.py`, `client_artifacts.py`, `adoption_plan.py`, `adoption_run.py`, `audit.py`, `curation.py`, `lexstore.py` (one derived column) and the scaffold skill references.
- No new tool. Results gain optional fields: `duplicate_of`, `same_url`, `families`, `family_added`, `mapped` and `duplicate_check_unavailable`. Frontmatter gains `families`, `also_imported_from` and `duplicate_of`.
- Behaviour change: a repeated text capture no longer creates a second file. A repeated recap no longer creates a second file.
- Not in scope: merging near-duplicates, cross-vault consolidation (`add-governed-vault-consolidation`), retitling compiled notes that share a title, folding duplicate own-notes, and archive member pools.
- Live folds of a real vault stay separate operations. Each batch needs a disposable-copy check, the owner's confirmation and a kept undo manifest.
