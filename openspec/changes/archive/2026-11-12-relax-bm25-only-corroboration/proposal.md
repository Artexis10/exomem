## Why

Hybrid find retains a BM25-ranked page that no other lane corroborated only
when the page holds **every** query stem. Natural questions carry stems the
answering page never contains — connectives and question phrasing such as
"after", "turn" or "remains" — so the veto drops the page its own BM25 lane
ranked first, and the product returns nothing for a question it can answer.
Measured against a live vault: a fully indexed and embedded research note
ranked first on every lane for its content terms and disappeared from the top
30 when three phrasing words were added back to the query.

The relaxed gate already exists, but only when the semantic lanes are
structurally absent for the query: a strict majority of the query's words
present (whole-word presence), with at least one content word among them.
When a vector or CLIP lane produces any candidate at all, the veto is
all-stems — even for the BM25-only candidates those lanes never touched.

## What Changes

- **Corroboration becomes per-candidate, not per-lane-availability.** A
  candidate that only the BM25 lane produced passes the same strict
  word-majority gate whether or not the semantic lanes are live. Candidates
  the vector, CLIP, graph or keyword lanes corroborated keep their existing
  evidence and are untouched.
- The strict all-stems veto no longer exists at this seam. Its precision
  contract is carried by the two existing guards: a strict majority of query
  words must be present, and at least one present word must be a content
  word, so pure function-word overlap cannot retain a page.
- `mode="keyword"` conjunctive semantics are unchanged.

## Impact

- `src/exomem/find.py`: `_supported_stems` uses the existing
  `find_policy.stem_word_coverage` gate for every corroboration-needing
  candidate; `semantic_lanes_absent` and the lanes-absent branch go away.
- Spec delta under `multilingual-recall`: the coverage-gate requirement
  gains the per-candidate contract.
- Tests: `tests/test_find_degraded_retention.py` gains the live-lanes
  retention cases; the test pinning the all-stems veto is revised to the
  new contract.
