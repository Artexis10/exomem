## 1. Read-only audit

- [x] 1.1 Add red-first tests for passing controls, capacity/inventory drift, absent alerts, optional quota units, malformed/partial provider data, command timeout and secret-sentinel suppression; implement the minimal audit and pass `pytest -q tests/test_hosted_database_budget.py`.
- [x] 1.2 Add the operational runbook with read-modify-read controls, cutoff semantics and retained idle baseline; verify its commands against Neon v2 and pass `tests/test_openspec_only.py`.

## 2. Delivery

- [ ] 2.1 Independently review the diff and rerun the negative audit cases; run the real read-only audit, relevant hosted infra/static suites and strict OpenSpec validation, retaining sanitized output.
- [ ] 2.2 Merge the reviewed PR after required checks, confirm default-branch state, then synchronize/archive this change with merge evidence and strict validation; remove the clean task worktree and branch.
