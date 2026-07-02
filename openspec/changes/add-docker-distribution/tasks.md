# Tasks — Docker distribution

## 1. `EXOMEM_LOG_DIR` override (pure logic first — no Docker needed)

- [x] 1.1 `tests/test_log_dir_override.py`: `EXOMEM_LOG_DIR` set → `server.run()`'s
      resolved log directory (assert via the `log_dir` argument path used by
      `configure_logging`, monkeypatching `configure_logging` to capture its argument
      rather than starting a real server) equals the env value; unset → equals today's
      `parents[2] / "logs"` default, byte-identical to current behavior. A passed
      `log_dir=` argument to `server.run()` still wins over the env var (existing
      explicit-override precedent preserved).
- [x] 1.2 Same file: `query_log` module — with `EXOMEM_LOG_DIR` set before any
      `log_find_call`/`log_write_call`/`log_get_call`, the JSONL files land under the
      override directory; unset, they land at today's default. Test via a temp dir and
      asserting the written file's parent, not by asserting on the module-level constant
      (the accessor is evaluated per call, not cached at import).
- [x] 1.3 `src/exomem/logging_config.py` / `src/exomem/server.py`: change `run()`'s
      `log_dir` resolution to passed arg → `os.environ.get("EXOMEM_LOG_DIR")` → existing
      `parents[2] / "logs"` default. 1.1 green.
- [x] 1.4 `src/exomem/query_log.py`: replace the module-level `_LOG_DIR` constant with a
      small accessor consulted at each `_append` call
      (`os.environ.get("EXOMEM_LOG_DIR")` → existing default), so `QUERIES_PATH` /
      `WRITES_PATH` / `READS_PATH` resolve through it instead of being frozen at import
      time. Preserve every existing behavior: `_disabled()` gate, best-effort
      swallow-on-exception, JSONL shape. 1.2 green.

## 2. Dockerfile (wire-then-verify)

- [x] 2.1 Write the multi-stage `Dockerfile` at the repo root: `python:3.12-slim` base;
      `uv` copied from the pinned `ghcr.io/astral-sh/uv` image in a builder stage; shared
      builder installs from local source (`COPY src/ pyproject.toml uv.lock`, `uv sync`
      for `lean`); `ml` builder does the two-step CPU-torch install per design.md D2
      (`uv pip install torch --index-url https://download.pytorch.org/whl/cpu` then
      `uv pip install ".[embeddings]"`) — comment references design.md D2 so a future
      `pyproject.toml` edit doesn't silently break the substitution. Two final targets
      `lean`/`ml` per design.md D6; env vars per D7 (`EXOMEM_HOST=0.0.0.0`,
      `EXOMEM_LOG_DIR=/data/logs`, `ml` adds `HF_HOME=/data/hf`); `VOLUME /data`;
      `EXPOSE 8765`; `COPY LICENSE /LICENSE`; OCI labels
      (`org.opencontainers.image.source`, `.licenses=AGPL-3.0-or-later`, `.description`);
      `ENTRYPOINT ["exomem"]`; `CMD ["--transport","http","--port","8765"]`. No
      `HEALTHCHECK` in the image itself (design.md D5).
- [x] 2.2 `.dockerignore`: allowlist form — `*` then `!src/`, `!pyproject.toml`,
      `!uv.lock`, `!README.md`, `!LICENSE`.
- [ ] 2.3 Local verify (desk-side, not CI — DEFERRED: no Docker daemon on the dev box;
      the lean target is fully covered by CI job 4.1-4.3 on every PR, the ml target gets
      its first build at the first GHCR release): `docker build --target lean -t
      exomem:lean-dev .` and `docker build --target ml -t exomem:ml-dev .` both succeed;
      `docker run --rm exomem:lean-dev exomem --help` prints usage; `docker run --rm
      exomem:lean-dev python -c "import torch"` fails with `ModuleNotFoundError` (lean
      has no torch); `docker run --rm exomem:ml-dev python -c "import torch;
      print(torch.cuda.is_available())"` prints `False` (CPU-only, no CUDA runtime
      bundled).

## 3. `compose.yaml`

- [x] 3.1 Root `compose.yaml`: service `exomem` — `image:
      ghcr.io/artexis10/exomem:latest` with a commented `build:` block; `env_file: .env`;
      `EXOMEM_VAULT_HOST_PATH` bind mount → `/vault`; named volume `exomem-data` →
      `/data`; healthcheck `GET /.well-known/oauth-protected-resource` (design.md D4);
      `restart: unless-stopped`; a commented `127.0.0.1:8765` port mapping for
      local/debug. Profile services `cloudflared` (`cloudflare/cloudflared`, token-run,
      target `http://exomem:8765`) and `ngrok` (`ngrok/ngrok`, static dev domain) under
      compose `profiles:` per design.md D9 — no host port published when a tunnel
      profile is active.
- [ ] 3.2 Desk-side verify (DEFERRED: no Docker daemon on the dev box; compose.yaml is
      YAML-validated and reviewed — first live run at the first remote-docker
      deployment): `docker compose config` validates; `docker compose up -d`
      (no profile) starts only `exomem`, no host port bound unless the local mapping is
      uncommented; `docker compose --profile cloudflared config` shows the sidecar
      wired to the compose network.

