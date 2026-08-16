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

## 3. Default-Union Tests

- [x] 3.1 Add a failing test that `attention(categories=["prediction_window"])` surfaces the due prediction as a ranked item carrying its reason.
- [x] 3.2 Add a failing test that a **default** `attention()` call over the same vault surfaces the due prediction without the caller naming the category.
- [x] 3.3 Replace the default-union guard with one that pins the exact six-tuple in its exact order, so a later addition to the daily surface fails the test rather than passing silently.
- [x] 3.4 Add a failing tiebreak test that at equal RRF scores `prediction_window` orders ahead of `corpus_contradictions`, `stale_review`, and `relation_debt`.
- [x] 3.5 Add a failing test that asserts the split directly: over one vault holding both an overdue experiment and a due prediction, a default `attention()` surfaces the prediction and not the experiment.

## 4. Audit Check Implementation

- [x] 4.1 Add `prediction_window` to `audit.ALL_CATEGORIES` and to `attention.DEFAULT_ATTENTION_CATEGORIES` in second tiebreak position, leaving `audit.EPISTEMIC_REVIEW_CATEGORIES` to carry only the opt-in experiment queue.
- [x] 4.2 Implement `_check_prediction_window`: page scoping, the `check_by` substring prefilter, one `parse_semantic_units` call per candidate page with the vault's language and relation registries, and the unit-local due/unresolved predicate.
- [x] 4.3 Resolve each relation kind through the relation registry to its canonical key before testing membership in the resolving family, so a registered alias is honoured.
- [x] 4.4 Emit `info` findings anchored on the parent path with `meta` carrying `signal_version`, `review_partition`, `unit_ref`, `anchor`, `kind`, `check_by`, and `overdue_days`; order most-overdue-first with deterministic tiebreaks.
- [x] 4.5 Dispatch the new check from `audit()` under its category guard.
- [x] 4.6 Document the check in the `audit.py` module docstring's check list.

## 5. Attention Wiring

- [x] 5.1 Record the ordering principle — authored commitments before inferred signals — as a comment on `DEFAULT_ATTENTION_CATEGORIES`, so the next queue addition has a criterion to argue against rather than a list to append to.
- [x] 5.2 Update the `attention.py` module docstring, which enumerates the default queues and would otherwise be made stale by this change. It is a plain module docstring, not a pinned MCP tool description, so it carries no fingerprint cost.
- [x] 5.3 Correct `op_attention`'s docstring. It is the registered description for the `attention` command on CLI, MCP, and REST, and it still enumerated four queues and a four-item `categories` set. Verified empirically that it is NOT part of the pinned surface: after editing it, `git diff --exit-code tests/fixtures/mcp_tool_schemas.json src/exomem/tool_surface_contract.json` stays clean even after running `test_tool_surface_contract.py` and `test_consolidated_tools.py`, and the description string appears in neither pinned file. `review_memory`'s own docstring is still untouched.
- [x] 5.4 Confirm `op_review_memory` needs no change: the existing generic `categories` plumbing already reaches the new category, and its pinned docstring is not edited.
- [x] 5.5 Modify the three `attention-queue` requirements this change moves — the default union, the RRF tiebreak order, and multi-signal additivity — reproducing each existing block verbatim before editing, and confirm `close-experiment-lifecycle` still touches none of them.

## 6. Composition Repair (found in review)

- [x] 6.1 Add a failing test that a partitioned finding and an unpartitioned finding on the same path compose ONE item whose votes sum — `test_multi_signal_additivity_and_dedup` covers only unpartitioned findings.
- [x] 6.2 Add a failing test that a page with two due predictions plus a page-level signal composes two items, each carrying the shared reason, with no third item for the page-level finding alone.
- [x] 6.3 Fold a path's unpartitioned findings into each of its partitioned items in `_rank` — reasons, RRF votes, and severity — and stop emitting the standalone unpartitioned item for that path.
- [x] 6.4 Add the end-to-end test over a real vault, since the default-union promotion is what made the collision routine.
- [x] 6.5 Widen the `check_by` prefilter to match `normalize_label`, and cover every spelling the parser accepts (`Check By`, `check by`, `check-by`, `CHECK_BY`).
- [x] 6.6 Add `dropped` and `planned` to both parked-status sets, matching what `_check_relation_debt`, `activation.py`, and `semantic_contract.py` already treat as inactive.
- [x] 6.7 Cover the scope guards the ADDED requirements assert with SHALL — page type, `started` present, index/log exclusion, and access tier — for both queues.
- [x] 6.8 Document `prediction_window` in the shipped scaffold's `audit-checks.md`, with a pin test, because every user now sees this queue unasked.

## 7. Verification

- [x] 7.1 Run the new test file plus `tests/test_audit.py`, `tests/test_attention.py`, and `tests/test_epistemic_loop_primitives.py` green with `EXOMEM_DISABLE_EMBEDDINGS=1`.
- [x] 7.2 Confirm `git diff --exit-code tests/fixtures/mcp_tool_schemas.json src/exomem/tool_surface_contract.json` is clean — the pinned tool surface must not move.
- [x] 7.3 Run the CI-required `uvx ruff check . --select F` gate clean, and the full-config `uvx ruff check` clean on every file this change touches. (A bare repo-wide `uvx ruff check .` reports a large pre-existing advisory baseline that predates this change; CI gates on `--select F` for exactly that reason.)
- [x] 7.4 Run `openspec validate add-prediction-window-review --strict` and `openspec validate --specs --strict` clean.
