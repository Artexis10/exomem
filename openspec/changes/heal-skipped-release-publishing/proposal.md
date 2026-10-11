## Why

The 0.115.0 release has no Cloud cell image and no cellctl image, and its GitHub release has no wheel, sdist or Cloud archives. Publishing ran only in the push run that created the release, so when that run stopped, nothing published and nothing retried. The manual recovery path rebuilt only four of the six images and uploaded no release assets. It was also a second copy of the release steps.

## What Changes

- One set of publish jobs in `release-please.yml` builds, attests and publishes every artifact: the release assets, PyPI, the MCP Registry entry and the lean, ml, cuda, hosted, cloud and cellctl images.
- A `plan` job checks which artifacts of the release are present. Only a run on the release tag, at the tag's commit, publishes, and it publishes only the missing artifacts.
- Every other run hands the publish to a run on the tag. Each push to `main` does this for the newest release, so a release whose own run never published is healed. A dispatch from `main` reaches the tag the same way.
- A `dry_run` dispatch input builds every artifact and publishes nothing.
- The duplicate manual publish jobs are removed.

## Capabilities

### New Capabilities

- `release-publication`: each release artifact is published once from the release tag, and later pushes heal a skipped publish.

### Modified Capabilities

- `hosted-image-candidate-publication`: the runtime candidate comes only from a run on the release tag; a dispatch from another ref hands off instead of failing.

## Impact

- Workflow: `.github/workflows/release-please.yml`.
- Docs: `docs/release.md`.
- Recovery: `infra/scripts/release_recovery.py`, using the existing hosted verifiers.
- Tests: container distribution and release recovery regressions.
- Behaviour: a normal release now publishes from a `workflow_dispatch` run on its tag, so new runtime candidates record the tag as their attested source ref. The candidate validator already accepts that shape, and the signer workflow stays `release-please.yml`.

## Recovery correction

The first branch dry run succeeded while skipping every package and image build.
Dispatch jobs must override skipped ancestors while requiring successful direct
prerequisites. Presence checks must compare package identity and immutable bytes.

New images publish by digest first. Original attestations and the existing hosted
candidate triplet must survive before discovery tags point to that digest. Retries
recover those bytes from release assets or producer Actions artifacts, including
runs that stopped before tagging. Legacy tags without original evidence remain
incomplete until a complete successor ships. Temporary Actions artifacts and
caches are allowed during dry runs; release and registry writes are not.

The corrected recovery checks BuildKit targets for every platform and the signed
producer invocation. It checks all immutable aliases before building or tagging.
A shared image-publication concurrency group serializes floating-tag updates
across release versions; dry runs do not occupy that queue. Complete published
distributions take precedence, and partial recovery excludes dry-run producers.
