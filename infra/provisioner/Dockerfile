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

FROM docker.io/library/python:3.13-slim-bookworm@sha256:de89ed8283721548f7f9c8acfd19d638f435fa3870693a017890e94be5d37fc6 AS python-runtime
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels exomem-provisioner \
    && rm -rf /wheels

FROM docker.io/library/postgres:17-bookworm@sha256:67870dc097790edf2bd6726658db995dcc830f799d41bb2b78ef07c9a2d5f010
COPY --from=python-runtime /usr/local /usr/local
COPY --from=helm /helm /opt/exomem/bin/helm
COPY infra/helm/cell /opt/exomem/charts/cell
COPY infra/provisioner/alembic.ini /opt/exomem/provisioner-migrations/alembic.ini
COPY infra/provisioner/alembic /opt/exomem/provisioner-migrations/alembic
RUN case "$(pg_dump --version)" in "pg_dump (PostgreSQL) 17."*) ;; *) exit 1;; esac \
    && for tool in pg_dump pg_restore psql dropdb createdb; do test -x "/usr/bin/${tool}"; done \
    && exomem-provisioner-api --help >/dev/null \
    && exomem-provisioner-worker --help >/dev/null \
    && exomem-volume-worker --help >/dev/null \
    && exomem-provisioner-volume-rebind --help >/dev/null \
    && exomem-durability-actions --help >/dev/null \
    && exomem-restore-fetch --help >/dev/null \
    && for command in exomem-provisioner-database-bootstrap \
        exomem-provisioner-database-migrate exomem-provisioner-database-validate \
        exomem-durability-backup-worker exomem-database-backup-worker \
        exomem-deletion-worker exomem-export-gc; do command -v "${command}" >/dev/null; done \
    && chown -R 0:0 /opt/exomem/provisioner-migrations \
    && find /opt/exomem/provisioner-migrations -type d -exec chmod 0555 {} + \
    && find /opt/exomem/provisioner-migrations -type f -exec chmod 0444 {} + \
    && chmod -R a-w /opt/exomem/provisioner-migrations \
    && groupadd --gid 10001 exomem-provisioner \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent exomem-provisioner
USER 10001:10001
WORKDIR /nonexistent

LABEL org.opencontainers.image.title="Exomem hosted provisioner"
LABEL org.opencontainers.image.source="https://github.com/artexis10/exomem"

ENTRYPOINT []
CMD ["exomem-provisioner-api"]
