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

## Commit convention

Release Please reads Conventional Commit messages after the latest release tag:

- `fix: ...` -> patch release
- `feat: ...` -> minor release
- `feat!: ...` or a `BREAKING CHANGE:` footer -> major release

Use scopes when helpful, for example `feat(doctor): ...` or
`fix(media): ...`. `docs:`, `ci:`, and `chore:` are hidden from the public
changelog by default and do not drive releases unless they include a breaking
marker.

## Release flow

1. Merge feature/fix PRs to `main` using Conventional Commit titles.
2. Release Please opens or updates a release PR that bumps `pyproject.toml`,
   `.release-please-manifest.json`, and `CHANGELOG.md`.
3. Confirm CI and the pre-release checks above.
4. Merge the Release Please PR.
5. Release Please tags `vX.Y.Z` and creates the GitHub Release.
6. The release workflow builds `dist/` with `uv build` and uploads the wheel/sdist
   to the GitHub Release.
7. If `PYPI_PUBLISH_ENABLED=true`, the release workflow builds the same package
   artifacts in the `pypi` environment and publishes them through PyPI trusted
   publishing.

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
