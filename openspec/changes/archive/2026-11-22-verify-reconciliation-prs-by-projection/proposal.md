## Why

After each release, the cut controller opens a reconciliation PR that records the release metadata on `main`. That PR ran the whole pull-request test tier, about 45 jobs, while the controller waits 20 minutes for its checks. Between 2026-11-15 and 2026-11-22, 28 cuts failed with "reconciliation is pending", and each one needed a manual retry. The PR changes only what the controller generates, so its test jobs prove nothing that `main` has not already proved.

## What Changes

- A PR from `release-reconcile/<source>` to `main`, in the same repository, gets the release tier, so no test job runs.
- The `release evidence` job checks out the checker at the PR base and runs `release_cuts.py check-reconciliation`. The PR passes only when merging its head onto the base gives exactly what the controller projects for the accepted origin of that source. This is the same test the controller applies before it merges.
- Any other content fails the required gate.

## Impact

- `software-release`: the PR-tier requirement names the reconciliation tier.
- Code: `scripts/release_cuts.py`, `scripts/release_evidence.py`, `.github/workflows/ci.yml`.
