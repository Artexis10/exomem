# cellctl, the Exomem Cloud cell controller (design D4).
#
# Built from infra/cellctl as the context. The runtime runs as UID 1000 with
# a read-only root filesystem (templates/cellctl.yaml): nothing is written at
# runtime except the heartbeat under the /tmp emptyDir, and bytecode is
# compiled at build time. The chart's exec probes call `python3`, which the
# venv on PATH provides.
#
# Both stages use the same digest-pinned base, so the venv's interpreter
# symlinks resolve in the runtime stage. Bump the digest deliberately.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

FROM ${PYTHON_IMAGE} AS build
# The pinned uv writer (infra/tool-versions.env), hash-checked from PyPI.
RUN printf '%s\n' 'uv==0.11.28 --hash=sha256:49fe42df9f42056037473f3876adec1615709b57d3470ed39178ff420f3afb9f' > /tmp/uv.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/uv.txt
WORKDIR /src
COPY pyproject.toml uv.lock ./
# uv.lock pins and hash-checks every dependency; --frozen never rewrites it.
# The project itself is not built (its build backend is not in the lock):
# its source is copied into the venv and compiled below.
RUN UV_PROJECT_ENVIRONMENT=/opt/cellctl UV_PYTHON=/usr/local/bin/python3 \
    uv sync --frozen --no-dev --no-install-project --compile-bytecode --no-cache
COPY src/cellctl /opt/cellctl/lib/python3.12/site-packages/cellctl
RUN /opt/cellctl/bin/python3 -m compileall -q /opt/cellctl/lib/python3.12/site-packages/cellctl

FROM ${PYTHON_IMAGE}
COPY --from=build /opt/cellctl /opt/cellctl
ENV PATH=/opt/cellctl/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["python3", "-m", "cellctl.main"]
