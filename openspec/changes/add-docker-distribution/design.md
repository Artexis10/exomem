# Design — Docker distribution

## D1. Build from local source, not a published PyPI wheel

**Decision:** every image — PR-time CI build and release build alike — builds from the
checked-out source tree (`COPY src/ pyproject.toml uv.lock`), never `pip install
exomem==X.Y.Z`.

**Rejected alternative — publish-from-PyPI-wheel base.** Building `FROM` a PyPI release
would mean CI could only smoke a container for a version that is already public, which
inverts the safety property a PR gate is supposed to have: the whole point is to catch a
broken image *before* it reaches users. A PyPI-wheel base also couples the Docker
pipeline's freshness to the PyPI publish pipeline's cadence for no benefit — local-source
builds are strictly more capable (same mechanism serves PR smoke and release publish) and
no slower in practice (`uv sync`/`uv pip install` against the lockfile is the same either
way).

## D2. `ml` target: CPU-index torch via `uv pip`, not `uv sync --extra embeddings`

`pyproject.toml` pins `[tool.uv.sources] torch = [{ index = "pytorch-cu132", marker =
"sys_platform == 'win32' or sys_platform == 'linux'" }]`. The container's platform is
Linux, so a plain `uv sync --extra embeddings` inside the `ml` builder would resolve
torch against the `cu132` (CUDA 13.2) index — multi-GB CUDA wheels baked into an image
that ships no GPU and no CUDA runtime, and that likely fails to even import correctly
without the matching driver/userspace libraries.

**Decision:** the `ml` builder stage does two separate installs:

1. `uv pip install torch --index-url https://download.pytorch.org/whl/cpu` — the CPU-only
   wheel, explicit index, bypassing `[tool.uv.sources]` entirely.
2. `uv pip install ".[embeddings]"` — the rest of the `embeddings` extra
   (`sentence-transformers`, `pillow`). `uv pip` (unlike `uv sync`/`uv add`) does not
   consult `[tool.uv.sources]`/`[tool.uv.index]`, so it does not re-resolve or reinstall
   torch against the pinned CUDA index; the already-installed CPU wheel satisfies the
   `torch>=2.12` requirement.

**Rejected alternative — `uv sync --extra embeddings` with a build-time env override
(`UV_TORCH_BACKEND=cpu` or similar).** `README.md`/`pyproject.toml` already documented a
prior decision *against* `UV_TORCH_BACKEND=auto` for reproducibility (it resolves
per-machine at install time, breaking lockfile parity across hosts) — reusing that same
per-machine resolution knob just to dodge the pin inside Docker would reintroduce exactly
the non-reproducibility that pin exists to prevent, and would silently diverge from
`uv.lock`. The two-step `uv pip install` keeps `uv.lock` authoritative for every
non-torch dependency and makes the CPU-torch substitution an explicit, auditable step in
the Dockerfile itself.

## D3. GPU-in-Docker is out of scope

