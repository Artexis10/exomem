## 1. Matching

- [x] 1.1 Store one FTS5 row per field with its record, field number and path, and bump the sidecar version
- [x] 1.2 Take query words from `find_policy.query_word_stem_groups`, drop function-word groups and one-character stems without a digit, and weight each group by its dataset frequency
- [x] 1.3 Find candidate records from the heaviest groups, score each record's best part of at most 40 fields by coverage, and require a value word and coverage of at least 0.5
- [x] 1.4 Apply the noise gate when the scope holds at least 40 datasets: drop a part whose found groups occur together in more than max(3, 2%) of the datasets

## 2. Results

- [x] 2.1 Order value hits by coverage, part size, newest card, then path; word matches before compact-only matches; pass at most 3 to `fuse`
- [x] 2.2 Build the passage from the match within 600 characters, with the matched lines first and "…" for gaps

## 3. Proof

- [x] 3.1 Tests for every scenario in the delta, and the existing value-search tests unchanged in intent
- [x] 3.2 After release, install on the personal service and check read-only that a supplier name with "IBAN", the possessive form and the "what is the IBAN for" question each return a supplier-profile original first (raw-protected versions stay outside value search, as before), and that "test results" and "review verdict" return no dataset original. Record the results here

  Verified live on 0.173.0 (2026-11-11). All three phrasings returned a value hit rendering the profile dataset original; the unprotected version surfaced ahead of the protected ones where ranking differed. "test results" and "review verdict" returned no dataset original. Observation for the record: raw-protected versions also produce value hits *for the owner principal* — the lane has never had an index-time raw exclusion (identical `is_indexable` + `admit_path` semantics in every version since #2678); protected originals are withheld from readers below the full level by `egress.annotate_hits`, and the owner reaches full, matching `query_dataset` authority. If exclusion for the owner too was intended, that is pre-existing debt, not a regression of this change.
