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

1. Merge feature/fix PRs to `main` using Conventional Commit titles.
2. Release Please opens or updates a release PR that bumps `pyproject.toml`,
   `.release-please-manifest.json`, and `CHANGELOG.md`.
3. Confirm CI and the pre-release checks above.
4. **Required: run one full CI pass on `main` and confirm it is green before
   merging the release PR.** The release PR runs lint, build, specs,
   capabilities, the wheel onboarding gate and release evidence, and no other
   test job: the full run at its base revision is its test evidence. Release
   evidence also refuses a release PR that changes any file release-please
   does not manage, so land a fix
   through its own pull request, never by a push to the release branch. Full
   CI (retrieval quality/latency, governance wire characterization, semantic
   write latency, graph convergence, docker, the frozen verifier and source K3s
   integration) runs on the nightly schedule and
   on explicit dispatch, never per push to the release branch
   (each release-PR push used to re-trigger a 46-job full run; 61% of a
   measured day's runner-minutes went to those stampedes while ordinary PRs
   queued). Dispatch, then watch the run this dispatch created — selecting by
   `--limit 1` alone can grab a *previous* dispatch run, so filter on runs
   created after a timestamp captured before dispatching (the retry loop
   covers the API lag before the new run appears):

   ```bash
   ts=$(date -u +%Y-%m-%dT%H:%M:%SZ); gh workflow run ci.yml --ref main
   until run_id=$(gh run list --workflow=ci.yml --branch=main --event=workflow_dispatch --created ">$ts" --limit 1 --json databaseId --jq '.[0].databaseId') && [ -n "$run_id" ]; do sleep 5; done; gh run watch "$run_id" --exit-status
   ```

   A green nightly run on the same `main` revision serves equally; a red or
   missing full run blocks the release-PR merge. The full run cancels the
   unfinished push run at the same revision itself, because it runs every job
   that the push run runs.

   Cloud image builds and smoke checks still run on the release pull request.
   The version checker parses Python and TOML assignments, so a version marker
   cannot hide another runtime or dependency change.
5. Merge the Release Please PR.
6. Release Please tags `vX.Y.Z` and creates the GitHub Release.
7. The release workflow's `plan` job finds the release artifacts that are missing
   and hands the publish to a run of the same workflow on the `vX.Y.Z` tag.
8. The run on the tag builds `dist/` with `uv build` and uploads the wheel, the
   sdist and the Cloud archives to the GitHub Release.
9. The same run publishes the missing artifacts whose gate is on:
   - PyPI, from the same wheel and sdist, if `PYPI_PUBLISH_ENABLED=true`;
   - the MCP Registry entry, if `MCP_REGISTRY_PUBLISH_ENABLED=true`;
   - the lean, ml, cuda, cloud and cellctl images, if
     `GHCR_PUBLISH_ENABLED=true`.

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
