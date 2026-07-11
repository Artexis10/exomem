## 1. Corpus Measurement

- [x] 1.1 Implement the dependency-light activation scanner, eligibility rules, coverage counts, and four structural findings.
- [x] 1.2 Add deterministic next-action metadata, content signal versions, and non-mutating behavior tests.

## 2. Review Workflow

- [x] 2.1 Generalize attention composition for a fixed activation category order while preserving default attention output.
- [x] 2.2 Add `review_memory(mode="activation")`, coverage output, activation item lookup, and triage lifecycle support.
- [x] 2.3 Keep activation and daily-attention identities distinct for overlapping pages and verify independent triage.

## 3. Surface And Lifecycle Verification

- [x] 3.1 Add focused scanner, ranking, review-state, and default-attention regression tests.
- [x] 3.2 Verify matching activation behavior through MCP, REST, and CLI and update command-schema fixtures where required.
- [x] 3.3 Run lint, the focused suite, the full dependency-light suite, and an installed-wheel full-lifecycle E2E.

## 4. Completion

- [x] 4.1 Validate the OpenSpec change against implementation and mark all completed tasks.
