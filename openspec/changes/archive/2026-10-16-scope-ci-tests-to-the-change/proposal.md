## Why

The Actions account runs at its 20-concurrent-job cap, so every job-minute is
queue time for every other pull request. Two kinds of pull request ran the full
test tier on changes that the tier cannot observe:

- A pull request that changes only docs, OpenSpec or root Markdown ran about
  206 runner-minutes of tests that read none of those paths.
- The release-please pull request, whose diff is version lines, the changelog
  and regenerated Cloud packages, re-ran the tier on every merge to main. Its
  test evidence is already the green full run that release evidence requires at
  its base revision.

The installed product journey contract allowed omission only for OpenSpec-only
changes, so narrowing either run is a contract change.

## What Changes

- Define the docs-only set from the repository layout: `docs/`, `openspec/`,
  and root Markdown other than `README.md`. None is a wheel or sdist input, and
  no runtime code reads them.
- A docs-only pull request runs only the tests marked `reads_repository_docs`.
  A test-process audit hook fails any test that reads a docs-only path without
  the marker, so the marked set stays complete on every ordinary run. The
  installed journey does not run on a docs-only pull request and reports the
  omission in its job summary.
- The release pull request runs no test job except the wheel onboarding gate.
  Release evidence refuses it unless a green full run exists at its base
  revision and every change is release-please's own work.
- Main, scheduled and manual runs keep the whole tier, including the installed
  journey.

## Impact

- Affected spec: `product-e2e` (Tiered model and media gates).
- Affected code: `.github/workflows/ci.yml`, `scripts/ci_change_scope.py`,
  `scripts/check_release_pr_files.py`, `tests/repository_docs_reads.py`, and the
  `reads_repository_docs` marker on test modules that read docs-only paths.
- A test that reads docs only through a child process or a stat call is not
  seen by the guard; the full push run on main still runs it.
