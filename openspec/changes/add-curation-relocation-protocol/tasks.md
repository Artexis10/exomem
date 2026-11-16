## 1. Lineage, resolver and reserved paths

- [x] 1.1 Append `{path: <old path>}` to `moved_from` through `move_file`'s `content_transform` for every Markdown page with a frontmatter block; read legacy `reclassified_from` entries first; hold both keys in code with a one-line reason. Test: a note moved A→B→C lists A then B, and links to A and B resolve to C.
- [x] 1.2 Make `reclassify_source.py` write `moved_from` and make its revert remove the latest lineage entry without appending one. Test: a legacy Source keeps its `reclassified_from` bytes when it moves, and a revert leaves no entry naming the path it left.
- [x] 1.3 Add the derived `prior_paths(prior_path, path)` table beside `identities` in the sidecar and the store; refill it wherever an identity row is refilled. Test: a rebuild and an incremental refresh give the same rows for a moved page.
- [x] 1.4 Fold the relayout record rows of `adopt-root-vault-layout` into the refill, by `exomem_id` or by new path, and drop rows that a relayout revert reverses. Test: a page relaid out and then moved resolves from its relayout old path, before and after a rebuild.
- [x] 1.5 Add the prior-path and prior-stem steps to `_normalize_wikilink`, filtering visibility before counting claimants and reusing the `LinkTargets` batch pattern. Test: a prior path that only a withheld page claims answers byte for byte as a missing path.
- [x] 1.6 Report `prior_path_conflict` in the audit and leave a link with two claimants unresolved. Test: two pages listing one prior path give an unresolved link and one finding that names both.
- [x] 1.7 Treat claimed prior paths as taken in `unique_path` and the create-only checks; allow a move back into the page's own lineage; answer a withheld claimant with `WRITE_REFUSED`. Test: a create at a reserved path refuses, and a move back commits.
- [x] 1.8 Keep the lineage claimant when a file without an `exomem_id` sits at a reserved path, and report it as `reserved_path_occupied`. Test: an editor-created empty file at the old path does not take its links.
- [x] 1.9 Prove store-mode parity for lineage and prior-path resolution. Test: the A→B→C case gives the same resolution with the store bound as in file mode.

## 2. Governed pointer write

- [x] 2.1 Add the `page-fields` fixed-pack registry with its vault overlay and a closed `role` attribute, following the `sync-providers` pattern; ship the eight pointer fields. Test: an overlay that retires a shipped entry reports a finding and the field stays a pointer.
- [x] 2.2 Implement the pointer write: caller-declared spans, a parsed before/after span diff, same-`exomem_id` proof for link spans, and `APPEND_ONLY` for any other difference. Test: only the declared spans of a Source differ and its body, provenance and media hashes are unchanged.
- [x] 2.3 Refuse a link repoint whose new target resolves to another `exomem_id`, and a change to an undeclared field or to body text. Test: each crafted change refuses and leaves the page's bytes unchanged.
- [x] 2.4 Write one create-only, owner-only record per pointer write, with its reason, and add its revert. Test: a revert restores the exact before bytes, and a revert after later work refuses.
- [ ] 2.5 Require a curation plan approval or a matching preview digest for a pointer write to a page the call did not name, refusing with `POINTER_PREVIEW_REQUIRED`; the call approves writes to the page it names. Test: a referrer repoint without the digest changes nothing, and a direct move of a Source commits without one.
- [x] 2.6 Narrow the `APPEND_ONLY` refusal in `set_frontmatter_field.py` so a registry pointer field passes to the pointer write as a value-span patch that sets no `updated`; its preview returns the old and new value. Test: a Source's `project` correction changes only that span, and a `title` patch still refuses.

## 3. `move_file`