No CUDA runtime, no `nvidia-container-toolkit` documentation, no GPU compose profile.
Rationale: the desktop/service install already documents the CUDA path in full
(`docs/deployment.md`'s GPU/CUDA notes) for users who need GPU throughput; Docker's job
here is zero-Python onboarding and a self-contained remote box, not a GPU-accelerated
deployment target. Revisit only if a user demonstrates a real need for GPU passthrough
inside a container (a materially more complex host/driver/image contract).

## D4. No new `/health` endpoint

**Decision:** the compose healthcheck (and any future orchestrator health probe) targets
the existing `GET /.well-known/oauth-protected-resource` route — already served
unauthenticated for claude.ai's OAuth discovery — instead of adding a dedicated
`/health`.

**Rejected alternative — a new `/health` route.** Would be a second unauthenticated
endpoint doing strictly less than the existing one (the existing route already proves
the HTTP server is up, FastMCP is mounted, and — when auth is configured — the OAuth
metadata route is wired correctly, which is a stronger signal than a bare 200). Adding it
would also be new attack surface and a new documented contract for zero additional
information. This route only exists when `require_auth` is true (HTTP transport), which
is exactly the case compose targets — stdio users have no HTTP surface to health-check in
the first place.

## D5. `HEALTHCHECK` lives in compose, not the Dockerfile

**Decision:** no `HEALTHCHECK` instruction in the `Dockerfile` itself; the healthcheck is
declared only in `compose.yaml`.

**Rejected alternative — a Dockerfile-level `HEALTHCHECK`.** A `HEALTHCHECK` baked into
the image would run unconditionally, including for the primary intended interactive use
of this image — `docker run -i --rm ... --transport stdio` for Claude Code — where there
is no HTTP server to probe at all. That would make every stdio-mode container report
`unhealthy` forever, which is misleading noise for `docker ps`/`docker inspect` on a
container that is working exactly as intended. Compose-level healthcheck applies only to
the long-running HTTP deployment where it's meaningful, and can be overridden or
disabled per-deployment without touching the image.

## D6. Two final targets, one Dockerfile

`lean` and `ml` are multi-stage targets in the same `Dockerfile` (`FROM base AS lean`,
`FROM base AS ml`, shared builder stages), not two separate Dockerfiles. This keeps the
shared setup (base image, `uv` copy, non-root user, `COPY LICENSE`, OCI labels, entrypoint
shape) in one place and lets `docker buildx build --target lean|ml` select the variant —
matching how CI/release both need to build both variants from the same commit without
drift between two hand-maintained files.

`lean` is the default target (`docker build .` with no `--target` builds `lean`) because
it is the fast, small, zero-model-download path that matches the stdio one-liner's
onboarding promise; a user who wants hybrid search opts in explicitly with `--target ml`
or the `:ml` published tag.

## D7. Env-var contract inside the container

- `EXOMEM_HOST=0.0.0.0` — required because the container's loopback is not reachable
  from the host/orchestrator network; `server.run()` already resolves
  `$EXOMEM_HOST > passed host > 127.0.0.1`, so this is a pure env-var set, no code change.
- `EXOMEM_LOG_DIR=/data/logs` (new override, see below) — keeps logs under the declared
  `/data` volume instead of failing to write under `site-packages`.
- `ml` additionally sets `HF_HOME=/data/hf` so downloaded model weights persist across
  container restarts under the same `/data` volume instead of re-downloading into an
  ephemeral layer on every restart.
- `VOLUME /data` is the single persistence boundary for both logs and (on `ml`) the HF
  cache; the vault itself is a separate bind mount (`/vault`) the user controls, kept out
  of `/data` so vault content and server-local state have distinct lifecycles (a user can
  `docker volume rm` the server state without touching their vault).
- `CMD ["--transport","http","--port","8765"]` is the default so `docker run
  ghcr.io/artexis10/exomem:latest` (no args) does something reasonable (serve HTTP) for
  a first-time `docker run --help`-style exploration, while the stdio one-liner
  explicitly overrides `--transport stdio` as documented in `docs/docker.md`. The
  existing `RuntimeError` on missing OAuth env fires unchanged in the HTTP default case —
  a container started with no `.env`/`-e` flags exits immediately with the actionable
  message rather than binding a socket with no auth.

## D8. `EXOMEM_LOG_DIR`: override, not a new logging subsystem

**Problem:** `server.run()` defaults `log_dir` to
`Path(__file__).resolve().parents[2] / "logs"` when the caller passes none, and
`query_log.py` computes a module-level `_LOG_DIR` the same way. Both resolve to the
*source-checkout* layout (`src/exomem/../../logs` = repo root `logs/`). Under a
pip-installed package, `parents[2]` from an installed `site-packages/exomem/server.py`
lands somewhere under (or above) `site-packages` — not writable by a non-root container
user, and not a sensible location even when writable.

**Decision:** both call sites check `os.environ.get("EXOMEM_LOG_DIR")` first and fall
back to today's exact default when unset, so:

- `server.run()`'s `log_dir` resolution becomes: passed `log_dir` param (unchanged, still
  wins when a caller supplies one explicitly) → `EXOMEM_LOG_DIR` env → today's
  `parents[2] / "logs"` default. This preserves the existing precedent that `server.run`
  accepts an explicit override while adding an env-driven one for the common case (no
  code caller, just a launched process).
