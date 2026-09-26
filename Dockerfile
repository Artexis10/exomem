# exomem — multi-stage container image.
#
# Five final targets share this one file:
#   lean (target `lean`, DEFAULT — base deps only, no torch, no model download)
#   ml   (target `ml`   — the `embeddings` extra, CPU-only torch, no CUDA runtime)
#   cuda (target `cuda` — the `embeddings` extra, CUDA-capable torch, CPU-default at idle)
#   hosted (target `hosted` — fixed non-root identity and immutable release metadata)
#   cloud (target `cloud` — Exomem Cloud cell runtime, derived from `hosted`)
#
# `lean` is intentionally the LAST stage in this file: `docker build .` / `docker
# buildx build .` with no `--target` builds the final stage in the file, and lean
# is meant to be that zero-argument default (matches the stdio one-liner's
# zero-Python, zero-model-download onboarding promise — see docs/docker.md).
# Build `ml` explicitly with `--target ml` (or pull the published `:ml` tag).
# Build `cuda` explicitly with `--target cuda` (or pull the published `:cuda` tag).
#
# Every stage builds from the checked-out source tree (COPY src/ pyproject.toml
# uv.lock), never a published PyPI wheel — see design.md D1.

ARG UV_VERSION=0.11.28

# COPY --from does not support ARG expansion; a global-scope FROM alias does
# (buildx's documented workaround), so the pin lives in one place above.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

########################################################################
# builder-lean — base dependencies only, installed from local source.
# Never touches pyproject.toml's cu132 (CUDA) torch index: `uv sync` here
# only resolves the base `dependencies = [...]` list, which has no torch.
########################################################################
FROM python:3.12-slim AS builder-lean
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
# Deterministic, reproducible build: install the venv at a fixed path, never
# let uv silently download its own Python (python:3.12-slim already has one),
# and copy (not hardlink) into the venv so it survives the COPY --from into a
# later stage cleanly.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

# Matches .dockerignore's allowlist exactly — nothing else is needed to build.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-editable

########################################################################
# builder-ml — adds the `embeddings` extra (hybrid search) with CPU-only
# torch. See design.md D2 for the full rationale; summary:
#
# pyproject.toml pins `[tool.uv.sources] torch = [{ index = "pytorch-cu132",
# marker = "sys_platform == 'win32' or sys_platform == 'linux'" }]`. This
# container's platform IS Linux, so a plain `uv sync --extra embeddings` here
# would resolve torch against the cu132 (CUDA 13.2) index — multi-GB CUDA
# wheels baked into an image with no CUDA runtime and no GPU. Instead:
#
#   1) `uv pip install torch --index-url .../cpu` — the CPU-only wheel from
#      an explicit index, bypassing [tool.uv.sources] entirely.
#   2) `uv pip install ".[embeddings]"` — the rest of the extra. `uv pip`
#      (unlike `uv sync`/`uv add`) does NOT consult [tool.uv.sources] or
#      [tool.uv.index], so it does not re-resolve/reinstall torch against the
#      CUDA index; the already-installed CPU wheel satisfies the `torch>=2.12`
#      requirement.
#
# If a future pyproject.toml edit changes the `embeddings` extra's torch pin
# in a way the pinned CPU wheel below can no longer satisfy, step 2 fails
# loudly (version conflict) — see design.md's Risks section before changing
# either the extra or this Dockerfile.
########################################################################
FROM builder-lean AS builder-ml
RUN uv pip install --python /app/.venv/bin/python "torch>=2.12" --index-url https://download.pytorch.org/whl/cpu \
 && uv pip install --python /app/.venv/bin/python ".[embeddings]"

########################################################################
# builder-cuda — adds the `embeddings` extra with CUDA-capable torch.
#
# This is intentionally separate from `ml`: the CPU image stays portable and
# small, while this image carries the NVIDIA/CUDA torch wheel for Linux hosts
# that run Docker with the NVIDIA container runtime. CUDA capability still does
# NOT imply CUDA residency: exomem's default `normal` mode selects CPU unless the
# user opts into performance mode or an explicit CUDA device.
########################################################################
FROM builder-lean AS builder-cuda
RUN uv pip install --python /app/.venv/bin/python "torch>=2.12" --index-url https://download.pytorch.org/whl/cu132 \
 && uv pip install --python /app/.venv/bin/python ".[embeddings]"

