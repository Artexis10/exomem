## 1. Contract and regression tests

- [x] 1.1 Add the attention-queue delta specification for forward-reference classification.
- [x] 1.2 Add red-first tests for missing-page classification and automatic clearing after target creation.
- [x] 1.3 Retain regression coverage that unresolved links do not satisfy semantic relation disposition.

## 2. Audit classification

- [x] 2.1 Register `forward_reference` as an audit category and share one scan across both link categories.
- [x] 2.2 Emit informational forward-reference findings for unresolved Markdown-page targets.
- [x] 2.3 Keep definite attachment, ambiguity, and note/attachment mismatch errors in `broken_wikilink`.

## 3. Verification

- [x] 3.1 Run focused audit and connectivity-lane tests.
- [ ] 3.2 Run the full pytest suite and `ruff check . --select F` on Linux.
- [x] 3.3 Validate the OpenSpec change in strict mode.
