## 1. Typed links and the citation seam (Batch B)

- [x] 1.1 Canonicalise `link` values on create, append, update, bulk upsert and import through the read-side link projector with the writer's visibility; warn on unresolved or ambiguous values; keep a link to a heading or a block as written, with a warning; report each changed value as supplied and as stored in the write reply.
  - Evidence: `collection_store/link_values.py`, hooks in `record_inputs.py` (append, update) and `bulk_inputs.py` (bulk upsert and import), `_LinkProjector.resolve` in `record_governance.py`. Tests: `tests/test_collection_link_values.py` (rename-proof ref, ambiguous title, withheld equals missing, withheld twin, append, update, bulk upsert and import).
- [x] 1.2 Index canonical link refs in `item_references` from the canonical item transaction and the existing backfill. Seed `item_reference_state` for link fields, and report title and path values as not indexed in `inspect`.
  - Evidence: `collection_store/reference_index.py` (`link_match`, link entries and the `UNINDEXED` tally in `maintain_many`, `backfill`, `unseeded`, `revise`, and `link_counts`), `query_engine/reference_report.py` (`check_links`), `structured_collections.py` (`LinkDeclaration`, `indexed_fields`). Tests: `test_link_entries_rebuild_exactly_from_rows_and_inspect_counts_what_is_indexed`, `test_inspect_counts_link_values_from_the_index_with_the_same_statements_at_any_size` and `test_a_derived_collections_link_fields_read_as_never_indexed_not_as_warming` in `tests/test_collection_link_values.py`.
- [x] 1.3 Add the manifest attribute `relation` on `link` fields, validated against registry relations whose `origins` include `collection_link`. Add that origin to `cites`, `evidenced_by`, `derived_from` and `links_to` in the core pack.
  - Evidence: `FieldSpec.relation` in `structured_collections.py`, `collection_store/link_declarations.py`, `relation_registry.py`, `vocabulary/packs/core/relations.yaml`. Test: `test_a_relation_is_refused_unless_its_origins_include_collection_link` in `tests/test_collection_link_values.py`.
- [x] 1.4 Implement `inbound_citations(ref, caller, limit, more)` and its batch form: indexed reads, two-ended visibility, the visible count (a lower bound past the count limit), a continuation and a `warming` state.
  - Evidence: `src/exomem/page_citations.py` (`inbound_citations`, `inbound_citations_batch`), `reference_index.link_fields`. Tests: `tests/test_page_citations.py` (ready list and continuation, warming, withheld page equals missing page, withheld row absent from items and count, two index statements for a batch, the same statements however many governed rows cite a page, no published corpus reads as `unavailable` without a vault walk, non-page ref unavailable).
- [x] 1.5 Add `results` to Planning, with `no_result_linked` on a done-class transition read from the registry. Resolve `motivation` at read time.
  - Evidence: `planning.py` (`_OPTIONAL`, `_MAX_RESULTS`, `_validate_declared_text`, `completion_advisory`; legacy and store-backed paths), `writer._item_carriers`; the Planning inspection admits `links` (`_OPTIONAL_INSPECTION_KEYS`). Tests: `tests/test_planning_results.py` (round trip and the citation on the page and the row, 17 refused, `no_result_linked` on a done transition and not on a drop, a linked result or a link-less manifest, `motivation` declared with link items is indexed and stays memory refs only, an update that names the stored result by its title is refused as no change). Read-time resolution of canonical record and plan refs: `reference_resolution.resolve_row`, `_LinkProjector`, tests in `tests/test_collection_link_values.py`.
- [x] 1.6 `read_page` returns `cited_by`.
  - Evidence: `commands.py` (`_with_cited_by`, `_cited_by_continuation`, `op_read_memory`), `page_citations.py` (`applies`, `without_page`, hex cursor). Tests: `tests/test_read_page_cited_by.py` (list and onward paging without a second page read, warming, scan and store-less vault carry none, withheld page equals missing page while warming and once built, malformed cursor refused); row targets in `tests/test_page_citations.py`.
- [x] 1.7 `recall_context` and `search_memory` add `cited_by_count` and up to three citations to the top ten page hits through one batched call.
  - Evidence: `page_citations.for_hits`, `commands._cite_search_hits` (`search_memory`), `commands._cite_packet_leads` (`recall_context` turn-slice anchors, charged to `budget.used_chars`). Tests: `tests/test_recall_citations.py` (count and first citations, no row as a hit, warming state, withheld collection absent, two `item_references` statements for 13 hits with ten cited, a purpose-narrowed rule on a page read, a search and a recall, a citation fault leaves the read whole, a lead inside the budget and fresh on the next request).