- `query_log.py`'s `QUERIES_PATH`/`WRITES_PATH`/`READS_PATH` stop being constants computed
  once at import time from a hardcoded path; the log directory is resolved through a
  small accessor consulted at each write, so setting `EXOMEM_LOG_DIR` before the module
  is used (the only realistic use — env vars are set before the process starts) takes
  effect, and tests can flip the env var and observe the new location without needing to
  reload the module via import-order tricks.

**Rejected alternative — leave `query_log.py` on its hardcoded default and only fix
`server.run()`.** `query_log.py`'s three JSONL logs are exactly as production-critical as
`exomem.log` (query/write/read audit trail) and share the identical failure mode inside a
container — fixing one call site and not the other would leave a silent gap the first
time someone runs `find` in the container and the process crashes trying to create
`queries.jsonl` under an unwritable path.

**Non-goals:** no change to rotation policy, format, or the existing
`EXOMEM_DISABLE_QUERY_LOG`/`EXOMEM_DISABLE_EMBEDDINGS` opt-outs in `query_log.py` — this
is purely a directory-resolution override, keeping the default behavior for every
existing non-container install (desktop, NSSM/launchd/systemd service) byte-identical.

## D9. Ingress as compose profiles, not a fixed second service

`cloudflared` and `ngrok` are declared as compose **profiles**
(`docker compose --profile cloudflared up`), not always-on services, and not a single
hardcoded choice. Rationale: a self-contained remote deployment needs *some* public
ingress, but which tunnel provider a user already has an account with varies, and many
users running `compose` purely for local/LAN use want neither — `docker compose up` with
no profile flag starts only the `exomem` service, no host port published, and no
egress-tunnel container running. This mirrors the existing desktop-install precedent of
documenting Tailscale Funnel vs. Cloudflare Tunnel as parallel options in
`docs/deployment.md`, translated into compose's native profile mechanism instead of a
second doc branch.

## Risks

- **CPU-torch substitution silently regressing to CUDA.** If a future `pyproject.toml`
  edit changes the `embeddings` extra's torch pin in a way `uv pip install ".[embeddings]"`
  can no longer satisfy with the pre-installed CPU wheel, the build would either fail
  loudly (version conflict) or — worse — quietly re-resolve. Mitigated by CI building the
  `lean` target only (cheap, every PR) and by documenting the two-step install inline in
  the Dockerfile with a comment pointing at this design doc's D2, so a future editor sees
  the constraint before touching either the extra or the Dockerfile. The `ml` target
  itself is not part of the PR gate (no GPU-less CI runner can meaningfully exercise CPU
  embeddings within CI time budget); it is validated at release-build time and by desk-side
  smoke.
- **Multi-arch build cost/flake at release time.** `buildx`+QEMU cross-emulation for
  `arm64` on an `amd64` GitHub-hosted runner is slower and has occasionally been flaky in
  other projects. Mitigated by keeping multi-arch scoped to the release publish job only
  (not the PR gate) and by the `workflow_dispatch` republish path being able to retry
  independently of a new release.
- **`.dockerignore` allowlist drift.** An allowlist (`*` then explicit `!` entries) fails
  safe (a forgotten new source directory is excluded from the build, causing an obvious
  `ModuleNotFoundError` at build/run time) rather than unsafe (a denylist that misses a
  newly-added sensitive file type would silently leak it into the image). The CI docker
  job building successfully on every PR is the drift detector for the safe-failure case.
- **Windows + WSL2 bind-mount caveat undersold.** Documented explicitly in
  `docs/docker.md` (inotify doesn't propagate through a WSL2 bind mount, so the live
  file-watcher misses out-of-band vault edits, plus NTFS-backed mount I/O is slower) so a
  Windows user doesn't file a "file watcher doesn't work" report against a configuration
  this change never claims to support well — the native install remains the recommended
  path for Windows daily use.