########################################################################
# builder-hosted — builder-lean plus ONNX Runtime and the embedding weights
# resolved at BUILD time into the image itself.
#
# A hosted cell runs under a default-deny NetworkPolicy with no egress rules
# and a read-only root filesystem. It therefore cannot download a model, and
# has nowhere writable to cache one if it could. Any weight the cell needs
# must already be a layer, or the `embeddings` grant produces a cell that
# advertises semantic recall and fails on first use.
#
# This stage builds from `builder-lean`, NOT `builder-ml`: the hosted lane
# serves the same bi-encoder through ONNX Runtime, which imports ~40 MiB
# against torch's ~400 and holds a materially smaller peak. Peak per cell is
# what decides how many tenants a node carries, so torch's absence here is the
# capacity argument, not a packaging preference. It is only possible in this
# lane — the reranker and CLIP are sentence-transformers models, and the hosted
# stage withholds both (EXOMEM_DISABLE_RANKING; CLIP belongs to `vision`).
#
# Only the bi-encoder is fetched, and only in the form the runtime reads.
########################################################################
FROM builder-lean AS builder-hosted
# A cell encodes recall with the English model until a node encoder serves the
# multilingual one; name it here, where no cell flag is set, so the build
# fetches the model its cells load.
ENV HF_HOME=/opt/exomem-models \
    EXOMEM_RECALL_MODEL=BAAI/bge-base-en-v1.5
# Resolve the model through the very backend the cell serves with, so the build
# fetches exactly what that runtime opens — the ONNX export, the fast tokenizer,
# and the sequence config — and never a torch serialization there is no loader
# for. Then load once more with the network closed. That second load is the
# gate: if anything the runtime needs is absent, the build fails here instead of
# every cell failing on its first query.
RUN uv pip install --python /app/.venv/bin/python ".[embeddings-onnx]" \
 && EXOMEM_EMBED_BACKEND=onnx /app/.venv/bin/python -c "\
from exomem.embeddings import MODEL_NAME; \
from exomem import embedding_backend as backend; \
backend.load_encoder(MODEL_NAME, backend=backend.ONNX); \
print('fetched', MODEL_NAME)" \
 && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 EXOMEM_EMBED_BACKEND=onnx \
    /app/.venv/bin/python -c "\
import importlib.util; \
from exomem.embeddings import MODEL_NAME, VECTOR_DIM; \
from exomem import embedding_backend as backend; \
encoder = backend.load_encoder(MODEL_NAME, backend=backend.ONNX); \
v = encoder.encode(['offline load gate'], batch_size=1); \
assert v.shape == (1, VECTOR_DIM), v.shape; \
assert importlib.util.find_spec('torch') is None, 'torch reached the hosted image'; \
print('offline load verified', MODEL_NAME, v.shape)"

########################################################################
# restic-fetch — the static restic binary for the cloud image's backup and
# restore Jobs (design D8). Fetched and verified in its own small stage, not
# the final `cloud` image, so no curl/bzip2/apt residue ships in the runtime
# layer that the image-pin admission policy trusts.
########################################################################
FROM debian:bookworm-slim AS restic-fetch
ARG RESTIC_VERSION=0.19.1
ARG RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/restic.bz2 \
      "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2" \
 && echo "${RESTIC_SHA256}  /tmp/restic.bz2" | sha256sum -c - \
 && bunzip2 /tmp/restic.bz2 \
 && chmod 755 /tmp/restic \
 && mv /tmp/restic /usr/local/bin/restic

########################################################################
# Final: ml (target `ml`). Fresh slim base — no build tooling, no uv, no
# source tree — just the populated venv. HF_HOME pins the downloaded
# embedding/CLIP model weights under the declared /data volume so they
# persist across restarts instead of re-downloading into an ephemeral layer.
########################################################################
FROM python:3.12-slim AS ml
COPY --from=builder-ml /app/.venv /app/.venv
COPY LICENSE /LICENSE

