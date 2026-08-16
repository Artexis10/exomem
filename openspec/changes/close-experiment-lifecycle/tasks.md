## 1. Red-First Contract Tests

- [x] 1.1 Add a failing test that `audit` accepts `categories=["unfinished_experiments"]` without raising, and that the category is present in the audit registry.
- [x] 1.2 Add a failing test that an active experiment `started` 120 days ago with `duration: "30 days"` and no `outcome:` produces exactly one `info` finding whose meta reports `elapsed_days=120` and `overdue_days=90`.
- [x] 1.3 Add a failing test that the same experiment with a non-empty `outcome:` produces no finding.
- [x] 1.4 Add a failing test that an experiment with `status: concluded` and no `outcome:` past its window still produces a finding — status records that it stopped, not what it showed.
- [x] 1.5 Add a failing test that `duration: ongoing` (and an unparseable duration) never produces a finding regardless of how long ago it started.
- [x] 1.6 Add a failing test that an experiment still inside its window produces no finding, including the exact boundary where elapsed equals duration.
- [x] 1.7 Add a failing test that `archived`, `superseded`, and `draft` experiments are excluded from the queue.
- [x] 1.8 Add a failing test that the queue is ordered oldest-first by elapsed age with the vault-relative path as the tiebreak.
- [x] 1.9 Add a failing test that the check writes nothing: the vault's file set and content hashes are identical before and after the audit pass.

## 2. Attention Registration Tests

- [x] 2.1 Add a failing test that `attention(categories=["unfinished_experiments"])` surfaces the overdue experiment as a ranked item carrying its reason.
- [x] 2.2 Add a failing test that a default `attention()` call over the same vault surfaces no `unfinished_experiments` item, proving this change does not touch the default union.
- [x] 2.3 Add a test pinning `attention.DEFAULT_ATTENTION_CATEGORIES` to its exact tuple and order, so any widening of the daily surface has to be an explicit edit. (This change does not widen it; the sibling `add-prediction-window-review` does, and updates the pin.)
- [x] 2.4 Record the backlog-profile reasoning at the definition of `audit.EPISTEMIC_REVIEW_CATEGORIES`, so a reader comparing the two lifecycle queues sees a deliberate split rather than an inconsistency.

## 3. Audit Check Implementation

- [x] 3.1 Add `unfinished_experiments` to `audit.ALL_CATEGORIES` and introduce the registered-but-not-default epistemic review category tuple that `attention` consumes.
- [x] 3.2 Implement the `duration` span parser: a leading count with a day/week/month/year unit, a bare integer as days, and everything else — including `ongoing` — as no finite window.
- [x] 3.3 Implement `_check_unfinished_experiments` with the scoped predicate, `info` severity, oldest-first ordering, and `meta` carrying `signal_version`, `started`, `duration_days`, `elapsed_days`, `overdue_days`, and `status`.
- [x] 3.4 Dispatch the new check from `audit()` under its category guard.
- [x] 3.5 Document the check in the `audit.py` module docstring's check list.

## 4. Attention Wiring

- [x] 4.1 Extend `attention.ATTENTION_CATEGORIES` with the new epistemic review category tuple, leaving `DEFAULT_ATTENTION_CATEGORIES` unchanged.
- [x] 4.2 Confirm no change is required in `commands.py`: the existing generic `categories` plumbing on `review_memory(mode="attention"|"audit")` already reaches the new category, and no tool docstring is edited.

## 5. Documentation Truth Repair

- [x] 5.1 Correct the `stale_review` scope comment in `audit.py` so it cites `unfinished_experiments` for the experiment exclusion and states plainly that the `production-log` exclusion currently has no backing check.
- [x] 5.2 Rewrite the "Unfinished experiments" entry in `src/exomem/_scaffold/_Schema/references/audit-checks.md` to state the implemented predicate, the `info` severity, the open-ended-duration carve-out, the ordering, and that the category is opt-in on the attention surface.
- [x] 5.3 Confirm `note.py` needs no change — `STATUS_EXPERIMENT` already carries `concluded` and `EXPERIMENT_OUTCOME_VALUES` already aliases the shared epistemic outcome vocabulary — and record that finding in `design.md`.

## 6. Verification

- [x] 6.1 Run the new test file plus `tests/test_audit.py`, `tests/test_attention.py`, and `tests/test_epistemic_loop_primitives.py` green with `EXOMEM_DISABLE_EMBEDDINGS=1`.
- [x] 6.2 Run `tests/test_scaffold_no_leak.py` to confirm the scaffold edit introduces no personal or vault-structure token.
- [x] 6.3 Confirm `git diff --exit-code tests/fixtures/mcp_tool_schemas.json src/exomem/tool_surface_contract.json` is clean — the pinned tool surface must not move.
- [x] 6.4 Run the CI-required `uvx ruff check . --select F` gate clean, and the full-config `uvx ruff check` clean on every file this change touches. (A bare repo-wide `uvx ruff check .` reports a large pre-existing advisory baseline that predates this change; CI gates on `--select F` for exactly that reason.)
- [x] 6.5 Run `openspec validate close-experiment-lifecycle --strict` and `openspec validate --specs --strict` clean.
