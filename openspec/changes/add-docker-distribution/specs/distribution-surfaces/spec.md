## ADDED Requirements

### Requirement: Container Image

The system SHALL provide a multi-stage `Dockerfile` at the repo root, built from local
source (never a published PyPI wheel), producing two final targets: `lean` (default —
base dependencies only, no torch, `EXOMEM_DISABLE_EMBEDDINGS=1`) and `ml` (the
`embeddings` extra with CPU-only torch, installed via a CPU-index `uv pip install`
rather than `uv sync --extra embeddings`, so the CUDA torch index pinned for Linux in
`pyproject.toml` is never triggered). GPU acceleration inside the container is out of
scope. Both targets SHALL set `EXOMEM_HOST=0.0.0.0` and `EXOMEM_LOG_DIR=/data/logs`; `ml`
SHALL additionally set `HF_HOME=/data/hf`. Both SHALL declare `VOLUME /data`, `EXPOSE
8765`, `ENTRYPOINT ["exomem"]`, and default `CMD ["--transport","http","--port","8765"]`.
Both SHALL carry OCI labels (`org.opencontainers.image.source`,
`org.opencontainers.image.licenses=AGPL-3.0-or-later`,
`org.opencontainers.image.description`) and include `/LICENSE`. Published images SHALL be
multi-arch (`linux/amd64` and `linux/arm64`).

#### Scenario: stdio one-liner needs no OAuth environment

- **WHEN** a user runs
  `docker run -i --rm -v "/path/to/vault:/vault" -e EXOMEM_VAULT_PATH=/vault ghcr.io/artexis10/exomem:latest --transport stdio`
- **THEN** the container starts and serves MCP over stdio without any GitHub OAuth
  environment variable being required

#### Scenario: HTTP boot without OAuth env fails fast

