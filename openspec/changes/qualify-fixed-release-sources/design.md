# Design

## Context

See `proposal.md` for the problem and scope.
Full receipts already identify the tested commit and tree.
The current merge helper requires the selected source to equal current main.
The publisher also requires the tagged release commit to be an ancestor of main.
The repository permits squash merges only.

Release Please v5.0.0 supports target branches and bundles release-please 17.6.0.
It tags a merged release PR's squash commit; it offers no arbitrary target-SHA input.
A release tag outside main supplies a version but does not supply a reachable history boundary.

## Goals / Non-Goals

**Goals:** A selected source qualifies despite unrelated main merges and PR pushes.
Its artifacts contain exactly that source plus checked release metadata.
The following release includes intervening changes exactly once.

**Non-Goals:** Change the squash-only policy, replace Release Please, reduce Full coverage, or alter published releases.

## Decisions

### Use an isolated branch and the existing controller

Let C be the selected main source, H the generated release head, R its squash commit, and M the later metadata reconciliation.
Create one branch under `release-cuts/` at C, with a deterministic name derived from C.
Run Release Please against that branch from a controller executing on main.
Keep App credentials in the existing main-scoped release environment.
Pin the currently consumed Release Please action commit for this integration.

Reuse complete Full evidence for C or dispatch one fallback against the stable branch at C.
Validate H with the existing metadata tier, Cloud package check and lock check.
Before tagging, verify that R has sole parent C and the checked tree of H.
Unexpected changes stop this cut; unrelated source work continues.

The controller retires both main release-PR creation and main release creation.
Main pushes retain artifact recovery.
CI already runs on all PR bases; only release scope recognition needs extension.
Cut PRs inherit main's configured required-check policy, including integration identities.
An empty required-check list on an unprotected cut branch cannot authorize a merge.

### Keep a trusted origin for isolated tags

Reconcile a write-once record at `.github/release-cuts/<tag>.json` through a normal main PR before publisher dispatch.
The record binds C and its tree, H and its tree, R and its tree, version, Full producer and required-check policy.
It also identifies the preceding published version and its source boundary.
Read it from an explicitly fetched trusted-main revision.

The publisher verifies the tag, record, R's parent, C's main ancestry, managed delta and complete evidence before initial publication.
Retain the current ancestry path for legacy releases.
After initial acceptance, the main record preserves that decision for recovery when temporary Actions evidence expires.
Recovery still verifies the immutable tag, source binding and original distribution/image evidence.
Conflicting records or immutable bytes fail; temporarily missing graph state remains visibly pending during bounded retries.

This uses existing main trust and release evidence rather than a new attestation service.
The controller addresses a record by tag and fetches bounded history; it does not scan all releases.

### Preserve metadata and the next source boundary

Update release versions in current main, preserving its product changes and dependency selections.
Change the root package version in its current lock without resolving dependency upgrades.
Regenerate committed packages from current main; never copy an old lock or package tree over newer inputs.
If generator inputs change before reconciliation merges, refresh only that reconciliation.

Generate the record and `last-release-sha=C` together from one cut identity.
The next cut must contain the preceding reconciliation and its manifest must match the preceding published version.
Resolve the preceding reconciliation commit from the immutable record's main history; never put a commit's own hash inside its content.
For the first isolated cut, use the preceding legacy release as the ancestry anchor.
Serialize cuts through reconciliation, while ordinary source merges and PR pushes continue.

The next Release Please scan stops at C, not M.
Using M would omit changes merged while C qualified.
R must remain outside main ancestry: the upstream scan can stop at an encountered release commit before reaching an explicit boundary.

### Reuse checks and close the remaining metadata gap

