## 1. Workflow

- [x] 1.1 Build the core and harness matrices in `scripts/ci_change_scope.py` from the runtime list and the shard counts that the scope job passes.
- [x] 1.2 Read the `core-tests`, `harness-tests` and `timing-tests` matrices from the scope job, and skip a tier job when its matrix is empty.
- [x] 1.3 Run a pull request's harness test modules through `--changed-tests`, as the core tier does.
- [x] 1.4 Describe the new pull request tier in `ci.yml`, `AGENTS.md` and `docs/release.md`.

## 2. Proof

- [x] 2.1 Test the matrices for a pull request without test changes, a pull request with core and harness test changes, a docs-only pull request, a push to main and a Full run.
- [x] 2.2 Simulate the scope step for a pull request that changes source only, a pull request that changes a test module, a push and a schedule.

## 3. Follow-up

- [ ] 3.1 In the first week after the merge, measure the pull request verdict time and the main push failure rate again, and compare them with the 2026-11-15 to 2026-11-22 numbers.
