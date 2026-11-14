<!-- authority:non-specification -->

# Release checklist

exomem is source-first today: users can install from a checkout with `uv`.
Release Please manages version bumps, `CHANGELOG.md`, tags, and GitHub Releases.
When `PYPI_PUBLISH_ENABLED=true` is configured for the repository, release-created
workflows also publish the wheel/sdist to PyPI using trusted publishing.

## Versioning policy

The source of truth is `[project].version` in `pyproject.toml`, updated by
Release Please. Tags use `vX.Y.Z`.

While the project is pre-1.0 (`0.y.z`):

- Bump **minor** for new public CLI/MCP/REST behavior, vault-schema changes, or
  compatibility changes a user might need to notice.
- Bump **patch** for bug fixes, docs, packaging polish, CI, sample-vault updates,
  and implementation changes that do not alter the public surface.
- Call out breaking pre-1.0 changes in the release notes even when they are
  represented as a minor bump.

After `1.0.0`, use standard SemVer:

- **MAJOR** for incompatible changes to public CLI/MCP/REST behavior, stored
  vault conventions, or required environment semantics.
- **MINOR** for additive public behavior.
- **PATCH** for compatible fixes and docs.

## Pre-release checks

Run from the repo root:

```bash
uv sync
uv run python -m pytest -q
uvx ruff check .
npm exec --yes @fission-ai/openspec -- validate --specs --strict
uv run exomem demo --json
uv run python scripts/generate-capabilities.py --check
uv build
```

After a release with GHCR publishing enabled, smoke the published image:

```bash
docker run --rm ghcr.io/artexis10/exomem:latest demo --json
```

Optional host-specific checks:

```bash
uv run python -m exomem doctor --profile hybrid
uv run python -m exomem doctor --profile media
uv run python -m exomem doctor --profile remote
```

Run the optional checks only on machines configured for those profiles. `media`
expects the media extra plus Tesseract; `remote` expects OAuth and public-url
environment variables.

## Rolling a release onto a running service

