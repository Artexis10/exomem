# exomem — multi-stage container image.
#
# Three final targets share this one file:
#   lean (target `lean`, DEFAULT — base deps only, no torch, no model download)
#   ml   (target `ml`   — the `embeddings` extra, CPU-only torch, no CUDA runtime)
#   cuda (target `cuda` — the `embeddings` extra, CUDA-capable torch, CPU-default at idle)
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

ARG UV_VERSION=0.9.7

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
