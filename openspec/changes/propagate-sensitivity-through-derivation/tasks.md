# Tasks

## 1. Registry and fields

- [ ] 1.1 Add the closed relation behaviour `inherits_sensitivity` and assign it in the core pack to `derived_from`, `evidenced_by`, `cites`, `refines`, `duplicates` and `supersedes`.
- [ ] 1.2 Declare `inherited_sensitivity` in the `page-fields` pack as a protocol-owned list field. Only the pre-plan step adds to it; only classify lowers it.

## 2. Carry-over at write time

- [ ] 2.1 Add the pre-plan carry-over step beside the names step, and call it from every write surface. The plan function refuses a short floor.
- [ ] 2.2 Accept `sources` on `edit_note`, `add_observation`, `add_record` and `edit_record`, merged into the page's `sources` field or the row's source refs. Keep each tool within its schema byte ceiling.
- [ ] 2.3 Treat an episode's `about` refs as its sources for the floor.
- [ ] 2.4 Teach agents, in the scaffold references and the capture skill, to name in `sources` every page whose content a write uses.

## 3. Live lineage

- [ ] 3.1 Add the `page_lineage` owner object with the next owner version, recomputed on the store thread in the transaction that changes classes or `inherits_sensitivity` edges. Chunk large raises under the 100 ms hold.
- [ ] 3.2 Give episode `about` a graph edge with the behaviour if the graph has none.
- [ ] 3.3 Index `item_sources` by source ref; join row lineage at decision time.
- [ ] 3.4 Key `admission_memo`, the store-mode membership map and collection view decisions on (sha256, generation); republish v4 item scopes when a generation moves.
- [ ] 3.5 Refuse a class on a page outside the graph with `SENSITIVITY_PAGE_UNTRACKED`; report already-classified pages outside the graph in the release check.

## 4. Disclosure

- [ ] 4.1 Complete the non-owner release door list from every caller of `snapshot` and `owned_page`. Declare each call site's reader with a reason, and add the test that fails on an undeclared one.
- [ ] 4.2 Supply the lineage currency proof from `read.py`; egress answers missing without it, under the existing underived bound.
- [ ] 4.3 Add the `sensitivity` selector and exclusion; refuse it on a vault without the store.
- [ ] 4.4 Casefold path selectors; make a create over an unreadable page refuse like an unwritable path.
- [ ] 4.5 Add the session read record to the governance sidecar with its retention, and list shown-but-unused classified pages in write results.

## 5. Proof and delivery

- [ ] 5.1 Measure a restricted first read and a hub raise before and after, on generated data at the load target.
- [ ] 5.2 Extend the twin-world canary rig with four synthetic canaries (direct, cited, episode `about`, uncited copy). Run a fresh `security-reviewer` re-attack lane on Opus; it must find no leak and no oracle before merge.
- [ ] 5.3 Run the full suite, the strict OpenSpec gate and the privacy gate. Obtain two independent reviews, merge, and verify the ordinary release.
