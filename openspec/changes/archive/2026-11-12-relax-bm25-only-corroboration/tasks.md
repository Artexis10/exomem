## 1. Implementation

- [x] 1.1 `src/exomem/find.py`: replace the lanes-absent branch in
  `_supported_stems` with the strict word-majority plus content-word gate
  for every corroboration-needing candidate; compute the query word
  groups unconditionally; drop `semantic_lanes_absent`.

## 2. Tests

- [x] 2.1 Update `test_strict_veto_when_vector_lane_is_active` to the new
  contract: a BM25-only page meeting the gate is retained while the
  vector lane is live.
- [x] 2.2 Add a regression case mirroring the measured defect: a
  natural-language query whose phrasing words are absent from the correct
  page retains that page while semantic lanes produce candidates.
- [x] 2.3 Pin the preserved guards under live lanes: exactly-half and
  function-word-majority queries still veto BM25-only candidates.

## 3. Validation

- [x] 3.1 `uv run pytest tests/test_find_degraded_retention.py` green.
- [x] 3.2 `openspec validate --all --strict` clean.
- [x] 3.3 Public-artifact privacy gate clean.
