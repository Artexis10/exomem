## 1. Red-first contract tests

- [x] 1.1 Add focused tests for resolved eligible and Source body-wikilink connectivity, including signal, warning, and non-blocking behavior
- [x] 1.2 Add focused tests proving self, inactive, non-connectable, unresolved, inbound, provenance-back-reference, and captured-Source bootstrap boundaries remain non-qualifying where required
- [x] 1.3 Run the focused tests and record the expected pre-implementation failures

## 2. Connectivity implementation

- [x] 2.1 Normalize page-state body-wikilink targets and check direct membership in `connectable_target_paths` after fact-based connectivity
- [x] 2.2 Reuse the existing qualifying disposition kind and connectivity warning without constructing relation facts or touching governed predicates
- [x] 2.3 Run focused connectivity tests green

## 3. Acceptance

- [x] 3.1 Run the governance-overhead test and full available test suite
  Delivered in [PR #428](https://github.com/Artexis10/exomem/pull/428), merged as
  `7d470659eabd01529ebbf28117d7eb3ce1e4232e` after final head
  `6df472371cc017188deda749701c2623aa643ebf` passed all 21 reported checks.
  Its CI covered the complete Linux suite, governance-overhead gate and
  semantic-write latency job. Historical local scope was
  `tests/test_semantic_connectivity_lane.py`, `tests/test_semantic_contract.py`,
  `tests/test_audit.py` and `tests/test_note.py` (227 passed).
  The merge is an ancestor of release `v0.115.0`, commit
  `5abed4438ae767af5c89ccd1438c63e35df107d8`.
  Canonical sync now includes the semantic-language contract, the proved
  connectivity overlap and the final body-link behavior in that order.
  `measure-real-note-connectivity` remains open with its other capabilities and
  tasks unsynchronized and unchecked.

- [x] 3.2 Measure 2k/8k write latency after implementation in the same session and record the comparison in `design.md`
- [x] 3.3 Run F-only Ruff and strict OpenSpec validation

## Closure operation

Archive after strict validation with
`openspec archive connect-body-wikilinks --skip-specs --json`.
OpenSpec CLI 1.14.0 completed the command on 2026-10-11 and returned
`archivedAs: 2026-10-11-connect-body-wikilinks`.
Its `specsUpdated: false` records the skipped mechanical sync after the canonical
merge and full preservation check.