- **WHEN** the container runs with the default `CMD` (`--transport http --port 8765`)
  and `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `EXOMEM_GITHUB_USERNAME`, or
  `EXOMEM_BASE_URL` is unset
- **THEN** the process exits non-zero immediately with the existing actionable "Missing
  required env vars for GitHub OAuth" message, and no socket is bound unauthenticated

#### Scenario: lean image excludes torch

- **WHEN** the `lean` target is built and inspected
- **THEN** it contains no torch or sentence-transformers packages, and
  `EXOMEM_DISABLE_EMBEDDINGS` defaults to `1`

#### Scenario: ml image runs CPU-only

- **WHEN** the `ml` target is built and run
- **THEN** torch is present and CPU-only (`torch.cuda.is_available()` is `False`, no
  CUDA runtime bundled), and hybrid embeddings/CLIP search function without a GPU

#### Scenario: vault access is confined to the bind mount

- **WHEN** a vault directory is bind-mounted to `/vault` and `EXOMEM_VAULT_PATH=/vault`
  is set
- **THEN** all file reads and writes stay confined to that mount, matching the server's
  existing vault-containment guarantee

### Requirement: Configurable Log Directory

The system SHALL honor an `EXOMEM_LOG_DIR` environment variable that overrides where the
rotating application log (`exomem.log`) and the query/write/read JSONL audit logs
(`queries.jsonl`, `writes.jsonl`, `reads.jsonl`) are written. Both `server.run()`'s log
directory resolution and `query_log.py`'s log paths SHALL honor the same override, so no
log path stays hardcoded under the package install location. When `EXOMEM_LOG_DIR` is
unset, behavior SHALL be unchanged from today's repo-relative default.

#### Scenario: Override honored for the application log

- **WHEN** `EXOMEM_LOG_DIR=/data/logs` is set and the server starts
- **THEN** `exomem.log` is created under `/data/logs` and no write is attempted under
  the package install location

#### Scenario: Override honored for query/write/read JSONL logs

- **WHEN** `EXOMEM_LOG_DIR=/data/logs` is set
- **THEN** `queries.jsonl`, `writes.jsonl`, and `reads.jsonl` are written under
  `/data/logs`, not the repo-relative default

#### Scenario: Default is unchanged when unset

- **WHEN** `EXOMEM_LOG_DIR` is not set
- **THEN** logs are written to the existing default location exactly as before this
  change, for both the application log and the JSONL audit logs

#### Scenario: Non-root container process can always write its log directory

- **WHEN** the process runs as a non-root user inside a container with
  `EXOMEM_LOG_DIR` pointed at a writable volume (e.g. `/data/logs` under the declared
  `/data` volume)
- **THEN** log directory creation and every log write succeed without a permission
  error

### Requirement: Container CI Smoke

CI SHALL add a `docker` job that builds the `lean` target on every pull request
(`linux/amd64`, no push) and exercises it in-container without downloading any
embeddings/media model: running `exomem doctor` and a keyword `find` against the bundled
public sample vault over the CLI surface, then booting the image with HTTP transport and
dummy OAuth environment values (startup SHALL NOT contact GitHub) and verifying a `401`
response from `/mcp` and `200` responses from both `/.well-known/oauth-authorization-server`
and `/.well-known/oauth-protected-resource`.

#### Scenario: PR builds and smokes the lean image

- **WHEN** a pull request runs CI
- **THEN** the `docker` job builds the `lean` target for `linux/amd64` and does not push
  the image anywhere

#### Scenario: In-container demo over the packaged sample vault

- **WHEN** the built image is run with no vault mounted at all
- **THEN** `demo --json` exits `0` with `"success": true` — doctor, keyword `find` (with
  the known sample hit), `get`, and `audit` all pass against the sample vault packaged
  inside the image, using only the CLI surface (no MCP client required)

#### Scenario: HTTP boot proves the discovery workaround without contacting GitHub

- **WHEN** the image is booted with `--transport http` and dummy
  `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`/`EXOMEM_GITHUB_USERNAME`/`EXOMEM_BASE_URL`/
  `EXOMEM_JWT_SIGNING_KEY` values
- **THEN** startup succeeds with no outbound call to GitHub, a request to `/mcp` returns
  `401`, and both well-known OAuth discovery paths return `200`

#### Scenario: CI never downloads models

- **WHEN** the `docker` job runs
- **THEN** no embeddings or media model weights are downloaded, and only the `lean`
  target is built in CI

### Requirement: Container Publish Pipeline

On a Release Please-created release, a `publish-image` job SHALL build and push
multi-arch (`linux/amd64` + `linux/arm64`) images to `ghcr.io/artexis10/exomem`, gated on
the repository variable `GHCR_PUBLISH_ENABLED == 'true'` (mirroring the existing
`PYPI_PUBLISH_ENABLED` gate for PyPI), tagging `latest` and the released `X.Y.Z` version
for the `lean` target and `ml` / `X.Y.Z-ml` for the `ml` target. A `workflow_dispatch`
input SHALL allow republishing an already-released tag, mirroring the existing
`publish-existing-pypi` job.

#### Scenario: Release publishes both variants

- **WHEN** a release is created and `GHCR_PUBLISH_ENABLED` is `true`
- **THEN** `ghcr.io/artexis10/exomem:latest`, `:X.Y.Z`, `:ml`, and `:X.Y.Z-ml` are pushed
  for both `linux/amd64` and `linux/arm64`

#### Scenario: Publish stays off by default

- **WHEN** `GHCR_PUBLISH_ENABLED` is unset or `false`
- **THEN** the `publish-image` job does not run, mirroring PyPI's default-off gate

#### Scenario: Manual republish of an existing tag

- **WHEN** a maintainer triggers the `workflow_dispatch` input with an existing release
  tag
- **THEN** the workflow checks out that tag, verifies `pyproject.toml`'s version matches
  it, and republishes the same set of image tags

#### Scenario: Release checklist verifies the published image

- **WHEN** a maintainer follows `docs/release.md`'s checklist for a release with
  `GHCR_PUBLISH_ENABLED` on
- **THEN** the checklist includes pulling the published image and smoke-testing it
  before considering the release complete

### Requirement: Compose Remote Example

The repo SHALL provide a root `compose.yaml` with a service `exomem` (running the
published `lean` image by default, with a commented `build:` block for local builds),
`env_file: .env`, a bind-mounted vault via `EXOMEM_VAULT_HOST_PATH`, a named
`exomem-data` volume for `/data`, a healthcheck against the existing unauthenticated
`GET /.well-known/oauth-protected-resource` route, and `restart: unless-stopped`.
Ingress SHALL be offered as opt-in compose profiles (`cloudflared`, `ngrok`) reaching the
service over the compose network, with no host ports published by default.

#### Scenario: Compose up with a tunnel profile exposes nothing on the host

- **WHEN** `docker compose --profile cloudflared up` is run
- **THEN** no host port is published for the `exomem` service, and the `cloudflared`
  sidecar reaches it at `http://exomem:8765` over the compose network

#### Scenario: Healthcheck reuses an existing route

- **WHEN** the `exomem` service is healthy
- **THEN** `docker compose ps` reports it healthy based on a `GET` to
  `/.well-known/oauth-protected-resource`, with no new `/health` endpoint added anywhere
  in the system

#### Scenario: Local debugging can still bind a port

- **WHEN** a user uncomments the local port mapping in `compose.yaml`
- **THEN** the service becomes reachable at `127.0.0.1:8765` for local or debug use

#### Scenario: No ingress profile means no tunnel container

- **WHEN** `docker compose up` is run with no `--profile` flag
- **THEN** only the `exomem` service starts; neither the `cloudflared` nor the `ngrok`
  sidecar runs
