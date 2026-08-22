## 1. Red Contracts For Motivation Shape

- [x] 1.1 Write failing `normalize_item` unit tests: a valid `motivation` list
      is accepted, a list of more than 16 refs is refused, a malformed ref
      string is refused, a non-list value is refused, and an
      `exomem://plan/...` reference is refused (motivation cites knowledge,
      never another plan).
- [x] 1.2 Write a failing regression test proving absence of `motivation`
      produces exactly today's defaulted item shape (no key added).
- [x] 1.3 Run `uv run --frozen python -m pytest -q tests/test_planning_motivation.py`
      and capture the failing output before writing implementation.

## 2. Motivation Shape Validation

- [x] 2.1 Add `_validate_motivation` to `src/exomem/planning.py`, mirroring
      `_validate_evidence`: bounded list (≤16), each entry checked through
      `memory_refs.parse_memory_ref`, refused (`INVALID_PLAN`) otherwise.
- [x] 2.2 Wire `_validate_motivation` into `_validate_optional` alongside the
      existing `progress_evidence`/`execution` calls.
- [x] 2.3 Add `motivation` to `_OPTIONAL` so `update()` can delete it like any
      other optional field.
- [x] 2.4 Run `uv run --frozen python -m pytest -q tests/test_planning_motivation.py`
      and confirm green.

## 3. Round Trip And Query Filter

- [x] 3.1 Write/confirm a serialization round-trip test through
      `record_formats.render_markdown_item` + `vault.parse_frontmatter`.
- [x] 3.2 Write/confirm an `add()`/`query()` integration round trip, including
      that a collection must declare `motivation` in its manifest schema to
      accept it (same precondition as `progress_evidence`).
- [x] 3.3 Write/confirm a query-filter test selecting only the item whose
      `motivation` contains a given ref via the existing generic
      `filters=[{"column": "motivation", "op": "contains", "value": ref}]`
      path (no new `plan_memory` parameter — see `design.md` Decision 2).

## 4. Non-Goal Enforcement

- [x] 4.1 Write/confirm a test proving `motivation` does not satisfy a
      committed initiative's required `parent` relation — the relation graph
      only reads `parent`/`area`, never `motivation`.
- [x] 4.2 Confirm (by inspection, documented in the report) that raw Planning
      items — where `motivation` lives — never reach the lexical/vector/graph
      indexers; only the collection manifest does
      (`tests/test_planning_recall.py` already locks this at scale). No new
      recall-exclusion test is needed because the guarantee is path-based,
      not field-based.

## 5. Spec And Gates

- [x] 5.1 Author `specs/planning/spec.md` as `## ADDED Requirements` (no
      `openspec/specs/planning/` capability exists yet).
- [x] 5.2 Run `openspec validate link-planning-motivation --strict`.
- [x] 5.3 Run `uvx ruff check` on changed files.
- [x] 5.4 Run the targeted gate: new tests, `tests/test_planning_*.py`, and
      the record mutation suites (`test_record_audit_protocol.py`,
      `test_record_formats.py`, `test_record_formats_rereview.py`,
      `test_record_formats_review.py`, `test_record_governance.py`,
      `test_record_mutation.py`, `test_record_mutation_matrix.py`). Do not
      run the full suite.
