# Tasks

## 1. Implementation

- [x] 1.1 Create the Records and Planning query link projector without the cold walk while a corpus generation is coming, decided by one function from the borrow's decline reason, and keep the bounded walk when none is coming.
- [x] 1.2 Add `unresolved_links` to the Records reply egress and to the Planning reply keys, null when every returned link field was checked, with validation against the manifest's link fields.
- [x] 1.3 Project only the requested columns' fields, use the staged projection for aggregates, and project the row of a `latest` answer.
- [x] 1.4 Start one single-flight background corpus warm when a query's borrow finds no corpus generation, and include it in the warm quiesce seam.
- [x] 1.5 While a generation is coming, name each returned link field in `unresolved_links`, and refuse a selection on a link field, inline or in a saved view, with the retryable `LINK_INDEX_WARMING`.
- [x] 1.6 Start or join the warm on every decline reason that counts as coming, and count none as coming when warms are off, so every refusal has a warm that ends it.
- [x] 1.7 Name only the link fields of the caller's field plan in a refusal, and read a query's selection columns through one function shared with the store's staged selection.
- [x] 1.8 After a bounded walk that stopped at its cap, name each returned link field in `unresolved_links` and refuse a selection on a link field with `LINK_INDEX_INCOMPLETE`.

## 2. Proof

- [x] 2.1 Test at the service entry point that no query walks the vault for links while a generation is coming, that a returned link field is named in `unresolved_links`, and that a link filter is refused for a retry.
- [x] 2.2 Test at the service entry point that a query without a corpus answers with `unresolved_links`, and that the next query after the warm resolves the link.
- [x] 2.3 Test at the Records entry point that a link filter, a link distinct and a saved view with a link filter are refused for a retry while a generation is coming and answer once it is published, and that a process whose vault change events are not live walks as before.
- [x] 2.4 Measure query CPU before and after on a vault with 1,000 pages per folder, with and without a published corpus, while the warm runs, and for a link filter while the warm runs.
- [x] 2.5 Rerun the Records, Planning and collection store query modules.
- [x] 2.6 Test at the service entry point that a link filter refused after a page write answers once the warm that the refusal started is done, without walking.
- [x] 2.7 Test that with warms off a query walks, that a capped walk refuses a link filter and names the returned link fields, and that a profile view's refusal names no field the caller's plan withholds.

## 3. Delivery

- [ ] 3.1 Review the exact change independently, with disclosure, egress and the query budget as the main risks; resolve findings.
- [ ] 3.2 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`.
