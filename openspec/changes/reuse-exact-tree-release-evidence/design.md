# Design

## Context

See proposal.md for the problem. CI currently runs a reduced PR tier and a full scheduled or dispatched tier. The PR tier runs the supported floor, Python 3.11; the Full tier adds Python 3.13 and specialized jobs. The normal PR tier cannot supply historical full evidence.

The publisher already runs Python publication separately from image builds. The missing boundary is scoped qualification with an explicit producer. Existing distribution parsers and download helpers live in `infra/scripts/release_recovery.py`.

## Goals / Non-Goals

The release shall reuse full source proof across squash commits with identical trees. Pull requests, draft or ready, run the partial tier; Full runs on schedule and dispatch and supplies release evidence. Release metadata keeps its targeted checks. This change does not alter service installation authority.

## Decisions

### Retain full scope and record the tested tree

Compute Full scope once in an unconditional CI job. Select Full for schedules and manual runs only. Feed that output to runtime matrices, specialized jobs, the required gate, and receipt recording. Pull requests run the partial tier on Python 3.11, the supported floor; Python 3.13 joins in Full. Docs-only narrowing applies to every non-release pull request.

A reusable workflow keeps its caller's schedule or dispatch event, so the K3s workflow already bypasses PR path reduction for Full; its concurrency groups carry the caller workflow name. The standalone K3s workflow runs on every non-release PR that touches its paths, draft or ready. Make release evidence a trivial success outside release PRs so the strict Full gate cannot accept unexpected skips.

Record a receipt after the required gate. Bind the actual checkout commit and tree, run, issuing attempt, event, and scope. Only a schedule or dispatch run issues a receipt. Verify the receipt against its producer and compare its tested tree with merged main.

Use one `scripts/release_evidence.py` helper for evidence selection and release merge. Reuse `gh`, Git, Actions artifacts, and the existing release file checker. The helper adds identity checks that those maintained tools do not combine. Keep the implementation bounded; it does not become a second CI scheduler.

Select runs for the current source commit and its merged PR head; the head qualifies through a Full run dispatched on the source branch. Require a successful latest run and a valid receipt from its successful issuing gate. A retry may retain an earlier receipt; report its original attempt separately from the successful retry. Existing successful full runs on the exact main commit remain usable under their original event contract. An unavailable or mismatched receipt chooses the existing full fallback.

### Run advisory measurement outside the evidence run

The MCP read latency comparison gates nothing: every allowance is null, and its jobs continue on error. It ran inside the Full run, so the run could end well after its gate passed, and release evidence reads only a completed run. It now runs nightly and on dispatch in its own workflow with read-only repository access. Its advisory status is unchanged; ending it belongs to the change that owns that measurement.

### Balance the measured K3s work

The [baseline Full run](https://github.com/Artexis10/exomem/actions/runs/40294036858) records three K3s jobs at 687, 719, and 756 seconds. Other jobs stayed below ten minutes. Its JUnit records contain six import/export cases and thirteen platform cases. The largest platform case took 567.498 seconds.

Keep pytest-split and the existing isolated cluster fixtures. Use four import shards and two platform shards, with a small duration snapshot derived from those JUnit records. Duration-aware grouping keeps the large platform case alone. Reuse the existing collection test to prove nonempty, disjoint, complete shard selection. Keep timing as an observed goal, not a new timeout gate that drops work during runner delays.

### Partition paired sampling and compare complete series

Newly merged paired recall jobs exceed the target. Their [source Full run](https://github.com/Artexis10/exomem/actions/runs/40332167885) records mixed sampling at 1193 seconds, unit at 817 seconds, and filtered sampling at 700 seconds.

Reuse the existing paired runner and comparison. Add a case partition to that runner because test-level sharding cannot divide its sampling loop. Resolve the release tag and commit once before sampling. Build the complete reference corpus on each runner and warm the full selected series. Select four disjoint case subsets only after assigning the original case indices, so arm order stays unchanged. Each case retains both arms and every repeat on one runner.

Upload raw fragments with no statistical verdict. Bind their case definitions, profile, source commits, locks, encoder, and corpus. An aggregation job verifies exact case, principal, arm, and repeat coverage before calling the existing comparison once per complete series. It rejects missing, duplicate, foreign, or mixed-provenance samples. It never averages shard verdicts or confidence intervals. Retain the raw fragments and their provenance with the final series evidence. No calibration or acceptance threshold changes.

### Separate package downloads from installation

A [failed setup job](https://github.com/Artexis10/exomem/actions/runs/40356598877/job/121690618843) downloaded a package in about 149 seconds. The shared 150-second deadline then interrupted package configuration before pytest started.

Keep the existing bounded index-update loop. Use apt's maintained `--download-only` operation before one `--no-download` installation, each with its own deadline. A failed download must not start installation. An interrupted installation returns failure without automatic replay or package repair. Verify the requested packages are installed before reporting success.

The default helper allowance totals 500 seconds, including update backoffs and timeout kill grace. Give each caller nine minutes and retain its existing job cap. Prove that budget by executing the helper with isolated fake external commands and reading every workflow caller through the YAML parser. Do not parse shell source with a regex or add a downloader framework.

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
2. Dispatch Full CI on the source branch, or rely on the main run, and verify the merged tree; expect reusable source evidence.
3. Release through the guarded command and Python qualification job; expect explicit producer and timing receipts.
4. Revert the batch if evidence selection fails; expect the prior full-on-main path to remain available.
