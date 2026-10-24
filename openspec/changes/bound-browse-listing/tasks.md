## 1. Bounded listing

- [x] 1.1 Let `reserved_paths.list_generic_tree` take a `select` callback that names the entries to open, stat and read, after the unchanged reserved-path and identity rules; add `list_generic_names`, which reads no file's bytes. Prove that unselected files are not read.
- [x] 1.2 Page `list_directory.list_directory`: sort the names, resume after the cursor key, apply the exclusion rule, the reserved-path rule and `keep` while filling, stop after `limit + 1` visible entries, and read bytes and frontmatter only for the returned entries.
- [x] 1.3 Add `limit` and `cursor` to `op_browse_memory`. Clamp `limit` to 1 to 1,000 (default 200), encode and verify the cursor, and return `truncated` and `next_cursor`. `op_list_directory` keeps the whole listing for its callers.
- [x] 1.4 Tests, one per behaviour that differs:
  - a large folder returns one bounded page with a cursor;
  - following cursors yields the complete listing once, in order;
  - a page that ends exactly at `limit` reports no further page;
  - a cursor for another path fails with `INVALID_CURSOR`;
  - withheld entries neither shorten a page nor appear in a cursor;
  - one page reads the bytes of at most `limit` files.
- [x] 1.5 Regenerate the tool-surface digest and its generated contracts and fixtures in one change, and run `openspec validate --all --strict`.

## 2. Release

- [x] 2.1 After the release is installed, report browse `list` p50 and p95 from the service call spans against the 2026-10-19 baseline. Record the browse latency bar's engineering choice in `bound-request-latency-across-surfaces`, or keep it pending with its reason.
      Evidence: the installed request ledger at the dispatch boundary, with browse `list` identified by the ledger's recorded argument digest. Baseline 2026-10-18 and 19: n=61, p50 1,975 ms, p95 114,448 ms, max 161,076 ms. With the bounded listing installed, 2026-10-21 to 24: n=40, p50 608 ms, p95 7,072 ms, max 10,822 ms, and 18 calls passed `limit` or `cursor`. The bar choice is recorded in `bound-request-latency-across-surfaces/design.md` under Open Questions: browse follows the programme-wide bar and currently fails it.
- [ ] 2.2 Ask the operator to refresh the ChatGPT plugin, so that ChatGPT can send `cursor`.
