## 1. Red-First Contract Tests

- [x] 1.1 Add a failing test that `audit` accepts `categories=["prediction_window"]` without raising, and that the category is present in the audit registry.
- [x] 1.2 Add a failing test that a unit with `check_by` 14 days ago, no `verdict`, and no relations produces exactly one `info` finding anchored on the parent page whose meta reports 14 overdue days.
- [x] 1.3 Add a failing test that a `verdict` on the unit clears the finding.
- [x] 1.4 Add failing tests that each of `supports`, `contradicts`, `resolves`, and `evidenced_by` authored as an outbound relation on the unit clears the finding.
- [x] 1.5 Add a failing test that a non-resolving outbound relation such as `relates_to` does not clear the finding.
- [x] 1.6 Add a failing test that a resolving relation on a sibling unit does not clear a due unit on the same page — the predicate is unit-local.
- [x] 1.7 Add failing tests for the date boundary: `check_by` equal to today is due, `check_by` tomorrow is not, and a unit with no `check_by` is never surfaced.
- [x] 1.8 Add a failing test that `superseded`, `archived`, and `draft` pages are excluded.
- [x] 1.9 Add a failing test that the queue is ordered most-overdue-first with deterministic tiebreaks.
- [x] 1.10 Add a failing test that the check writes nothing: the vault's file set and content hashes are identical before and after the audit pass.

## 2. Fingerprint And Partition Tests

- [x] 2.1 Add a failing test that a finding's `meta.signal_version` equals the surfaced unit's fingerprint.
- [x] 2.2 Add a failing test that two due units on one page compose as two `attention` items sharing the page path with distinct review identities and distinct fingerprints.
- [x] 2.3 Add a failing test that editing a surfaced unit's authored content changes its `signal_version`, so the review item resurfaces rather than inheriting a prior decision.

## 3. Attention Registration Tests

- [x] 3.1 Add a failing test that `attention(categories=["prediction_window"])` surfaces the due prediction as a ranked item carrying its reason.
- [x] 3.2 Add a failing test that a default `attention()` call over the same vault surfaces no `prediction_window` item, proving the default union is untouched.

## 4. Audit Check Implementation

- [x] 4.1 Add `prediction_window` to `audit.ALL_CATEGORIES` and to the registered-but-not-default epistemic review category tuple that `attention` consumes.
- [x] 4.2 Implement `_check_prediction_window`: page scoping, the `check_by` substring prefilter, one `parse_semantic_units` call per candidate page with the vault's language and relation registries, and the unit-local due/unresolved predicate.
- [x] 4.3 Resolve each relation kind through the relation registry to its canonical key before testing membership in the resolving family, so a registered alias is honoured.
- [x] 4.4 Emit `info` findings anchored on the parent path with `meta` carrying `signal_version`, `review_partition`, `unit_ref`, `anchor`, `kind`, `check_by`, and `overdue_days`; order most-overdue-first with deterministic tiebreaks.
- [x] 4.5 Dispatch the new check from `audit()` under its category guard.
- [x] 4.6 Document the check in the `audit.py` module docstring's check list.

## 5. Attention Wiring

- [x] 5.1 Extend the epistemic review category tuple that `attention.ATTENTION_CATEGORIES` consumes, leaving `DEFAULT_ATTENTION_CATEGORIES` unchanged.
- [x] 5.2 Confirm no change is required in `commands.py`: the existing generic `categories` plumbing on `review_memory(mode="attention"|"audit")` already reaches the new category, and no tool docstring is edited.

## 6. Verification

- [x] 6.1 Run the new test file plus `tests/test_audit.py`, `tests/test_attention.py`, and `tests/test_epistemic_loop_primitives.py` green with `EXOMEM_DISABLE_EMBEDDINGS=1`.
- [x] 6.2 Confirm `git diff --exit-code tests/fixtures/mcp_tool_schemas.json src/exomem/tool_surface_contract.json` is clean — the pinned tool surface must not move.
- [x] 6.3 Run the CI-required `uvx ruff check . --select F` gate clean, and the full-config `uvx ruff check` clean on every file this change touches. (A bare repo-wide `uvx ruff check .` reports a large pre-existing advisory baseline that predates this change; CI gates on `--select F` for exactly that reason.)
- [x] 6.4 Run `openspec validate add-prediction-window-review --strict` and `openspec validate --specs --strict` clean.