- [x] 3.1 Give each referrer one preview outcome and return a digest of the outcomes and bindings; list only pages a restricted mover may see. Test: a preview lists a writable note, a Source and a Records row with their three outcomes.
- [x] 3.2 Repoint append-only referrers of a page with an `exomem_id` through the pointer write in the move's batch; keep `APPEND_ONLY` for a page without one; move the `ingested_into` case onto the pointer write. Test: an Evidence body link names the new path and every other byte is unchanged.
- [x] 3.3 Add the optional preview digest to the `manage_memory_file` move and reclassification, and update the pinned tool schemas. Test: a move that repoints a Source referrer without the digest refuses with `POINTER_PREVIEW_REQUIRED`, and a promotion that repoints nothing commits without one.
- [x] 3.4 Make reclassification repoint its append-only referrers through the pointer write and make its revert restore them from their records. Test: a Source linked by path from an Evidence page reclassifies with only the link span changed, and a revert restores the Evidence page's exact bytes.
- [x] 3.5 Make `find_inbound_wikilinks` read the target's lineage from its bytes. Test: a delete check finds a link to a prior path while the derived index is stale.

## 4. Curation

- [ ] 4.1 Narrow the relocation predicate to `delete` and `recover`; update the pinned refusals in `tests/test_curation_execution.py` and `tests/test_curation_store.py`. Test: a plan with `delete` or `recover` still refuses with no run store, and a plan with `move` proposes.
- [ ] 4.2 Add the optional `fields` patch to `move`, limited to registry pointer fields, and drop `update_wikilinks` from the curation `move` schema. Test: a `fields` key that is not a pointer refuses with `CURATION_STEP_INVALID`.
- [ ] 4.3 Add `repoint-links` as the last step: seal every referrer of the plan's moves, bind pages earlier moves wrote at their sealed after hash, and commit all rewrites and pointer repoints in one batch with the witness. Test: two moved pages that link to each other propose without `CURATION_STEP_CONFLICT`.
- [ ] 4.4 Publish and flush the create-only intent record from a seam that runs once before the rename. Test: an outside rename back after the intent record blocks with `CURATION_RENAME_HISTORY_UNPROVABLE` and no rename runs.
- [ ] 4.5 Build the adopt branch: an adopt-mode semantic preflight over the virtual before corpus, an adopt commit that rechecks the rename-after guards, a graph rename epoch that reads the new path, and the reused catalog, batch and index steps. Test: after a crash past the rename, resume leaves the page indexed, cataloged and graphed at the new path.
- [ ] 4.6 Replace the placement stubs with the recovery state table and add a `BaseException` barrier before the intent record, after it, after the rename and after the batch. Test: at every barrier, recovery issues no second rename and loses no lineage entry.
- [ ] 4.7 Compensate `move` by restoring sealed pre-step bytes and `repoint-links` by restoring each rewritten page, using the pointer write's revert for append-only pages. Test: an undone rehome restores every moved page, referrer and Source to its pre-step hash.
- [ ] 4.8 Let an `edit` step with `patch_frontmatter` correct a registry pointer field on an append-only page as a pointer write, with no `updated` minting and exact-byte binding; compensate it through the revert. Test: a plan that corrects `project` on several Sources applies and its compensation restores each Source's before hash.
- [ ] 4.9 Run the end-to-end rehome workflow through the curation dispatcher: three notes, a `project` patch, a Source referrer and `repoint-links`. Test: every note sits at its new path with one lineage entry and the Source changed only in its link span.

## 5. Teaching

- [ ] 5.1 Return worked examples from `read_instructions(topic="curation.propose")`: a split with each child's `project`, a `move` with its preview outcomes, and a rehome plan whose `edit` steps correct its Sources' `project`. Test: proposal accepts each example's plan in a fixture vault.
- [ ] 5.2 Update the workflow skills under `src/exomem/_scaffold/_Schema/` that say Sources and Evidence stay immutable: content stays immutable and pointers change through governed moves. Keep the text generic. Test: `tests/test_scaffold_no_leak.py` passes.

## 6. Outcome check

- [ ] 6.1 On a disposable copy of a real vault, in an isolated state root with no running service, preview and apply a rehome plan through the connected-agent path. Record the referrer outcomes, the resolution of every old link, and the audit findings before and after.

## 7. Closure

- [ ] 7.1 Run `openspec validate --all --strict` before and after each artifact change and at archive.
- [ ] 7.2 After this change ships, archive it with `openspec archive`; then close `make-source-reclassification-reversible` Phase 2 with this change as its delivery evidence.
- [ ] 7.3 Confirm with the owner of `adopt-root-vault-layout` that its `command-surface` delta of "Append-Only Tree Relocation" is dropped and that its relayout record carries the agreed row shape, before either change archives.