ENV PATH=/app/.venv/bin:$PATH \
    EXOMEM_HOST=0.0.0.0 \
    EXOMEM_LOG_DIR=/data/logs \
    HF_HOME=/data/hf \
    EXOMEM_CONTAINER_VARIANT=ml

LABEL org.opencontainers.image.source="https://github.com/Artexis10/exomem" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="exomem — local knowledge substrate for owned markdown/Obsidian vaults, exposed through MCP, REST, and CLI (ml: hybrid embeddings + CLIP search, CPU-only torch, no GPU)"

# No HEALTHCHECK here by design (design.md D5) — a Dockerfile-level HEALTHCHECK
# would run unconditionally, including for `--transport stdio`, where there is
# no HTTP server to probe. The healthcheck lives in compose.yaml instead, where
# it only applies to the long-running HTTP deployment.
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["exomem"]
CMD ["--transport", "http", "--port", "8765"]

########################################################################
# Final: cuda (target `cuda`). Fresh slim base — no build tooling, no uv, no
# source tree — just the populated venv. The image is CUDA-capable but remains
# CPU-default at idle via exomem's mode resolver. NVIDIA_* env vars are hints
# consumed by the NVIDIA container runtime; without that runtime, doctor reports
# CUDA unavailable and the service degrades to CPU.
########################################################################
FROM python:3.12-slim AS cuda
COPY --from=builder-cuda /app/.venv /app/.venv
COPY LICENSE /LICENSE

ENV PATH=/app/.venv/bin:$PATH \
    EXOMEM_HOST=0.0.0.0 \
    EXOMEM_LOG_DIR=/data/logs \
    HF_HOME=/data/hf \
    EXOMEM_CONTAINER_VARIANT=cuda \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

LABEL org.opencontainers.image.source="https://github.com/Artexis10/exomem" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="exomem — local knowledge substrate for owned markdown/Obsidian vaults, exposed through MCP, REST, and CLI (cuda: hybrid embeddings + CLIP search, CUDA-capable torch, CPU-default at idle)"

# No HEALTHCHECK here by design — a Dockerfile-level HEALTHCHECK would run
# unconditionally, including for `--transport stdio`, where there is no HTTP
# server to probe. The healthcheck lives in compose.yaml instead.
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["exomem"]
CMD ["--transport", "http", "--port", "8765"]

########################################################################
# Final: hosted (target `hosted`). The platform mounts only the bound
# vault/state/log roots plus native Secret/operator projections. Kubernetes
# supplies readOnlyRootFilesystem; this image supplies the matching fixed
# runtime identity and avoids bytecode writes to the image layer.
#
# This carries the embedding runtime and pre-baked weights, not the lean venv:
# a paying tenant gets the same semantic recall as the free local runtime.
# The cell has no egress and no writable root, so the weights are a read-only
# image layer under HF_HOME and the hub client is pinned offline — an
# accidental fetch must fail loudly at load rather than hang a tenant query
# against a blocked NetworkPolicy until the client's deadline.
########################################################################
FROM python:3.12-slim AS hosted
COPY --from=builder-hosted /app/.venv /app/.venv
COPY --from=builder-hosted /opt/exomem-models /opt/exomem-models
COPY LICENSE /LICENSE

ARG EXOMEM_RELEASE_BUILD_TIME
RUN test -n "$EXOMEM_RELEASE_BUILD_TIME" \
 && groupadd --gid 10001 exomem \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin exomem

ENV PATH=/app/.venv/bin:$PATH \
    EXOMEM_HOST=0.0.0.0 \
    EXOMEM_CONTAINER_VARIANT=hosted \
    EXOMEM_RELEASE_BUILD_TIME=${EXOMEM_RELEASE_BUILD_TIME} \
    HF_HOME=/opt/exomem-models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EXOMEM_DISABLE_RANKING=1 \
    EXOMEM_EMBED_BACKEND=onnx \
    EXOMEM_RECALL_MODEL=BAAI/bge-base-en-v1.5 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.source="https://github.com/Artexis10/exomem" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="exomem — isolated hosted cell runtime (fixed UID/GID, operator CLI, read-only-root compatible)"

USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["exomem"]
CMD ["--transport", "http", "--port", "8765"]

########################################################################
# Final: cloud (target `cloud`). Exomem Cloud cell runtime (design D1/D2/D8).
#
# Derived from `hosted`: the same offline ONNX model environment,
# `EXOMEM_DISABLE_RANKING`, pre-baked embedding weights and read-only-root
# compatibility carry over unchanged. Cloud mode (`EXOMEM_CLOUD_CELL=1`) is a
# thin seam over the standalone runtime (D1), so this stage exists to add the
# identity, backup tooling and command the cell pod needs — not a different
# Python environment.
#
# UID/GID 10001 keeps `hosted`'s numeric identity but gets home `/data/host`
# instead of `/nonexistent`: standalone custody resolves under
# `<home>/.local/state/exomem/standalone-host-control-v1` with no code
# override (`authorization_custody._standalone_host_control_root`, D2), so the
# passwd home directory IS the custody root. The pod's `fsGroup: 10001` plus
# this image's explicit UID/GID keep ownership consistent across first start,
# pod replacement and restore.
#
# restic ships baked into the image (not fetched at runtime) because a cloud
# cell's NetworkPolicy grants no runtime egress at all, and every Job that
# touches the volume — init, backup, restore — runs this one digest-pinned
# image under the ValidatingAdmissionPolicy (D4, D8).
########################################################################
FROM hosted AS cloud
COPY --from=restic-fetch /usr/local/bin/restic /usr/local/bin/restic

USER root
RUN usermod --home /data/host exomem

# EXOMEM_LOG_DIR: no runtime log ever lands on the tenant volume D8 backs up,
# even without the manifest setting it (design D1.2 "Log directory"). D2's
# writable `/tmp` emptyDir is where this actually lands at runtime.
#
# FASTMCP_CHECK_FOR_UPDATES / FASTMCP_SHOW_SERVER_BANNER: a cell's
# NetworkPolicy grants no egress at all, not even DNS (D5), so FastMCP's
# startup update check otherwise stalls every cold start on DNS (measured:
# `/health` up after 24s against 4s with it off). Both names are read by the
# installed `fastmcp.settings.Settings` (`env_prefix="FASTMCP_"`).
ENV EXOMEM_CONTAINER_VARIANT=cloud \
    EXOMEM_LOG_DIR=/tmp/exomem-logs \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false

LABEL org.opencontainers.image.source="https://github.com/Artexis10/exomem" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="exomem — Exomem Cloud cell runtime (fixed UID/GID 10001, home /data/host, restic-equipped, standalone trust model)"

# No HEALTHCHECK here by design (design.md D5) — see the `ml` stage's comment
# above; cellctl reads readiness from the pod's own probes, not from Docker.
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["exomem"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8765"]

########################################################################
# Final: lean (target `lean`) — the DEFAULT stage (last in this file; see
# the top-of-file note). Base dependencies only: no torch, no
# sentence-transformers, no model download. EXOMEM_DISABLE_EMBEDDINGS=1
# keeps search in keyword/BM25 mode. Opt into `ml` explicitly for hybrid
# search (`--target ml` at build time, or the published `:ml` tag).
########################################################################
FROM python:3.12-slim AS lean
COPY --from=builder-lean /app/.venv /app/.venv
COPY LICENSE /LICENSE

ENV PATH=/app/.venv/bin:$PATH \
    EXOMEM_HOST=0.0.0.0 \
    EXOMEM_LOG_DIR=/data/logs \
    EXOMEM_DISABLE_EMBEDDINGS=1 \
    EXOMEM_CONTAINER_VARIANT=lean

LABEL org.opencontainers.image.source="https://github.com/Artexis10/exomem" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="exomem — local knowledge substrate for owned markdown/Obsidian vaults, exposed through MCP, REST, and CLI (lean: keyword/BM25 search, no torch, no model download)"

# No HEALTHCHECK here by design (design.md D5) — see the `ml` stage's comment
# above; the healthcheck lives in compose.yaml.
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["exomem"]
CMD ["--transport", "http", "--port", "8765"]
