## Why

Competitor parity: engram's entire pitch is "one Docker container"; exomem has no
container story today. A container also gives the fastest zero-Python onboarding path
(a `docker run` stdio one-liner for Claude Code — no `uv`, no Python, no clone) and a
self-contained remote deployment (a `compose` stack with a tunnel sidecar, no manual
NSSM/launchd/systemd service install).

The archived `2026-06-30-complete-oss-readiness` change explicitly scoped this out —
its proposal lists "Docker images, PyPI publishing automation, hosted control-plane, or
new server capabilities" as out of scope. This change picks up the Docker piece of that
deferred list as its own capability.

## What Changes

- **Multi-stage `Dockerfile` at the repo root**, base `python:3.12-slim`, `uv` copied
  from the pinned `ghcr.io/astral-sh/uv` image (builder stage only), built from **local
  source** — never PyPI — so CI can build and smoke the image on every PR before a
  release exists, and release images build from the release tag. Two final targets:
  `lean` (default; base dependencies only, no torch, `EXOMEM_DISABLE_EMBEDDINGS=1`) and
  `ml` (the `embeddings` extra with CPU-only torch). The `ml` builder cannot
  `uv sync --extra embeddings`, because `pyproject.toml`'s `[tool.uv.sources]` pins a
  CUDA (`cu132`) torch index for `sys_platform == 'win32' or 'linux'` — Linux is exactly
  the container's platform, so a plain `uv sync` there would try to pull multi-GB CUDA
  wheels into a CPU-only image. Instead the `ml` builder installs CPU torch from
  `https://download.pytorch.org/whl/cpu` via `uv pip install`, then
  `uv pip install ".[embeddings]"` — `uv pip` does not consult `[tool.uv.sources]`, so
  torch is not reinstalled from the CUDA index. GPU-in-Docker is explicitly out of
  scope. Both final targets set `EXOMEM_HOST=0.0.0.0` (the existing env-wins-over-flag
  precedent in `server.run()`), `EXOMEM_LOG_DIR=/data/logs`; `ml` additionally sets
  `HF_HOME=/data/hf`. Both declare `VOLUME /data`, `EXPOSE 8765`,
  `ENTRYPOINT ["exomem"]`, `CMD ["--transport","http","--port","8765"]` — the existing
  actionable `RuntimeError` for missing OAuth env fires unchanged, so a misconfigured
  container fails fast instead of silently serving unauthenticated. OCI labels
  (`org.opencontainers.image.source`, `.licenses=AGPL-3.0-or-later`, `.description`) plus
  `COPY LICENSE /LICENSE`. Multi-arch `linux/amd64` + `linux/arm64` at publish time via
  buildx; PR builds stay single-arch (`linux/amd64`) for CI speed.
- **New required code change: `EXOMEM_LOG_DIR` env override for the log directory.**
  Today `logging_config.py`'s caller and `query_log.py` both hardcode logs under the
  package-parent directory (`Path(__file__).resolve().parents[2] / "logs"`) — inside a
  pip-installed container that resolves under `site-packages`, which a non-root
  container user cannot write to, crashing on the first log line. `server.run()`'s log
  directory resolution and `query_log.py`'s log paths both honor `EXOMEM_LOG_DIR` when
  set, falling back to today's exact default when unset. This is a genuine gap beyond
  Docker — the last hardcoded path in an otherwise env-driven server — surfaced by
  building the container.
- **Root `compose.yaml`**: service `exomem` (image `ghcr.io/artexis10/exomem:latest`
  with a commented `build:` block for local builds; `env_file: .env`; vault bind mount
  via `EXOMEM_VAULT_HOST_PATH` → `/vault`; named volume `exomem-data` → `/data`;
  healthcheck `GET http://127.0.0.1:8765/.well-known/oauth-protected-resource` — the one
  already-unauthenticated route, no new `/health` endpoint needed; `restart:
  unless-stopped`) plus ingress sidecars as compose **profiles**: `cloudflared`
  (`cloudflare/cloudflared`, token-run, reaches `http://exomem:8765` over the compose
  network) and `ngrok` (`ngrok/ngrok`, static dev domain). No published host ports with
  a tunnel profile active; a commented `127.0.0.1:8765` mapping for local/debug use.
