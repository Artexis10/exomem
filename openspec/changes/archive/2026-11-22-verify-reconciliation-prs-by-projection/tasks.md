## 1. Reconciliation tier

- [x] 1.1 Classify a same-repository `release-reconcile/<source>` PR to `main` as release scope in `release_evidence.ci_scope`.
- [x] 1.2 Add `release_cuts.py check-reconciliation`, which applies the controller's own prospective-tree test against the projection onto the PR base.
- [x] 1.3 Run the check from the base checkout in the `release evidence` job, with a read-only token.
- [x] 1.4 Test that a projection, a branch updated from `main` and a race repair pass, and that a forged code change fails.
- [x] 1.5 Run the check against real reconciliation PRs #3032, #3021 and #2822 (passed) and a feature PR head (refused).