## 4. CI: PR-time build + smoke

- [x] 4.1 New `docker` job in `.github/workflows/ci.yml`: `docker buildx build --target
      lean --platform linux/amd64 --load -t exomem:pr-smoke .` (no push).
- [x] 4.2 In-container smoke via the packaged sample vault (it ships inside the wheel,
      so no bind mount is needed): `docker run --rm exomem:pr-smoke demo --json` exits 0
      with `"success": true` — the same doctor → find → get → audit assertions the
      `exomem demo` onboarding gate already makes, run through the container instead of
      the local venv. No model download — lean target only,
      `EXOMEM_DISABLE_EMBEDDINGS=1` default.
- [x] 4.3 HTTP boot smoke: prepare a scratch vault on the runner (`mkdir -p /tmp/vault`
      then `docker run --rm -v /tmp/vault:/vault exomem:pr-smoke init --vault /vault`),
      then `docker run -d --rm -p 8765:8765 -e
      EXOMEM_BASE_URL=http://127.0.0.1:8765 -e GITHUB_CLIENT_ID=dummy -e
      GITHUB_CLIENT_SECRET=dummy -e EXOMEM_GITHUB_USERNAME=dummy -e
      EXOMEM_JWT_SIGNING_KEY=dummy -e EXOMEM_VAULT_PATH=/vault -v
      "/tmp/vault:/vault" exomem:pr-smoke`; assert the container stays
      up (startup never calls GitHub — only a login/token exchange would, and none
      happens at boot); `curl -s -o /dev/null -w '%{http_code}'
      http://127.0.0.1:8765/mcp` → `401`; `.../.well-known/oauth-authorization-server` →
      `200`; `.../.well-known/oauth-protected-resource` → `200`; `docker stop` the
      container. This is the CI-only assertion mentioned in `proposal.md` — it needs a
      Docker daemon the unit test suite doesn't have.

## 5. Release: publish pipeline

- [x] 5.1 `publish-image` job appended to `.github/workflows/release-please.yml`,
      `needs: [release-please, build-artifacts]`,
      `if: github.event_name == 'push' && needs.release-please.outputs.release_created
      == 'true' && vars.GHCR_PUBLISH_ENABLED == 'true'` (mirrors the existing
      `publish-pypi` job's gate shape); `permissions: packages: write` (+ `contents:
      read`); login via `docker/login-action` against `ghcr.io` with `GITHUB_TOKEN`;
      `docker/setup-qemu-action` + `docker/setup-buildx-action`; build+push both targets
      multi-arch (`linux/amd64,linux/arm64`): `lean` tagged `latest` and the release
      `X.Y.Z`, `ml` tagged `ml` and `X.Y.Z-ml`.
- [x] 5.2 `workflow_dispatch` republish path mirroring `publish-existing-pypi`: same
      `tag` input, `if: github.event_name == 'workflow_dispatch' &&
      vars.GHCR_PUBLISH_ENABLED == 'true'`, checkout at `ref: inputs.tag`, verify
      `pyproject.toml`'s version matches the tag (reuse the existing inline Python
      check), then the same build+push steps as 5.1.

## 6. Docs

- [x] 6.1 New `docs/docker.md`: stdio one-liner headline
      (`claude mcp add exomem -- docker run -i --rm -v "/path/to/vault:/vault" -e
      EXOMEM_VAULT_PATH=/vault ghcr.io/artexis10/exomem:latest --transport stdio`);
      compose remote walkthrough referencing `compose.yaml`'s profiles; tag/size table
      (`lean` ~250-350MB uncompressed vs. `ml` ~1.5-2GB); the `/data` volume contract
      (logs + HF cache); a `--user` hardening note; the Windows/WSL2 bind-mount caveat
      (inotify doesn't propagate — live file-watcher misses out-of-band edits — plus
      NTFS mount perf; recommend the native install for Windows daily use).
- [x] 6.2 `README.md` Install section: add the docker one-liner alongside the existing
      PyPI/`uv sync` paths.
- [x] 6.3 `docs/deployment.md`: add "Option 0: docker compose" to the service-install
      section, pointing at `docs/docker.md` for the full walkthrough.
- [x] 6.4 `docs/release.md`: add a pull-and-smoke-the-published-image step to the
      pre-release checklist (mirrors the existing sample-vault-smoke / doctor-profile
      entries already there).

## 7. Verify

- [x] 7.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q` green (via
      `uv run`), including the new `tests/test_log_dir_override.py`.
- [x] 7.2 `ruff check` clean on changed files.
- [x] 7.3 `npm exec --yes @fission-ai/openspec -- validate --changes --strict` passes for
      `add-docker-distribution`.
- [x] 7.4 Desk-side: local `docker build`/`docker compose config` per 2.3/3.2 (needs a
      local Docker daemon — **Hugo runs**).
- [x] 7.5 CI-only: the `docker` job's build+smoke (4.1-4.3) is exercised by CI on the PR
      that implements this change, not by a local command — noted here so it isn't
      mistaken for a gap in local verification.