- **Allowlist-style `.dockerignore`** (`*` then `!src/`, `!pyproject.toml`, `!uv.lock`,
  `!README.md`, `!LICENSE`) so `.env`, `logs/`, and vault leftovers can never enter the
  build context regardless of what a contributor's working tree happens to contain.
- **CI**: a new `docker` job in `ci.yml` builds the `lean` target on every PR
  (`linux/amd64`, no push) and smokes it in-container: `exomem doctor` plus a `find`
  against the bundled sample vault over the CLI surface, then boots HTTP with dummy
  OAuth env (startup never calls GitHub) and curls a 401 from `/mcp` and 200s from both
  well-known OAuth discovery paths — proving the claude.ai discovery workaround is live
  inside the image without any model download. Release: a `publish-image` job appended
  to `release-please.yml`, gated on `vars.GHCR_PUBLISH_ENABLED == 'true'` (mirroring the
  existing `PYPI_PUBLISH_ENABLED` gate), `permissions: packages: write`, login with
  `GITHUB_TOKEN`, buildx + QEMU for multi-arch, tags
  `ghcr.io/artexis10/exomem:{latest, X.Y.Z, ml, X.Y.Z-ml}`; plus a `workflow_dispatch`
  republish path mirroring the existing `publish-existing-pypi` job.
- **Docs**: new `docs/docker.md` (the stdio one-liner headline:
  `claude mcp add exomem -- docker run -i --rm -v "/path/to/vault:/vault" -e EXOMEM_VAULT_PATH=/vault ghcr.io/artexis10/exomem:latest --transport stdio`;
  compose remote walkthrough; tag/size expectations — `lean` ~250-350MB uncompressed
  vs. `ml` ~1.5-2GB; the `/data` volume contract; a `--user` hardening note; a Windows
  caveat that WSL2 bind mounts don't propagate `inotify`, so the live file-watcher
  misses out-of-band edits and NTFS-backed mounts are slower — Windows users should
  prefer the native install for daily editing). README's Install section gains the
  docker one-liner; `docs/deployment.md`'s service section gains "Option 0: docker
  compose"; `docs/release.md`'s checklist gains a pull-and-smoke-the-published-image
  step.
- **Tests**: `tests/test_log_dir_override.py` (`EXOMEM_LOG_DIR` honored by the logging
  config and by `query_log`; default behavior unchanged when unset). The container
  smoke itself is CI-only and is documented as such in `tasks.md` — it needs a Docker
  daemon that isn't available to the unit test suite.

## Capabilities

### New Capabilities

- `distribution-surfaces`: packaged, versioned container images (`lean` and `ml`
  variants) built from local source with documented stdio and HTTP contracts, a vault
  volume contract, CI build+smoke on every PR, a gated GHCR publish pipeline on release,
  and a self-contained `compose.yaml` remote deployment with opt-in tunnel-sidecar
  ingress profiles.

This change is kept **single-capability**. The `EXOMEM_LOG_DIR` override is real
production code (not docs-only), but its only motivating scenario is the non-root
container log path this change introduces, it adds no new `doctor` check, and it does
not change any documented install-readiness contract (the default path is byte-identical
when the variable is unset) — so `install-readiness`'s spec does not need a delta. If a
future change gives `doctor` its own `EXOMEM_LOG_DIR`-aware check, that is the point to
add a Modified Capability there.

## Impact

- New repo files: `Dockerfile`, `compose.yaml`, `.dockerignore`, `docs/docker.md`,
  additions to `.github/workflows/ci.yml` (`docker` job) and
  `.github/workflows/release-please.yml` (`publish-image` job +
  `workflow_dispatch` republish input).
- Code: `src/exomem/logging_config.py` / `src/exomem/server.py` (`EXOMEM_LOG_DIR`
  resolution for the application log), `src/exomem/query_log.py` (`EXOMEM_LOG_DIR`
  resolution for the queries/writes/reads JSONL logs).
- Docs: `README.md` (Install section), `docs/deployment.md` (service options),
  `docs/release.md` (release checklist).
- Tests: `tests/test_log_dir_override.py` (new, pure logic — no Docker daemon
  required). Container build/smoke stays CI-only; no vault migration, no MCP/REST/CLI
  schema change, no new reasoning surface.
