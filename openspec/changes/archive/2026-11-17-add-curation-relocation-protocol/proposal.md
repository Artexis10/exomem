## Why

An owner cannot rename or rehome a page through governed curation today. Three
mechanisms block it:

- Curation refuses every `move`, `delete` and `recover` step with
  `CURATION_RENAME_HISTORY_UNPROVABLE`. The refusal waits for a durable
  parent-namespace generation token that no supported filesystem provides, so
  it is permanent in practice.
- A move that would rewrite a link inside a `Sources/` or `Evidence/` page
  refuses with `APPEND_ONLY`. A note that captured Episodes link by path can
  therefore never move.
- The resolver keeps no record of a page's previous paths. A link that Exomem
  cannot rewrite dangles after a move.

The owner ruled on 2026-11-16 that Sources and Evidence keep their content and
provenance unchanged, and that their pointers change only through a governed,
approved, proven, audited and reversible write. Approval happens in
conversation with the connected agent.

## What Changes

- Each move of a Markdown page with frontmatter appends the old path to a
  `moved_from` list in that page, in the same commit. `reclassified_from`
  stays readable as the legacy spelling.
- The resolver gains a prior-path step and a prior-stem step. Reads follow a
  page's lineage. The relayout record of `adopt-root-vault-layout` is a second
  lineage source under the same rules, because a bulk relayout writes no
  `moved_from`. Write targets stay literal. A prior path that only a hidden
  page claims answers as missing. Two live claimants leave the link unresolved,
  and the audit reports it.
- A prior path stays reserved while a live page claims it. A page may move back
  to its own old path. A file without an `exomem_id` at a reserved path does
  not take the address.
- A governed pointer write may change only the pointer spans of a `Sources/` or
  `Evidence/` page: link targets that resolve to the same `exomem_id`, and
  frontmatter fields that a new `page-fields` vocabulary registry declares with
  `role: pointer`. A parsed span diff proves that no other byte changed. A
  create-only record keeps each old span, and a revert restores the exact
  bytes. A write to a page that the call did not name needs a curation plan
  approval or the digest of the preview that listed it.
- `move_file` repoints append-only referrers of a page with an `exomem_id`
  through that write instead of refusing with `APPEND_ONLY`. Its preview gives
  each referrer one outcome. Every governed move, reclassification included,
  follows this rule in file and store mode.
- `edit_memory`'s `patch_frontmatter` corrects a pointer field of a `Sources/`
  or `Evidence/` page in place through the same write, for example a Source's
  `project`. Every other field keeps the `APPEND_ONLY` refusal. A curation
  `edit` step carries the correction, so no new step kind is needed.
- Curation keeps refusing `delete` and `recover`, and runs `move`. A `move`
  step takes an optional `fields` patch of pointer fields. A new final step,
  `repoint-links`, rewrites the plan's referrers in one batch.
- A curation move publishes a create-only intent record before its rename.
  Recovery never issues a rename. It adopts a completed rename and publishes
  the rest of the move, or it blocks.
- `read_instructions(topic="curation.propose")` teaches the split, move and
  rehome plan shapes with worked examples.
- The token, candidate and authorization protocol that this change formerly
  specified is withdrawn.

## Capabilities

### New Capabilities

- `page-field-registry`: the `page-fields` vocabulary registry. It declares
  which frontmatter fields are pointers.

### Modified Capabilities

- `governed-curation-lane`: `move` runs through an intent record and
  rename-free recovery; `move` takes a pointer `fields` patch; the new
  `repoint-links` step; an `edit` step may correct a pointer field on an
  append-only page; compensation of each; `delete` and `recover` stay
  refused; curation teaches its plan shapes.
- `command-surface`: page lineage and its relayout source, prior-path
  resolution, reserved prior paths, the governed pointer write, in-place
  pointer correction, per-referrer move outcomes, and the append-only
  relocation rule for both modes.

## Impact

- Code: `curation.py` (relocation predicate, move preparation, `repoint-links`,
  intent record, recovery state table, compensation), `move_file.py`
  (lineage transform, referrer outcomes, pointer repoint, preview digest,
  adopt-mode publication), `semantic_writes.py` (adopt-mode move preflight and
  commit), `graph_sync.py` (rename epoch read from the destination),
  `vault.py` (resolver steps, `unique_path`, create-only checks),
  `memory_refs.py` and `vault_store/derive.py` (`prior_paths` index),
  `reclassify_source.py` (writes `moved_from`, repoints append-only
  referrers, reverts them from records), `set_frontmatter_field.py` (pointer
  correction on append-only pages), a new `page-fields` registry module and
  shipped pack.
- Public contract: the `manage_memory_file` move and reclassification gain an
  optional preview digest. A missing one refuses with
  `POINTER_PREVIEW_REQUIRED` when the call repoints an append-only page that it
  did not name. The curation plan schema gains `repoint-links`, and `move` gains
  `fields` and loses `update_wikilinks`. The pinned MCP tool schemas change.
- Data: no page changes until a move runs. Existing `reclassified_from` values
  stay readable and are never rewritten to rename the key.
- Scaffold: the workflow skills that say Sources and Evidence stay immutable
  learn that content stays immutable and pointers change through governed
  writes.
- Coordination: `make-source-reclassification-reversible` takes the
  `moved_from` name, repoints append-only referrers, and records this change
  as its Phase 2 delivery. It archives after this change ships.
  `adopt-root-vault-layout` keeps its relayout record in the vault as the
  second lineage source. Its `command-surface` delta of "Append-Only Tree
  Relocation" moves into this change.
