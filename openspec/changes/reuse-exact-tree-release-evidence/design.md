# Design

## Context

See proposal.md for the problem. CI currently runs a reduced PR tier and a full scheduled or dispatched tier. The latter also covers Python 3.11 and specialized jobs. The normal PR tier cannot supply historical full evidence.

The publisher already runs Python publication separately from image builds. The missing boundary is scoped qualification with an explicit producer. Existing distribution parsers and download helpers live in `infra/scripts/release_recovery.py`.

## Goals / Non-Goals

The release shall reuse full source proof across squash commits with identical trees. Ready source PRs run historical Full coverage; drafts retain partial feedback. Release metadata keeps its targeted checks. This change does not alter service installation authority.

## Decisions

### Retain full scope and record the tested tree

Compute Full scope once in an unconditional CI job. Select Full for schedules, manual runs, and ready non-release PRs. Feed that output to runtime matrices, specialized jobs, docs scoping, the required gate, and receipt recording. Ordinary and Full-only jobs start together after this small dependency. A draft-to-ready event starts Full; readiness changes supersede older runs.

Pass Full scope into reusable workflows. The K3s workflow must bypass PR path reduction for Full and distinguish caller workflow concurrency groups. The standalone K3s workflow retains draft feedback; CI owns ready Full execution. Make release evidence a trivial success outside release PRs so the strict Full gate cannot accept unexpected skips.

Record a receipt after the required gate. Bind the actual checkout commit and tree, run, issuing attempt, event, and scope. For PRs, retain the synthetic merge commit and its head/base identities. The source head association is not the tested commit. Verify the receipt against its producer and compare its tested tree with merged main.

Use one `scripts/release_evidence.py` helper for evidence selection and release merge. Reuse `gh`, Git, Actions artifacts, and the existing release file checker. The helper adds identity checks that those maintained tools do not combine. Keep the implementation bounded; it does not become a second CI scheduler.

Select runs for the current source commit and its merged PR head. Require a successful latest run and a valid receipt from its successful issuing gate. A retry may retain an earlier receipt; report its original attempt separately from the successful retry. Existing successful full runs on the exact main commit remain usable under their original event contract. An unavailable or mismatched receipt chooses the existing full fallback.

### Balance the measured K3s work

The [baseline Full run](https://github.com/Artexis10/exomem/actions/runs/40294036858) records three K3s jobs at 687, 719, and 756 seconds. Other jobs stayed below ten minutes. Its JUnit records contain six import/export cases and thirteen platform cases. The largest platform case took 567.498 seconds.

Keep pytest-split and the existing isolated cluster fixtures. Use four import shards and two platform shards, with a small duration snapshot derived from those JUnit records. Duration-aware grouping keeps the large platform case alone. Reuse the existing collection test to prove nonempty, disjoint, complete shard selection. Keep timing as an observed goal, not a new timeout gate that drops work during runner delays.

### Stop the supported merge command on failure

Keep current-base verification and `gh pr merge` in one checked process. Verify required checks and evidence, then reread the base and head immediately before merging with `--match-head-commit`. One regression drives a failed preflight through this command and proves that merge never runs.

Replace release intent through long-lived auto-merge with explicit evidence dispatch followed by the guarded command. The evidence workflow supplies proof and refreshes checks; it does not merge. GitHub's current merge API pins only the head. A concurrent main update after the last read remains a visible limitation. Global strict checks would affect unrelated PRs and are outside this change.

The preflight prevents releasing stale source. A false refusal delays the maintainer until fresh source proof exists. It adds no human gate.

### Qualify Python at the existing publication boundary

Extend `release_recovery.py` with read-only qualification. Reuse its package parsers, release download helpers, and PyPI reader. Verify exact producer run and attempt, source, relevant successful jobs, and official distribution bytes. Retain the build job attempt as an output and pass it separately from the qualification attempt, because retries can reuse successful jobs. Retain a scoped JSON receipt as an Actions artifact and job summary. Run after Python publication, independently of image completion. Registry lag remains retryable, bounded observation; it is not an image dependency.

### Measure real boundaries

Record the ready PR/head event and its source. Measure ready-to-all-green CI and ready-to-qualified package plus images from that same event, including queue time and retries. Retain the earlier Python qualification time separately. Obtain consumer verification from the deployment owner when it exists. Report unknown consumer latency explicitly; publication cannot substitute for an installed consumer.

### Keep optional CUDA cache export outside publication

The [baseline publisher](https://github.com/Artexis10/exomem/actions/runs/40308231009) completed its CUDA image path in 292.7 seconds. An optional cache export then occupied another 1601.3 seconds after the registry push. Remove only the CUDA cache export and retain cache reads and artifact verification. A future cache miss can repeat image work; saving that work does not justify this publication delay.

## Risks / Trade-offs

- A receipt can expire: use full fallback instead of guessing its tree or scope.
- A reduced PR run looks green: retain explicit scope and require the full tier.
- Main can race the final merge: report actual ancestry and stop qualification on an unexplained mismatch.
- Image publication can fail later: retain a separate image result and keep the Python claim scoped.

## Migration Plan

1. Ship the helpers, workflow changes, and runbook in one reviewed batch; expect the new receipt contract.
2. Run parallel Full CI on the ready PR and verify the merged tree; expect reusable source evidence.
3. Release through the guarded command and Python qualification job; expect explicit producer and timing receipts.
4. Revert the batch if evidence selection fails; expect the prior full-on-main path to remain available.
