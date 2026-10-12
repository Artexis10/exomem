## Why

The 0.115.0 release initially lacked Cloud and cellctl images, package files and Cloud archives. Publication depended on its release-created push run. A stopped run left those artifacts missing. The manual recovery path covered only some images and duplicated the publication steps.

## What Changes

- One set of publish jobs in `release-please.yml` builds, attests and publishes every artifact: release assets, PyPI, the MCP Registry entry and the five supported images. The image roles are lean, ml, cuda, cloud and cellctl.
- A `plan` job checks which artifacts of the release are present. Only a run on the release tag, at the tag's commit, publishes, and it publishes only the missing artifacts.
- Every other run hands the publish to a run on the tag. Each push to `main` does this for the newest release, so a release whose own run never published is healed. A dispatch from `main` reaches the tag the same way.
- A `dry_run` dispatch input builds every artifact and publishes nothing.
- The duplicate manual publish jobs are removed.

## Capabilities

### New Capabilities

- `release-publication`: each release artifact is published once from the release tag, and later pushes heal a skipped publish.

## Impact

- Workflow: `.github/workflows/release-please.yml`.
- Docs: `docs/release.md`.
- Recovery: `infra/scripts/release_recovery.py`, using `gh attestation verify` and generic image receipts.
- Tests: container distribution and release recovery regressions.
- Behaviour: a normal release publishes from a `workflow_dispatch` run on its tag. The signer workflow stays `release-please.yml`.

## Recovery correction

The first branch dry run succeeded while skipping every package and image build.
Dispatch jobs must override skipped ancestors while requiring successful direct
prerequisites. Presence checks must compare package identity and immutable bytes.

New images publish by digest first. Original image attestations must survive
before discovery tags point to that digest. Retries
recover those bytes from release assets or producer Actions artifacts, including
runs that stopped before tagging. Legacy tags without original evidence remain
incomplete until a complete successor ships. Temporary Actions artifacts and
caches are allowed during dry runs; release and registry writes are not.

Current publication and automatic recovery exclude Hosted images and Records
rollback claims. Historical Hosted assets retain their original filenames,
producer fields and signed bytes. Their original source-commit verifier checks
the candidate and both bundles; both immutable aliases must match its digest.

The corrected recovery checks BuildKit targets for every platform and the signed
producer invocation. It checks all immutable aliases before building or tagging.
A shared image-publication concurrency group serializes floating-tag updates
across release versions; dry runs do not occupy that queue. Complete published
distributions take precedence, and partial recovery excludes dry-run producers.
