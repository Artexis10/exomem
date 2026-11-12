## 1. Value rendering

- [x] 1.1 Add the bounded value rendering beside `source_inspection.py`, reusing `query_data.load_rows_bytes`, with named budget constants
- [x] 1.2 Decide qualification from the card's inspection status and the original's size and parse limits

## 2. Derived sidecar

- [x] 2.1 Add `dataset_values.py`: a derived sidecar with a word index and a compact trigram index over each qualifying dataset's records, keyed by the original's path
- [x] 2.2 Build each value hit's passage from a fresh rendering of the original, after a SHA-256 check against the card
- [x] 2.3 Reconcile the sidecar at query time: index a card with no row or a changed `card_hash`, delete rows whose card left the recall projection, and search only rows built from the card's current hash
- [x] 2.4 Run the lane in `commands.op_find` and merge its hits into the result with rank fusion

## 3. Release

- [x] 3.1 Drop a value hit in `annotate_hits` below the full release level, with no excerpt and no notice
- [x] 3.2 End-to-end test: preserve a small JSON, then find a value as a full-access reader; with membership unresolved or below full level, get no value hit while the card stays findable; a dataset over 64 KiB gets no value hit
- [x] 3.3 After release, count the live dataset cards read-only, then confirm on the personal service that searches for the 7 November account number return the supplier-profile original: the bare number, a supplier name with "IBAN", and the same name with "bank account". Record which phrasings find it and which do not
  - Checked on 2026-11-11 on 0.171.0. The vault holds 757 dataset cards (755 JSON, 2 CSV).
  - All three phrasings returned a supplier-profile original with the account in its passage: the bare unspaced number, the supplier name with "IBAN", and the supplier name with "bank account".
  - The bare number ranked the earlier profile version second and did not place the latest version in the top 3. Each passage starts at the record start, not at the matched line.
