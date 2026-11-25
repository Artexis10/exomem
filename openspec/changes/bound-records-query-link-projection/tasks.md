# Tasks

## 1. Implementation

- [x] 1.1 Create the Records and Planning query link projector without the cold walk, and name each returned link field in `unresolved_links` when no index is borrowed.
- [x] 1.2 Add `unresolved_links` to the Records reply egress and to the Planning reply keys, null when every returned link field was checked, with validation against the manifest's link fields.
- [x] 1.3 Project only the requested columns' fields, use the staged projection for aggregates, and project the row of a `latest` answer.
- [x] 1.4 Start one single-flight background corpus warm when a query's borrow finds no corpus generation, and include it in the warm quiesce seam.

## 2. Proof

- [x] 2.1 Test at the service entry point that no query walks the vault for links, and that a returned link field is named in `unresolved_links`.
- [x] 2.2 Test at the service entry point that a query without a corpus answers with `unresolved_links`, and that the next query after the warm resolves the link.
- [x] 2.3 Move the tests that read memory references or bare titles through a query onto a published corpus, and rerun the Records, Planning and collection store query modules.
- [x] 2.4 Measure query CPU before and after on a vault with 1,000 pages per folder, with and without a published corpus, and while the warm runs.

## 3. Delivery

- [ ] 3.1 Review the exact change independently, with disclosure, egress and the query budget as the main risks; resolve findings.
- [ ] 3.2 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`.
