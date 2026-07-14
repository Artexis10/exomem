FROM docker.io/library/python:3.13-slim-bookworm@sha256:de89ed8283721548f7f9c8acfd19d638f435fa3870693a017890e94be5d37fc6 AS helm

ARG HELM_VERSION=3.19.4
ARG HELM_SHA256=759c656fbd9c11e6a47784ecbeac6ad1eb16a9e76d202e51163ab78504848862
ENV HELM_VERSION=${HELM_VERSION} HELM_SHA256=${HELM_SHA256}
RUN python - <<'PY'
import hashlib
import io
import os
import tarfile
import urllib.request

version = os.environ.get("HELM_VERSION", "3.19.4")
expected = os.environ.get(
    "HELM_SHA256",
    "759c656fbd9c11e6a47784ecbeac6ad1eb16a9e76d202e51163ab78504848862",
)
with urllib.request.urlopen(
    f"https://get.helm.sh/helm-v{version}-linux-amd64.tar.gz", timeout=30
) as response:
    archive = response.read()
if hashlib.sha256(archive).hexdigest() != expected:
    raise SystemExit("Helm archive checksum mismatch")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
    member = tar.getmember("linux-amd64/helm")
    if not member.isfile() or member.size > 100 * 1024 * 1024:
        raise SystemExit("Helm archive member is invalid")
    source = tar.extractfile(member)
    if source is None:
        raise SystemExit("Helm archive member is absent")
    with open("/helm", "wb") as target:
        target.write(source.read())
os.chmod("/helm", 0o755)
PY

FROM docker.io/library/python:3.13-slim-bookworm@sha256:de89ed8283721548f7f9c8acfd19d638f435fa3870693a017890e94be5d37fc6 AS wheels
WORKDIR /build
COPY infra/provisioner/pyproject.toml infra/provisioner/uv.lock infra/provisioner/README.md ./
COPY infra/provisioner/src ./src
RUN python -m pip install --no-cache-dir uv==0.11.28 \
    && uv export --quiet --frozen --no-dev --no-emit-project --no-annotate --no-header \
        --output-file /tmp/requirements.txt \
    && python -m pip wheel --no-cache-dir --require-hashes \
        --requirement /tmp/requirements.txt --wheel-dir /wheels \
    && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM docker.io/library/python:3.13-slim-bookworm@sha256:de89ed8283721548f7f9c8acfd19d638f435fa3870693a017890e94be5d37fc6
COPY --from=helm /helm /opt/exomem/bin/helm
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels exomem-provisioner \
    && rm -rf /wheels \
    && groupadd --gid 10001 exomem-provisioner \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent exomem-provisioner
COPY infra/helm/cell /opt/exomem/charts/cell
USER 10001:10001
WORKDIR /nonexistent

LABEL org.opencontainers.image.title="Exomem hosted provisioner"
LABEL org.opencontainers.image.source="https://github.com/artexis10/exomem"

CMD ["exomem-provisioner-api"]
