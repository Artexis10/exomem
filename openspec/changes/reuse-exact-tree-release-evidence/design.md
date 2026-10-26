# Design

## Context

See proposal.md for the problem. CI currently runs a reduced PR tier and a full scheduled or dispatched tier. The latter also covers Python 3.11 and specialized jobs. The normal PR tier cannot supply historical full evidence.

The publisher already runs Python publication separately from image builds. The missing boundary is scoped qualification with an explicit producer. Existing distribution parsers and download helpers live in `infra/scripts/release_recovery.py`.

## Goals / Non-Goals

The release shall reuse full source proof across squash commits with identical trees. This change does not weaken the full suite, expand every PR into a heavy run, or change service installation authority.

## Decisions

### Retain full scope and record the tested tree

Add a small CI receipt after the required gate. Record the actual checkout commit and tree, run, attempt, event, and scope. Only the existing scheduled or dispatched full tier qualifies initially. A full dispatch on a source PR branch can qualify its later identical merged tree. Ordinary PR and docs-only results remain partial.

Use one `scripts/release_evidence.py` helper for evidence selection and release merge. Reuse `gh`, Git, Actions artifacts, and the existing release file checker. The helper adds identity checks that those maintained tools do not combine. Keep the implementation bounded; it does not become a second CI scheduler.

Select runs for the current source commit and its merged PR head. Verify exact run and attempt identity, workflow, successful full scope, and tested tree. Read the immutable artifact for that run and attempt. Existing successful full runs on the exact main commit remain usable under their original event contract. An unavailable or mismatched receipt chooses the existing full fallback.

### Stop the supported merge command on failure

Keep current-base verification and `gh pr merge` in one checked process. Verify required checks and evidence, then reread the base and head immediately before merging with `--match-head-commit`. One regression drives a failed preflight through this command and proves that merge never runs.

Replace release intent through long-lived auto-merge with explicit evidence dispatch followed by the guarded command. The evidence workflow supplies proof and refreshes checks; it does not merge. GitHub's current merge API pins only the head. A concurrent main update after the last read remains a visible limitation. Global strict checks would affect unrelated PRs and are outside this change.

The preflight prevents releasing stale source. A false refusal delays the maintainer until fresh source proof exists. It adds no human gate.

### Qualify Python at the existing publication boundary

Extend `release_recovery.py` with read-only qualification. Reuse its package parsers, release download helpers, and PyPI reader. Verify exact producer run and attempt, source, relevant successful jobs, and official distribution bytes. Retain the build job attempt as an output and pass it separately from the qualification attempt, because retries can reuse successful jobs. Retain a scoped JSON receipt as an Actions artifact and job summary. Run after Python publication, independently of image completion. Registry lag remains retryable, bounded observation; it is not an image dependency.

### Measure real boundaries

Record the reviewed source-ready timestamp and its source before the first release. Record scoped qualification from the new producer receipt. Obtain consumer verification from the deployment owner when it exists. Report unknown production latency explicitly; publication cannot substitute for an installed consumer.

## Risks / Trade-offs

- A receipt can expire: use full fallback instead of guessing its tree or scope.
- A reduced PR run looks green: retain explicit scope and require the full tier.
- Main can race the final merge: report actual ancestry and stop qualification on an unexplained mismatch.
- Image publication can fail later: retain a separate image result and keep the Python claim scoped.

## Migration Plan

1. Ship the helpers, workflow changes, and runbook in one reviewed batch; expect the new receipt contract.
2. Run full CI on that source branch and verify the merged tree; expect reusable source evidence.
3. Release through the guarded command and Python qualification job; expect explicit producer and timing receipts.
4. Revert the batch if evidence selection fails; expect the prior full-on-main path to remain available.
