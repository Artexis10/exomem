## 1. Release pull request

- [x] 1.1 Skip the test jobs on the release-please pull request except the wheel onboarding gate, and run `uv lock --check` in the build job; verify the contract suite in `tests/test_ci_reliability_contract.py` passes.
- [x] 1.2 Refuse a release pull request from another repository or with any change release-please did not make, using `scripts/check_release_pr_files.py` from the base revision under `shell: bash`; verify `tests/test_check_release_pr_files.py` passes and the checker passes real release pull requests and refuses one with a hand commit.

## 2. Docs-only pull requests

- [x] 2.1 Decide the docs-only scope from the complete merge diff in `scripts/ci_change_scope.py` and narrow the core and harness shards to `-m reads_repository_docs` and omit the installed journey with a job-summary notice; verify the scope cases in `tests/test_ci_reliability_contract.py` pass.
- [x] 2.2 Fail any test or module that reads a docs-only path without the marker through the audit hook in `tests/repository_docs_reads.py`, and mark every module that the first guarded CI run named; verify `tests/test_repository_docs_reads.py` passes and fails with the hook blinded.