- [x] 1.8 Tests at the public tool path:
  - title-to-ref canonicalisation, which survives a page rename;
  - an ambiguous-title warning;
  - withheld and missing are identical for rows, pages and collections;
  - `warming` is never shown as empty;
  - `results` with `no_result_linked`;
  - `cited_by` paging;
  - a query-count test of a fixed number of citation statements per recall response.
  - Evidence (each bullet in order, tests at the writer, `read_page`, `search_memory` and `recall_context` entry points):
    - Title to ref and rename: `test_a_title_is_stored_as_the_ref_that_survives_a_page_rename`, `test_every_write_path_stores_the_ref_and_reports_it_as_supplied_and_stored` in `tests/test_collection_link_values.py`.
    - A link to a heading keeps its anchor: `test_a_heading_link_is_kept_as_written_with_a_warning_and_an_alias_alone_becomes_the_ref` in the same file.
    - Ambiguous title: `test_an_ambiguous_title_is_kept_and_the_commit_warns_with_at_most_three_candidates` and `test_a_withheld_twin_neither_makes_a_title_ambiguous_nor_appears_in_a_warning` in the same file.
    - Withheld equals missing, rows: `test_a_row_is_cited_by_the_rows_that_link_to_it_and_a_withheld_row_answers_like_a_missing_one` and `test_a_withheld_row_of_a_collection_the_caller_can_read_is_absent_from_items_and_count` in `tests/test_page_citations.py`. Pages: `test_a_withheld_page_answers_exactly_like_a_missing_one` there, `test_a_continuation_for_a_withheld_page_answers_exactly_like_one_for_a_missing_page` in `tests/test_read_page_cited_by.py`, and `test_a_withheld_page_warns_exactly_like_a_missing_one`. Collections: `test_a_row_in_a_collection_the_caller_cannot_read_leaves_a_page_as_if_no_row_cited_it` in `tests/test_recall_citations.py` (same `cited_by` on a page read and the same recall hit as a page nothing cites), and `test_a_row_of_a_collection_the_caller_cannot_read_is_absent_from_items_and_count` in `tests/test_page_citations.py`.
    - Warming is never empty: `test_a_warming_index_never_reads_as_an_empty_list` (`tests/test_page_citations.py`), `test_a_warming_index_reads_as_warming_never_as_no_citations` (`tests/test_read_page_cited_by.py`), `test_a_warming_index_says_so_on_the_hit_instead_of_a_zero_count` (`tests/test_recall_citations.py`).
    - `results` with `no_result_linked`: `tests/test_planning_results.py`.
    - `cited_by` paging: `test_a_page_read_lists_its_citing_rows_and_pages_onward_without_reading_the_page_again` and `test_a_ready_page_lists_each_citing_row_with_what_a_reader_needs`.
    - A fixed citation query count per response: `test_a_batch_reads_the_index_twice_for_all_its_pages` and `test_a_read_runs_the_same_statements_however_many_governed_rows_cite_the_page` (`tests/test_page_citations.py`), and `test_a_response_reads_the_index_twice_however_many_page_hits_it_has` (`tests/test_recall_citations.py`).