Use `scripts/deploy.ps1 -Version X.Y.Z` rather than upgrading by hand. It resolves the
service interpreter from NSSM instead of assuming the current checkout, gates on `doctor`,
and verifies the running server actually reports the requested version before claiming
success. See [deployment.md](deployment.md#deploying-a-new-version).

Two release-time hazards it exists to catch:

- **The checkout is not necessarily the deploy target.** A wheel-backed service venv can sit
  beside a checkout whose `uv sync` has no effect on what runs. `/health` now reports
  `install_source`, so this is visible rather than inferred.
- **The CUDA pin does not survive a PyPI upgrade.** `[tool.uv.sources]` is repo
  configuration and does not travel with the published wheel, so upgrading a PyPI-backed
  venv silently swaps the pinned `+cu132` torch for the default CPU wheel. The deploy fails
  on that regression; pass `-AllowCpuTorch` on hosts that are intentionally CPU-only.

### Managed Linux service: what to check after the handoff

On a managed Linux/WSL unit, `bash scripts/upgrade.sh` stages the release and
hands off through the standby sequence: the candidate warms beside the worker
that is still serving, and only then is ingress paused, drained and cut over.
The script prints the handoff record. Check three things in it:

- `standby=ready` — the candidate warmed. `discarded` names the component it was
  waiting on and means the upgrade fell back to a cold replacement; the endpoint
  was still served throughout, but the first writes afterwards will be slower.
- `migration=skipped(declared_none)` — the target declared no state migration, so
  the offline migrator did not run. `ran` is expected when the release changes
  the state descriptor set.
- `snapshot=current` — the checkpoint the standby proved was still the one on
  disk at promotion. `rebuild-after-promotion` means the re-proof failed and the
  coalesced rebuild path is repairing it; expect slower first writes.

A non-zero `adoption.residue` in `/health/ready`'s `cutover` block is normal for
an upgrade during traffic: the standby adopted the pages the outgoing worker
left deferred and queued them for incremental repair. Reads that require a
current projection keep refusing until that repair drains, which it does in the
background without a client call.

Then confirm `/health/ready` reports `status: ready` with `cutover.standby` false.
The budgets and the full sequence are in
[managed-service-upgrades.md](managed-service-upgrades.md#the-standby-sequence).

Record warm recall latency after readiness. This records the live state and
does not gate the release. Keep live queries outside the repository.
The [latency reference](../benchmarks/recall-latency/README.md) defines the case
file, count source, contention rule and content-free summary schema.

1. Prepare a private live case file with forty queries for each declared series; the file stays on the served host.
2. Run the gate's `--corpus-counts` mode against the served vault; it writes actual counts in buckets of 500.
3. Run `--served-url` with `--live-cases`, `--live-corpus` and `--output`; the summary records passed, failed, refused or not measured.
4. Run `--validate-summary` against the summary; validation rejects content fields before publication.
5. Upload the summary as `recall-latency-live-cell-YYYYMMDDTHHMMSSZ.json`; the latest attempt supplies the release's live state.
6. If the state failed or refused, open an issue naming the release, series and failed stage rows; follow-up blocks nothing.

Use the release's existing upload path, with `TAG` and `RECALL_SUMMARY` set to
the release tag and the validated summary file:

```bash
gh release upload "$TAG" "$RECALL_SUMMARY"
```

A release without an attached live summary remains not measured. Preserve a
refused attempt beside a later repeat. Never publish the live case file.

After a new tag, compare `benchmarks/recall-latency/accept.json` with that tag.
If the record names an older tag, remove it in a pull request unless the next
accept record replaces it first. An expired record excuses no paired regression.

### Semantic-authoring migration note

This release tightens new in-process writes of active compiled notes. New active
creates, replacement successors, and inactive-to-active transitions now need at
least one valid, non-empty compact or rich semantic unit. Inactive drafts remain
allowed, existing pages are grandfathered for unrelated edits, moves retain that
status, and direct-editor changes are preserved and reported as debt. Tier-2
create, overwrite, and append operations evaluate the complete resulting
Markdown under the same rule. Call the intended writer with `validate_only=true`
to receive `missing_semantic_unit` or `empty_rich_unit` remediation without
mutation. See [first-class semantic language](semantic-language.md) for the exact
versioned grammar, applicability predicate, exemptions, and remediation.

The richer heading parser now keeps deeper headings inside their parent rich
unit and excludes recognized blocks with no substantive body. Its parser/index
version bump invalidates stale derived rows; it does not rewrite Markdown. After
an upgrade, follow the [derived-state rebuild guidance](deployment.md#deploying-a-new-version).

### Lifecycle status registry: downgrading below this release

The release that adds the lifecycle status registry lets the activation manifest
record a page `status` and its `class_at_activation`. An older release accepts
only the four original page fields, so it refuses a manifest that records either.

Do not skip step 3. If you skip it on a vault whose manifest records a page
`status`, every write on the older release refuses with
`ACTIVATION_MANIFEST_INVALID` until you delete the file.

1. Downgrade the service to the older release.
2. Open `Knowledge Base/_Schema/semantic-activation.yaml` in the vault.
3. If a page entry in that file records `status`, delete the file.
4. Make one ordinary write.

The older release prepares its own manifest on the first write that needs one,
and writes stop refusing. The new manifest takes the corpus at that moment as
its activation baseline.

### Parts in projected search: downgrading below this release

The release that adds the parts of a long Source to projected search refreshes
the governed catalog of an enrolled vault when the service starts. The refresh
writes the catalog rows at store version 3, which holds the part text. An older
release reads only store version 2 and cannot rebuild a catalog that it cannot
read. On an older release, an enrolled vault serves no projected search, and
every content write refuses with `GOVERNANCE_CATALOG_PUBLICATION_BLOCKED`.

Do not downgrade the service of an enrolled vault below this release. The same
failure occurs on a replica that runs an older release than the replica that
refreshed the catalog. If a service or a replica runs an older release:

1. Upgrade that service or replica to this release or a later one.
2. Make one ordinary write.

The write succeeds, and projected search answers again. The upgraded release
reads the refreshed catalog as it is, so no rebuild step is necessary. A vault
that is not enrolled in governance has no governed catalog, and a downgrade
does not affect it.

## Commit convention

Release Please reads Conventional Commit messages after the latest release tag:

- `fix: ...` -> patch release
- `feat: ...` -> minor release
- `feat!: ...` or a `BREAKING CHANGE:` footer -> major release

Use scopes when helpful, for example `feat(doctor): ...` or
`fix(media): ...`. `docs:`, `ci:`, and `chore:` are hidden from the public
changelog by default and do not drive releases unless they include a breaking
marker.

GitHub squash merges use the pull-request title as the resulting commit header,
so the `Conventional Commit title` PR check validates this exact shape:

```text
<lowercase-type>[optional scope][!]: <non-empty description>
```

For example, `fix(release): preserve parsed squash commits` and
`feat(api)!: replace the response contract` pass; `Fix the release pipeline`
does not. Any lowercase Conventional Commit type is parseable, but only the
non-breaking types configured in `release-please-config.json` drive a normal
version bump or receive a named changelog section. A breaking `!` on any
parseable type can drive the configured breaking-release bump.

The `main` branch ruleset must require the `Conventional Commit title` status
check. Without that repository setting, the workflow reports an invalid title
but GitHub does not block the merge. To check a title locally, run:

```bash
python scripts/check_pr_title.py "fix(release): preserve parsed squash commits"
```

## Release flow

Pull requests, draft or ready, run the partial tier on Python 3.11, the
supported floor. Docs-only pull requests narrow it to the tests that read docs.
Full coverage runs only on schedule or dispatch, on main or on a source branch
on request. It covers Python 3.11 and 3.13, every specialized Full job, and the
latency gate. Paired recall uses four
raw case shards per series and one complete-series comparison. Each shard
retains the full corpus and both arms of every assigned case. Discovery pins
the release baseline once; aggregation refuses incomplete or mixed fragments.
The instrument check runs once before fan-out, and sampler elapsed records
remain distinct from complete-series verdicts. New CI timing remains unverified.
The advisory MCP read latency comparison runs nightly in its own workflow
(`mcp-read-latency.yml`), so a Full run ends when its required gate passes.
Release metadata keeps its targeted checks backed by source evidence.

One scope job selects the CI policy: Full runs only on schedule or dispatch.
After its required gate passes, a Full run retains `full-ci-evidence-RUN-ATTEMPT`
with the actual checkout commit and Git tree. The selector verifies repository,
workflow, issuer gate, full scope, and the tested commit. It compares that tested
tree with merged main. Release evidence is a Full run on main, or a Full run
dispatched on the source branch whose tree equals the merged tree. Identical
trees reuse proof after squash; a changed tree, expired receipt, or incomplete
proof uses the Full fallback. Legacy receipt-free evidence remains eligible only
for a scheduled or dispatched Full run at its exact main commit.

A retry can inherit an earlier successful gate. The receipt keeps its issuing
attempt and names the latest successful attempt separately. Qualification does
not rerun Full solely because a retry copied the successful gate.

Select one merged source commit. The controller creates `release-cuts/<source>`
at that commit and runs the pinned Release Please action against that branch.
Later main commits do not change the selected source. Main remains available
for normal merges and PR pushes.

1. Resolve the merged source; expect one full immutable commit identity.

```bash
source_commit=$(gh api "repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/commits/main" --jq .sha)
```

2. Start or resume the cut; expect one controller run on main for that source.

```bash
gh workflow run release-evidence-automation.yml --ref main -f source="$source_commit"
```

3. Read the controller and publisher summaries; expect separate source, Python, image and consumer states.

This is one operation. The controller creates the proposal, regenerates packages,
retains the selected head, qualifies evidence, merges, tags, reconciles and dispatches
publication. It reads App credentials only from the main-scoped `release`
environment. `RELEASE_AUTO_MERGE=off` stops landing and native tag creation.

The controller reuses authentic Full evidence for the source or an identical
tested tree. It reuses an existing matching Full run before requesting one fallback
on the stable cut branch. A retained intent prevents duplicate fallbacks.
The intent stays on the cut PR and binds its source, checked head, check policy
and Full producer. A nightly run without a cut intent cannot merge or tag.

Cut branches inherit main's configured required check contexts and integration
identities. Missing checks remain pending. An empty policy, a failed policy read,
a failed check or another App's check cannot authorize a merge.
A checkpoint retains pointers; it supplies no accepted proof. Every unaccepted
retry derives the version and predecessor from immutable commits, authenticates the
exact Full producer, and reads current required contexts and App identities.
After squash, the guarded publisher uses Release Please 17.6.0's native release
operation. Its mutation boundary checks the actual candidate's tag, version and
squash against that authenticated identity. A conflicting tag stops without a rewrite.
Accepted main origins retain authority when temporary proof expires or policy changes.

The metadata tier checks the Cloud packages, committed Claude plugin content,
selected plugin version, lockfile and configured version updates. An allowed
JSON path permits only the maintained updater's version changes.

After the squash, the controller verifies its sole parent and checked tree before
Release Please creates the native tag. It then opens a main PR that patches current
metadata and regenerates packages from current main. The PR preserves intervening
product changes and dependency selections. The controller compares the prospective merge tree with regenerated outputs from
that same main revision. Unrelated main advancement needs no refreshed checks.
A conflict or different generated tree refreshes only reconciliation.
The controller checks the actual squash against its actual parent and repairs a
raced result before it declares reconciliation complete. It retains the source's
Full evidence throughout. A resumed older cut preserves a newer accepted version
and source boundary.

The reconciliation records `last-release-sha` as the selected source, not the later
metadata commit. The next source must contain this reconciliation and its published
manifest version. Before a new cut, authentic Full can supply the generated-package
proof only when its receipt names the current package-check contract. That contract
covers the lock, Cloud output and committed plugin content, manifest and selected version.
Otherwise, a local projection prevents an unfinished repair from starting another
Full run. A wrong rejection delays only that cut and costs one local package check.
The isolated release commit stays outside main ancestry so the
next native scan can reach that source boundary. Only cuts and reconciliation
serialize.

The write-once record at `.github/release-cuts/<tag>.json` binds the source,
reviewed head, squash, trees, version, Full producer, required checks and predecessor.
The publisher reads it from an explicitly fetched main revision before publishing.
It rejects changed, deleted or replaced records. Legacy releases retain their
main-ancestry path. Accepted records remain available after temporary Actions
artifacts expire; recovery still verifies original distributions and image evidence.

Retry the same source when a controller run stops. A retry after tagging keeps the
same version, tag, squash and Full producer. Changed cut content, tag conflicts and
conflicting immutable bytes stop that cut. Temporarily missing graph or check state
remains pending during bounded observation. A missing Full dispatch after an interrupted
request remains visible; investigate that request rather than dispatching another fallback.

Record the source-ready event, main advancement, exact Full producer, Python
qualification, all-image qualification and consumer verification separately.
Ready-to-artifacts time includes queueing and retries. Publication time cannot
replace a missing consumer timestamp.

### Migration and rollback

Deploy the controller between releases and close the stale main release proposal.
Main pushes then create neither release proposals nor releases; they retain artifact
recovery. Existing published tags and recovery evidence remain unchanged.

Set `RELEASE_AUTO_MERGE=off` to stop new landing. Finish or explicitly abandon an
unpublished cut before selecting another. Preserve every tag, accepted origin and
published byte after publication starts. Keep isolated-origin verification available
when disabling new cut creation. No repository-wide delivery hold is needed.

The publisher builds or recovers the wheel, sdist, and Cloud archives from the
tag. It publishes missing enabled artifacts. Python qualification runs after
`build-artifacts` and `publish-pypi`; it does not depend on image jobs or the
whole workflow conclusion. A skipped PyPI job can qualify when complete matching
distributions were already present.

The `python-qualification-RUN-ATTEMPT` artifact and job summary identify the tag,
source, exact producer artifact, wheel and sdist hashes, and qualification time.
The build job records its original attempt as an output. Qualification keeps
that identity when GitHub carries a successful build into a later retry.
The receipt reports the verification attempt separately. Manual qualification
uses `--attempt` for the original build and `--verification-attempt` for the
publication retry to inspect. Verification defaults to the original attempt.
The helper verifies actual bytes from the producer, GitHub Release, and PyPI.
It retries missing PyPI distributions for at most six observations, ten seconds
apart. Conflicting immutable bytes fail immediately.

Python qualification leaves images unqualified and consumer verification
unverified. It grants no installation or rollout authority. Report source-ready
to Python-qualified latency from the recorded timestamps. Report ready-to-artifacts
latency only after Python and all required images qualify. Keep consumer installation
and rollout as a separate boundary. Report ready-to-production latency only after
the deployment owner supplies consumer verification and its timestamp.
Publication time cannot replace missing consumer evidence.

Every later push to `main` checks the newest release again and hands any missing
artifact to a run on its tag. Recovery verifies filenames, hashes, original image evidence, and each discovery
tag. It preserves published bytes and tagged digests. After three failed publish runs on one tag, pushes stop handing
that tag off.

### Publish a release by hand

A tag released before this workflow existed refuses the hand-off. For any other
tag, use these steps:

1. Run `gh workflow run release-please.yml --ref main -f tag=vX.Y.Z -f dry_run=true`.
2. Check that run's `plan` summary and image builds. The dry run builds every
   artifact and publishes nothing.
3. Run `gh workflow run release-please.yml --ref main -f tag=vX.Y.Z`.

The run on `main` hands the publish to a run on `refs/tags/vX.Y.Z`. That run
publishes only the artifacts the release lacks.

A branch dry run uses the dispatch commit's workflow and recovery tools. It
builds the selected tag's product source. Record both commits separately.
The dry run bypasses original-image recovery and publication, so it proves
build scheduling rather than publication acceptance or the current product source.

New images publish by digest. The workflow verifies and retains original
attestations before attaching version, commit, or floating tags to that digest.
Every supported image retains its original Sigstore bundle as a release asset.

A retry searches release assets and producer Actions artifacts, even when no
version tag exists. It fills missing files and tags from verified original bytes.
Partial release evidence can recover from a complete producer artifact.

Image publishers share one Actions concurrency group across release versions.
The workflow checks the latest release inside that group before each floating-tag
update. A running image build delays the next publisher; dry runs use separate
groups and do not occupy the publication queue.

A legacy image without original evidence remains visibly incomplete. It needs a
complete successor release.

Raw image recovery checks BuildKit's selected target for every platform and
GitHub's signed producer run and attempt. The receipt filename only locates the
evidence. Recovery checks every immutable alias before rebuilding and checks all
tag conflicts before its first publication write.

PyPI completeness requires both distribution formats with matching filenames
and SHA256 hashes. Complete published distributions take precedence over Actions
artifacts. For partial distributions, recovery selects a publication attempt
that matches the published bytes. GitHub step records exclude historical dry
runs; new dry runs use separate artifact names. Recovery downloads the exact
artifact ID and does not combine bytes from different producers.

Cloud completeness uses the committed release metadata. Conflicting immutable bytes stop publication for the release maintainer to resolve.
Dry runs still build during registry outages. They may retain temporary Actions
artifacts and caches, but create no release assets, registry publications,
attestations, deployments, or live records.

### Verify historical Hosted receipts

Current publication and automatic recovery support lean, ml, cuda, cloud and
cellctl. Historical Hosted assets remain unchanged, including their filenames,
signed bytes, producer fields, version alias and commit alias.

Use the original standalone verifier from the receipt's exact source commit.
Keep the maintained `gh` CLI available on `PATH` for attestation verification.

1. Resolve the historical release tag to its source commit; expect the receipt's `source.commit` to match.
2. Read `infra/scripts/hosted_image_candidate.py` from that commit with `git show`; expect the original standalone verifier.
3. Download the original candidate and both Sigstore bundles without renaming them; expect the three unchanged release assets.
4. Run the verifier's `verify` command with `--candidate`, `--candidate-bundle` and `--bundle`; expect authenticated image and candidate subjects.
5. Compare both immutable aliases with the verified image digest; expect the version and commit aliases to match.

This verifies historical publication evidence. It does not qualify the retired
runtime or establish a current rollback target.

The initial `0.1.0` baseline is recorded in `.release-please-manifest.json` and
`CHANGELOG.md`; future releases should come from Release Please rather than
manual version edits.

## PyPI trusted publishing

PyPI publishing is wired but off by default. To enable it:

1. Create the PyPI project and configure a trusted publisher for this repository.
2. Use workflow `.github/workflows/release-please.yml`, environment `pypi`, and
   the `publish-pypi` job.
3. Add a GitHub repository variable `PYPI_PUBLISH_ENABLED` with value `true`.
4. Keep the `pypi` environment protected if a manual approval gate is desired.

No long-lived PyPI token is required; the publish job uses GitHub OIDC (`id-token:
write`) and `pypa/gh-action-pypi-publish`.
