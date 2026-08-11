## 1. Red-first contract tests

- [x] 1.1 Add focused tests for resolved eligible and Source body-wikilink connectivity, including signal, warning, and non-blocking behavior
- [x] 1.2 Add focused tests proving self, inactive, non-connectable, unresolved, inbound, provenance-back-reference, and captured-Source bootstrap boundaries remain non-qualifying where required
- [x] 1.3 Run the focused tests and record the expected pre-implementation failures

## 2. Connectivity implementation

- [x] 2.1 Normalize page-state body-wikilink targets and check direct membership in `connectable_target_paths` after fact-based connectivity
- [x] 2.2 Reuse the existing qualifying disposition kind and connectivity warning without constructing relation facts or touching governed predicates
- [x] 2.3 Run focused connectivity tests green

## 3. Acceptance

- [ ] 3.1 Run the governance-overhead test and full available test suite
- [x] 3.2 Measure 2k/8k write latency after implementation in the same session and record the comparison in `design.md`
- [x] 3.3 Run F-only Ruff and strict OpenSpec validation