- [x] 1.9 Before/after latency on a generated vault of 100,000 rows and 10,000 pages for `read_page`, `recall_context` and an edit by id, on the same instrument. Bounds: the Records load target and warm public p95 under 1 s.
  - Size measured: 21,000 rows with 10,000 pages, not 100,000 rows. The supported write path could not seed 100,000 rows: `bulk_upsert_records` (500 rows per call) slowed from about 56 rows per second at 5,000 rows to about 11 at 20,000 on both arms. That slowdown belongs to the existing write path and is tracked as its own defect. The proof stands at 21,000 rows because the change adds only per-call work: one indexed citation lookup per response and one title lookup per link value written. Neither grows with the row count, and the 5,000 and 21,000-row pairs agree.
  - Instrument, the same for both arms: an MCP client over `exomem --transport stdio` on a store-mode vault, warm service, embeddings off, 30 samples per call, p50/p95 in milliseconds, each arm run alone. Before is the merge base `15a89af83`, after is this branch. Every tenth row links a page; the hot page has 50 citations and the cold page none.

    | Call | 5,000 rows before | 5,000 rows after | 21,000 rows before | 21,000 rows after |
    |---|---|---|---|---|
    | `read_page`, 50 citations | 28.1 / 55.9 | 33.7 / 38.2 | 46.6 / 56.6 | 32.7 / 38.5 |
    | `read_page`, none | 23.6 / 28.5 | 31.5 / 36.7 | 42.8 / 104.1 | 31.3 / 33.2 |
    | `search_memory`, 4 hits | 25.5 / 27.8 | 35.9 / 39.4 | 28.0 / 37.5 | 34.1 / 39.1 |
    | `recall_context` | 323.3 / 352.8 | 337.9 / 373.7 | 426.8 / 1381.6 | 399.8 / 425.8 |
    | `query_records`, filter on the link field | 245.8 / 274.1 | 247.2 / 292.5 | 1428.2 / 1478.2 | 1425.3 / 1483.3 |
    | `query_records`, filter on the natural key | 34.2 / 39.9 | 36.8 / 44.9 | 89.7 / 267.9 | 88.8 / 262.0 |
    | `edit_record` by id, other field | 156.0 / 196.5 | 150.6 / 189.7 | 259.5 / 433.0 | 257.0 / 466.7 |
    | `edit_record` by id, link given as a title | 157.3 / 199.0 | 172.9 / 231.1 | 262.4 / 500.4 | 273.1 / 506.6 |

  - Row-governed citation reads, measured after review. The table above has no row-level policy. Under one, the first version admitted every link-bearing collection on every read, proved its release by deciding every row, then read each citing row's subject in its own statement.
  - Instrument, the same for both arms: `commands.op_read_memory` in one process on a store-mode vault with a published corpus generation; 15 samples after one warm call; p50/p95 in milliseconds. One summary Records collection holds the rows, and every row cites the page. A scope names one row by its ref, with a ceiling-0 rule for the external audience, so release varies by row for both readers. Before is `0ebaebf3e`, after is the fix.

    | Rows | Reader | Page every row cites, before | after | Page no row cites, before | after | Page read alone, before | after |
    |---|---|---|---|---|---|---|---|
    | 1,000 | owner | 87.2 / 92.5 | 91.4 / 101.2 | 81.8 / 90.2 | 23.4 / 31.0 | 24.6 / 27.6 | 17.3 / 20.5 |
    | 1,000 | external | 191.4 / 203.6 | 84.0 / 94.1 | 83.2 / 87.3 | 19.5 / 22.2 | 28.6 / 34.8 | 16.5 / 17.1 |
    | 5,000 | owner | 313.2 / 322.7 | 301.1 / 327.8 | 329.6 / 359.3 | 18.5 / 21.9 | 27.6 / 31.6 | 14.5 / 15.3 |
    | 5,000 | external | 882.8 / 964.8 | 320.2 / 334.6 | 350.6 / 401.1 | 19.0 / 19.4 | 29.0 / 34.6 | 17.6 / 21.2 |
    | 20,000 | owner | 1192.1 / 1377.2 | 1290.2 / 1479.5 | 1098.4 / 1133.3 | 24.0 / 29.9 | 19.6 / 23.6 | 22.1 / 28.8 |
    | 20,000 | external | 3264.3 / 3510.6 | 1263.1 / 1513.3 | 1234.2 / 1306.0 | 33.4 / 39.9 | 26.2 / 30.5 | 30.6 / 41.0 |

  - Reading: a page that no row cites now costs about what the page read alone costs, at every size. A page that every row cites still needs one governance decision per citing row, now in one subject read per collection: the external reader is 2.3 to 2.8 times faster, and the owner is unchanged within noise. At 20,000 citing rows on one page the read is over the 1 s budget (p95 about 1.5 s). A profile puts nearly all of that time in governance's per-row decision, so the remaining cost belongs to the decision path, not to the citation reader. `test_a_read_runs_the_same_statements_however_many_governed_rows_cite_the_page` holds the statement count fixed: before the fix, 3 citing rows ran 53 statements and 39 ran 164.

  - Row-governed citation reads, second review round. The read above still decided every citing row, for the owner too. Now a reader for whom every row releases alike decides no row, and other readers count at most `EXOMEM_CITATION_COUNT_LIMIT` visible rows (default 1,000).
  - Instrument: the same harness and scenario as the table above. Before is `c982bb52f`, after is the fix; both arms ran back to back in one session. The 100,000-row point is a later run of the same harness after the fix only. "Count" is the after arm's count for the page every row cites; "lower bound" means `count_complete: false`.

    | Rows | Reader | Count | Page every row cites, before | after | Page no row cites, before | after | Page read alone, before | after |
    |---|---|---|---|---|---|---|---|---|
    | 1,000 | owner | 1,000 | 89.2 / 104.3 | 29.1 / 31.3 | 24.3 / 39.3 | 27.3 / 29.6 | 15.9 / 20.4 | 24.2 / 28.3 |
    | 1,000 | external | 999 | 85.2 / 102.1 | 64.8 / 69.5 | 32.2 / 37.9 | 22.7 / 30.9 | 30.6 / 39.8 | 21.8 / 31.7 |
    | 5,000 | owner | 5,000 | 327.6 / 351.0 | 21.3 / 37.0 | 31.4 / 36.4 | 25.6 / 29.6 | 25.2 / 33.9 | 17.6 / 25.0 |
    | 5,000 | external | 1,000, lower bound | 324.6 / 349.0 | 58.0 / 60.0 | 31.7 / 34.2 | 20.5 / 23.9 | 27.9 / 30.8 | 19.5 / 21.6 |
    | 20,000 | owner | 20,000 | 1209.2 / 1270.6 | 38.5 / 43.6 | 29.4 / 31.0 | 30.9 / 38.6 | 27.7 / 34.0 | 24.8 / 26.6 |
    | 20,000 | external | 1,000, lower bound | 1305.9 / 1358.4 | 72.7 / 81.8 | 31.7 / 40.7 | 34.2 / 61.1 | 29.1 / 34.2 | 30.6 / 34.4 |
    | 100,000 | owner | 100,000 | not run | 64.9 / 68.3 | not run | 25.1 / 28.2 | not run | 23.2 / 26.9 |
    | 100,000 | external | 1,000, lower bound | not run | 69.5 / 74.7 | not run | 30.5 / 32.0 | not run | 20.2 / 25.5 |

  - Reading: every point is under the 1 s budget at p95, up to 100,000 citing rows. The owner's read of a page that every row cites drops from 1.2 s to 39 ms at 20,000 rows, with an exact count. That count is an index-only count, about 40 ms over the page read alone at 100,000 rows. The external reader stops counting at 1,000 visible rows, so its read stays flat from 5,000 rows on. The before arm was not run at 100,000 rows: it decides every citing row, about 6 s a read by its 20,000-row slope.
  - Reading: in this scenario the external reader sees all but one citing row. The third round below adds a reader who sees 3 of them.

  - Row-governed citation reads, third review round. The read above still decided each citing row for a reader whose release varies by row, so a reader who saw few of them could run out of time. Under the 200 ms interactive deadline, a page with thousands of withheld citing rows then read as `unavailable` while its twin read as `ready`. Now a summary or log collection is counted and paged through a row filter that governance builds once a read (design.md).
  - Instrument: the same harness and scenario as the tables above, now under the shipped query profiles. For the third reader the scope names every row but the first 3 by ref, so the external reader sees 3 citing rows. Before is `3a308cd8c`, after is the fix. The after arm ran each size in its own process, and the before arm ran both sizes in one. The 100,000-row point is the after arm only. "Count" is the page every row cites, before → after; "exact" means `count_complete: true`.

    | Rows | Reader | Count, before → after | Page every row cites, before | after | Page no row cites, before | after | Page read alone, before | after |
    |---|---|---|---|---|---|---|---|---|
    | 5,000 | owner | 5,000 → 5,000 | 33.5 / 36.1 | 32.6 / 35.5 | 31.7 / 33.2 | 29.5 / 34.7 | 27.5 / 33.8 | 27.6 / 29.9 |
    | 5,000 | external, sees all but 1 | 1,000, lower bound → 4,999, exact | 73.3 / 77.8 | 39.4 / 43.0 | 34.1 / 38.1 | 33.2 / 38.7 | 27.2 / 33.2 | 30.5 / 34.8 |
    | 5,000 | external, sees 3 | `unavailable` → 3, exact | 222.5 / 229.8 | 56.0 / 63.1 | 22.9 / 37.9 | 32.0 / 41.1 | 17.7 / 20.3 | 29.2 / 33.3 |
    | 20,000 | owner | 20,000 → 20,000 | 26.8 / 36.8 | 38.7 / 42.9 | 18.9 / 23.8 | 28.3 / 33.2 | 22.9 / 28.0 | 30.0 / 42.9 |
    | 20,000 | external, sees all but 1 | 1,000, lower bound → 19,999, exact | 60.2 / 69.3 | 64.0 / 71.9 | 26.4 / 31.6 | 39.5 / 90.7 | 19.0 / 22.8 | 30.6 / 37.3 |
    | 20,000 | external, sees 3 | `unavailable` → 3, exact | 222.1 / 231.1 | 138.8 / 149.5 | 21.0 / 23.1 | 34.5 / 39.9 | 17.0 / 20.9 | 31.6 / 34.7 |
    | 100,000 | owner | not run → 100,000 | not run | 51.6 / 56.7 | not run | 17.8 / 24.4 | not run | 14.6 / 20.9 |
    | 100,000 | external, sees all but 1 | not run → 99,999, exact | not run | 125.4 / 133.1 | not run | 20.5 / 23.2 | not run | 16.4 / 18.1 |
    | 100,000 | external, sees 3 | not run → `unavailable` | not run | 88.9 / 100.7 | not run | 19.3 / 23.2 | not run | 16.0 / 16.6 |

  - Reading: before, the reader who sees 3 citing rows ran out of the deadline at 222 ms and read `unavailable`, at 5,000 and at 20,000 rows. After, it reads `ready` with the exact count 3, at 63 and 150 ms p95. The external reader now gets an exact count at every size, at 133 ms p95 or less up to 100,000 rows. The owner is unchanged within the run-to-run spread, which the page read alone shows at about 10 ms.
  - Reading: the reader who sees 3 still pays for the rows that the scope names by ref, about 5 µs a row. At 100,000 rows with 99,997 named, its read runs out of time and reads `unavailable`, while a page that the collection does not cite reads `ready`. design.md names this bound under Risks. A scope that withholds rows by tag costs one decision per distinct value instead: a separate run with 100,000 tag-withheld citing rows read `ready` in 130 to 157 ms of CPU, against the 200 ms deadline (a reviewer measured 145 ms). Nearly all of that time is SQL: the scan that collects the metadata values, and one filter test per citing entry for the count and for the walk past the last visible row.

  - Row-governed citation reads, fourth review round. The row filter above covered summary and log collections only. An items collection, the default layout for a store that keeps Markdown, still decided each citing row by its own view file, so a reader who saw few of them ran out of time. Now an items collection whose view files no path test tells apart is counted and paged through the same row filter (design.md D3).
  - Instrument: the same harness under the shipped query profiles, on a `markdown-items` collection with one view file a row. Every row but the first 3 carries the tag `secret`, which a tag scope and a ceiling-0 rule withhold from the external reader, so that reader sees 3 citing rows. Before is `f89d17075`, after is the fix. The after arm ran each size in its own process, and the before arm ran both sizes in one.

    | Rows | Reader | Count, before → after | Page every row cites, before | after | Page no row cites, before | after | Page read alone, before | after |
    |---|---|---|---|---|---|---|---|---|
    | 5,000 | external, sees 3 | `unavailable` → 3, exact | 287.9 / 305.6 | 33.7 / 37.0 | 29.5 / 33.4 | 21.2 / 24.4 | 19.8 / 22.0 | 17.0 / 19.3 |
    | 20,000 | external, sees 3 | `unavailable` → 3, exact | 521.7 / 544.8 | 61.4 / 65.0 | 25.2 / 32.6 | 22.3 / 31.8 | 21.2 / 25.2 | 17.1 / 24.5 |

  - Reading: before, the reader who sees 3 citing rows ran out of the deadline and read `unavailable` at both sizes, while a page that no row cites read `ready`. After, it reads `ready` with the exact count 3, at 37 and 65 ms p95.
  - Reading: when a path glob tells the view files apart, the read decides each citing row it walks, one decision for each group of equal rows. A separate run of the after arm put that at 47 to 64 ms for 5,000 withheld citing rows and 130 to 154 ms for 20,000, about 6.5 µs a withheld row. So that read runs out of time at about 25,000 to 30,000 withheld rows citing one page. design.md names this bound with the ref-named one under Risks, and the page-citations requirement names both.

  - Reading: every warm public call stays under 1 s at p95 except the 21,000-row `recall_context` before (one tail of 1.38 s, a single run) and the filter on a link field, which scans the collection on both arms (1.4 s at 21,000 rows). The change adds 5 to 15 ms to a page read, a search and a recall at 5,000 rows, and nothing visible to the filter or the edit. The filter on a link field at 5,000 rows is 261 and 269 ms p95 after against 263 and 268 before (two paired runs of 60). Two paired recall runs at 21,000 rows with server timings put the change at 23 to 38 ms on the server and 11 to 12 ms outside it, though the merge base's membership graph was still warming in those runs, so they do not compare like with like.