Reuse `scripts/cloud-plugin.py check`, lock checks, source/tree receipts and the existing recovery tools.
The current allowlist alone does not prove generated content or whole JSON files harmless.
Compare JSON changes with the configured version updates through maintained updaters and parsed data.
During reconciliation, allow only the generated `last-release-sha` change in release configuration.
Require existing deterministic package checks before tagging; reject altered generated instructions even when their paths are allowed.
The Cloud check covers `plugins/cloud/generated`, not the committed Claude plugin tree.
Run the existing content and manifest cases from `tests/test_plugin_sync.py` in the release metadata tier.
Also validate the selected release version explicitly; the current manifest comparison deliberately ignores its version.
Do not add a second package generator or a broad test framework.

### Resume one cut instead of selecting another source

Use the existing intent and GitHub branch, PR, merge and tag identities as state.
Retrying after a squash or tag creation resumes the same C, R and version.
A duplicate intent reuses the same cut and Full producer.
An existing conflicting branch, record or tag stops that cut without rewriting it.
After publication starts, repair forward and preserve published bytes.

## Risks / Trade-offs

- History-boundary mistakes omit or repeat release notes. A two-release integration proof must expose both failures.
- Metadata reconciliation can conflict with new dependencies. Patch current metadata and retain current dependency selections.
- Isolated tags lose the old main-ancestry shortcut. A checked record on trusted main supplies the missing origin binding.
- Required checks can be absent on the cut branch. Reuse the configured main policy instead of accepting an empty result.
- Source or generator changes can delay reconciliation. They delay this release or the next cut, never unrelated delivery.
- Per-tag records and reconciliation add maintenance. Existing release generation, verification and recovery remain the owners of their jobs.

The controls prevent publication of unproved content and incorrect version history.
A false rejection delays one cut and costs the publisher time; it does not freeze main or require a new human check.

## Proof and Delivery

Use one author and an independent review of the completed batch, including the artifact trust boundary.
Reuse the current evidence/recovery tests and add cases for changed heads, missing proof, conflicting records and duplicate intents.
The highest-level proof is two cuts in a disposable local repository with real squash commits and the pinned Release Please implementation.
Use recorded API transport fixtures and block outbound writes.

1. Select C and pause its Full fixture; expect a stable cut identity.
2. Merge D with a feature, dependency and generator change; expect ordinary main work to continue.
3. Finish C's proof and cut R; expect released code and packages to exclude D.
4. Interrupt after tagging and resume; expect the same R, version and producer.
5. Reconcile onto main and cut again; expect D and its changelog entry exactly once.
6. Alter allowed JSON and generated content; expect rejection before tag creation.

The fixture proves integration and ordering, not Exomem's Full corpus.
Qualify the first ordinary cut with one authentic Full source run, then verify its actual artifacts and canary.
Record source, main advancement, CI, Python, all-image and consumer timestamps separately.
Observe ordinary concurrent work when available; another owner's activity is not a release gate.

## Migration Plan

1. Obtain approval for this contract and its single cohesive implementation batch; expect implementation authority.
2. Complete the isolated proof and independent review; expect the selected source and next notes to remain correct.
3. Enable cut creation between releases and close stale main release proposals; expect no competing release engine.
4. Publish one ordinary isolated cut and verify its provenance and canary; expect complete release evidence.
5. Synchronize and archive this change after delivery; expect canonical specifications to describe the shipped contract.

Rollback disables new cut creation and leaves main work available.
Finish or explicitly abandon an unpublished cut; preserve tags, records and bytes for any published cut.
Keep isolated-origin recovery compatible after the first isolated release.

## References

- [Release Please v5 action inputs and execution](https://github.com/googleapis/release-please-action/blob/45996ed1f6d02564a971a2fa1b5860e934307cf7/src/index.ts).
- [Release Please v17.6.0 history boundary](https://github.com/googleapis/release-please/blob/v17.6.0/src/manifest.ts#L604).
- [Release Please boundary override lifecycle](https://github.com/googleapis/release-please/blob/v17.6.0/docs/manifest-releaser.md#L127).
- [GitHub commit history ordering](https://docs.github.com/en/graphql/reference/commits#commit).
- [Simple, composable workflows](https://www.anthropic.com/research/building-effective-agents).
