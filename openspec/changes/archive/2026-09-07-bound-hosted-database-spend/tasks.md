## 1. Read-only audit

- [x] 1.1 Add red-first tests for passing controls, capacity/inventory drift, absent alerts, optional quota units, malformed/partial provider data, command timeout and secret-sentinel suppression; implement the minimal audit and pass `pytest -q tests/test_hosted_database_budget.py`.
- [x] 1.2 Add the operational runbook with read-modify-read controls, cutoff semantics and retained idle baseline; verify its commands against Neon v2 and pass `tests/test_openspec_only.py`.

## 2. Delivery

- [x] 2.1 Independently review the diff and rerun the negative audit cases; run the real read-only audit, relevant hosted infra/static suites and strict OpenSpec validation, retaining sanitized output.
- [x] 2.2 Merge the reviewed PR after required checks, confirm default-branch state, then synchronize/archive this change with merge evidence and strict validation; remove the clean task worktree and branch.

## Delivery evidence

- Implementation: [PR #1113](https://github.com/Artexis10/exomem/pull/1113), merged as `290dd1270c7b378abb32fdbf160efba4c4c6c81e`; reviewed files match the merge. Its clean worktree and local/remote branch were removed.
- Verification: 26 focused tests; complete local hosted set 1232 passed, 112 platform/deployment-gated skips; Ruff, Mypy and public-artifact privacy checks passed. Independent review has no outstanding findings.
- [Required CI](https://github.com/Artexis10/exomem/actions/runs/34134985466) and [hosted infrastructure validation](https://github.com/Artexis10/exomem/actions/runs/34134985428) passed on the reviewed commit.
- Live provider readback: one production branch and endpoint, 0.25–1 CU, default autosuspend and a 500-cent alert pass. The requested 100-CU-hour quota check correctly fails with `providerEnforced=false`: Neon rejected the quota write because the organization is Vercel-managed. This tooling closure does not claim that the operational hard stop is enabled.