- [x] 1.10 Outcome check on a disposable vault: a row written with a page title stores the ref and resolves after a rename, the page lists the row, a recall hit shows it, a withheld collection's row is absent, and a plan item's `results` resolves.
  - Evidence: run on 2026-11-24 over the real transport (`exomem --transport stdio`, driven by the MCP client of the `fastmcp` package) on a disposable store-mode vault with its own state root. 14 of 14 checks passed in 30 s: the write reply reports the title supplied and the ref stored, and the stored value is the ref. An unresolved title is stored as written with a `LINK_UNRESOLVED` warning. Completing a plan item with no result reports `no_result_linked`. After a page rename through `manage_memory` `files.move`, `read_page` still lists the two rows and the plan item in `cited_by`, the row value still reads as the page's ref, and the item's `results` resolve to it. A `search_memory` hit and a `recall_context` lead carry `cited_by_count`. The row of a collection the caller cannot read is absent from the page's `cited_by`, and the hit's count drops to match. The run is not a committed test: it needs the store-mode rig and a stdio server (30 s, the packet's limit), so it stays a run file of the batch's author.

## 2. Related pages, overlap and duplicates (Batch C)

- [x] 2.1 Row creation returns `related_pages` through one batched entity and title lookup per write. Update the capture skill's Records guidance in the scaffold and the shipped plugin skills.
  - Evidence: `src/exomem/collection_store/related_pages.py` runs one `WorkingSetIndex.resolve_names` call per write (the activation index's title, stem and alias table, read as stored) and keeps the pages the writer may read, at most three per row and 30 per reply. `record_inputs.append` and `bulk_inputs.detach` call it at prepare time, and the reply tail adds `related_pages` for the created keys only. Updates, Planning writes and import batches skip it: an update names no new subject, a plan item names work, and an import job's receipt reports the job, not each batch (the records spec says so). A failed or never-built index adds a `RELATED_PAGES_UNAVAILABLE` warning, never an empty list (spec scenario added). The reuse is the page-name index behind entity and title resolution, not `resolve_entity_candidate`, which globs the Entities tree on every call and cannot batch.
  - Guidance: the scaffold sources `references/planning-records.md` and `workflow-skills/exomem-capture/SKILL.md` carry the "read the related pages before an unknown or a placeholder" rule; `scripts/refresh-skill-contract.py`, `exomem package-skills --plugin-root plugins/claude-code` and `scripts/cloud-plugin.py build` regenerated the stamp, the plugin skills and the cloud packages, and `scripts/cloud-plugin.py check` reports `ok`.
- [ ] 2.2 Collection creation returns `similar_collections` with the existing reshape route and `merge_into`. Its thresholds are configuration, and the check never refuses.
- [ ] 2.3 The duplicate-fact maintenance job: resolved row-to-page pairs only, incremental, and one `duplicates` review item per candidate. It writes no page or row.
- [ ] 2.4 Restructure `retype_field` accepts `link` with `relation`, lossless and undoable within the restructure bounds. This lands after #3131 merges.
- [ ] 2.5 Tests:
  - [x] one batched lookup for a bulk seed;
    - Evidence: `test_a_bulk_seed_resolves_every_row_in_one_lookup_and_lists_at_most_thirty_pages` in `tests/test_collection_related_pages.py` (40 rows, 160 matching pages: exactly one `resolve_names` call holding 40 names, and 30 pages in the reply). Withheld pages: `test_a_withheld_page_is_listed_exactly_as_a_missing_one`; no index: `test_an_index_that_was_never_built_is_reported_not_read_as_no_pages`; update lists none: `test_a_new_row_lists_the_visible_pages_that_name_it_and_an_update_lists_none`.
  - hidden collections are neither compared nor named;
  - the detector opens one item and writes nothing else;
  - a retype refuses on one unresolved value;
  - undo restores the titles.
- [ ] 2.6 Real-model round (`eval-loop`, subscription models only): replay the 2026-11-24 Records seeding incident and record whether the agent reads `related_pages` before it writes an unknown.

## 3. Delivery

- [ ] 3.1 One blind independent review per batch PR.
- [ ] 3.2 After the release is installed on the owner's service: retype the free-text page fields of one existing personal collection to `link`, then check that reading a page that a row names lists that row.
- [ ] 3.3 Sync the deltas into the canonical specs and archive this change with `openspec archive`, after `openspec validate --all --strict`.
