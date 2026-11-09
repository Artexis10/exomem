## 1. Value rendering

- [ ] 1.1 Add the bounded value rendering beside `source_inspection.py`, reusing `query_data.load_rows_bytes`, with named budget constants
- [ ] 1.2 Decide qualification from the card's inspection status and the original's size and parse limits

## 2. Derived index rows

- [ ] 2.1 Emit the standalone derived row at the original's path from `lexstore.page_rows` for a qualifying card, with the card's filter axes and a combined content hash
- [ ] 2.2 Feed the same rendering to `embeddings._chunks_for_page` for that row
- [ ] 2.3 Build passages for value hits in `find_parts` from the rendering
- [ ] 2.4 Remove the row when its card is removed or tombstoned, and rebuild it when the card or original changes

## 3. Release

- [ ] 3.1 Confirm that `annotate_hits` decides value hits through the path-only decision and drops them below full level
- [ ] 3.2 End-to-end test: preserve a small JSON, then find a value as a full-access reader; with membership unresolved, get no value hit while the card stays findable; a dataset over 64 KiB gets no value hit
- [ ] 3.3 Reindex the personal service after release, and confirm that a search for the 7 November account number returns the supplier-profile original
