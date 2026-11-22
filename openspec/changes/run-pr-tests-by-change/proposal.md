## Why

GitHub runs at most 20 hosted jobs at once for this repository's account. From 2026-11-15 to 2026-11-22, a pull request waited 59 minutes at the median and 119 minutes at p90 for its CI verdict. Each pull request run used about 45 jobs and 225 runner-minutes. The 24 core shards used 76% of those minutes and the 6 harness shards used 13%. Every pull request queued those 30 shard jobs, whatever it changed. The maintainer asked for fast, efficient CI that runs the broad suite less often than on every pull request.

A selection of tests by import graph was measured and rejected: it gave a small selection for only 8 of 47 recent code pull requests.

## What Changes

- A pull request runs no core or harness shard. It keeps every other partial-tier job: lint, build, specs, capabilities, onboarding, release evidence, serial timing, structural recall, the Windows held filesystem, TUI, product end-to-end, quality and the gate.
- A pull request runs the test modules that it adds or changes, and a fixed, declared set of repository-wide guard modules (`tests/pr_guard_modules.txt`) that a change elsewhere can break. Core modules run on the supported floor Python and the newest supported runtime. Harness modules run on the floor. A docs-only pull request runs no guard module.
- Every push to main runs every core and harness shard on the floor Python. A newer push to main cancels the run it supersedes.
- The Full tier, docs-only narrowing and the release and reconciliation pull requests do not change.
- A failure that only the shards on main or the Full tier catch is fixed forward on main. A failing push run on main opens or updates one `ci-main-failing` tracking issue, and a green push run closes it.
- The scope job builds the core and harness matrices from one list of runtimes and one shard count per tier.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `software-release`: a pull request runs its own changed test modules and the declared guard modules instead of the core and harness shards, every push to main runs every partial shard on the floor Python, and a failing main push run is tracked in one issue.

## Impact

- `scripts/ci_change_scope.py` builds the core and harness matrices and the runtime list for each event.
- `.github/workflows/ci.yml`: `core-tests`, `harness-tests` and `timing-tests` read their matrices from the scope job.
- `tests/pr_guard_modules.txt` lists the guard modules, and `scripts/ci_change_scope.py` adds them to every non-docs pull request.
- `.github/workflows/ci.yml` gains `main-failing-issue`, which runs only on pushes to main after `gate` and holds `issues: write`.
- `AGENTS.md` and `docs/release.md` describe the new pull request tier and the tracking issue.
- A defect that a pull request's changed tests do not reach is found on main, after the merge, instead of before it. The first week after the merge measures the pull request verdict time and the main push failure rate.
