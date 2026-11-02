"""Building the rehearsal's own images and loading them into K3s by digest.

- **Cell images** come from this repository's `Dockerfile`, target `cloud`:
  the release under test (`v1`) and an upgrade (`v2`, the same source with a
  later `EXOMEM_RELEASE_BUILD_TIME`, hence a new digest). The forced canary
  failure (`broken`) derives from `v1` without any network step: its
  `exomem` executable still runs `cell-init` for real, then never serves,
  so it never passes `/health/ready`.
- **Gateway image** comes from Substrate's `Dockerfile.exomem-gateway` at
  the pinned commit. Where `docker build` cannot reach the npm registry
  with a trusted CA, `--gateway-build host` assembles the same final stage
  (same pinned Node image, user and command) from the bundle and production
  dependencies built on the host. The report records which one ran.
- **cellctl image**: the repository ships no cellctl image yet, so the
  rehearsal assembles one from the pinned Python base and a host-resolved
  install of `infra/cellctl`, plus `cellctl_entry.py` and `custodian_entry.py`.

Nothing here runs a network step inside `docker build` except the two
upstream Dockerfiles themselves.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import images
from .infra import docker_labels
from .shell import run

CELL_REPOSITORY = "rehearsal.local/exomem-cell"
# The platform chart schema admits only this gateway repository. The image
# is built locally and loaded into K3s; nothing is pushed or pulled.
GATEWAY_REPOSITORY = "ghcr.io/substrate-systems/substrate-gateway"
CELLCTL_REPOSITORY = "rehearsal.local/exomem-cellctl"
PYTHON_IMAGE = "python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
CELLCTL_ROOT = images.REPO_ROOT / "infra/cellctl"
ENTRY_MODULE = Path(__file__).with_name("cellctl_entry.py")
CUSTODIAN_ENTRY_MODULE = Path(__file__).with_name("custodian_entry.py")

BROKEN_SHIM = """#!/bin/sh
# Forced canary failure: cell-init runs for real, the server never starts.
if [ "$1" = "cell-init" ]; then exec "$0-real" "$@"; fi
exec sleep 2147483647
"""


@dataclass(frozen=True)
class BuiltImages:
    cell_v1: str
    cell_v2: str
    cell_broken: str
    gateway: str
    cellctl: str
    gateway_build: str
    build_seconds: dict[str, float]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_cell_images(run_id: str, workdir: Path, *, prebuilt: str | None) -> tuple[str, str, str]:
    v1 = f"{CELL_REPOSITORY}:{run_id}-v1"
    v2 = f"{CELL_REPOSITORY}:{run_id}-v2"
    broken = f"{CELL_REPOSITORY}:{run_id}-broken"
    if prebuilt:
        run(["docker", "tag", prebuilt, v1])
        # A prebuilt image still upgrades to a new digest built from it.
        _derive(workdir / "cell-v2", v1, v2, "LABEL io.exomem.rehearsal.release=v2\n", labels=docker_labels(run_id, image=True))
    else:
        for tag in (v1, v2):
            build_cell_image(tag, labels=docker_labels(run_id, image=True))
    broken_dir = workdir / "cell-broken"
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "exomem-shim").write_text(BROKEN_SHIM, encoding="utf-8")
    _derive(
        broken_dir,
        v1,
        broken,
        "USER root\n"
        "COPY --chmod=0755 exomem-shim /usr/local/lib/exomem-rehearsal-shim\n"
        'RUN real="$(command -v exomem)" && mv "$real" "$real-real" '
        '&& ln -s /usr/local/lib/exomem-rehearsal-shim "$real"\n'
        "USER 10001:10001\n",
        labels=docker_labels(run_id, image=True),
    )
    return v1, v2, broken


def build_cell_image(tag: str, *, labels: list[str] | None = None) -> None:
    """This checkout's `Dockerfile --target cloud`, stamped with a build time."""

    run(
        [
            "docker", "build", *(labels or []), "--target", "cloud", "--tag", tag,
            "--build-arg", f"EXOMEM_RELEASE_BUILD_TIME={_now_iso()}",
            str(images.REPO_ROOT),
        ],
        timeout=3600,
    )


def _derive(context: Path, base: str, tag: str, body: str, *, labels: list[str] | None = None) -> None:
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(f"FROM {base}\n{body}", encoding="utf-8")
    run(["docker", "build", *(labels or []), "--tag", tag, str(context)], timeout=900)


def build_reservation_provider_image(run_id: str, workdir: Path, provider: Path) -> str:
    """Reuse the pinned sidecars while replacing the source-built provider binary."""
    provenance = json.loads((provider / "provenance.json").read_text())
    expected_commit = run(["git", "rev-parse", "HEAD"], cwd=images.REPO_ROOT).stdout.strip()
    # A stale build would qualify another tree; only this disposable drill pays the refusal.
    if provenance["exomem_source_sha"] != expected_commit or not provenance["exomem_source_clean"]:
        raise RuntimeError("the reservation provider was not built from this clean source revision")
    for relative in ("build/hypertopolvm", "build/reservation-host-proof",
                     "config/crd/bases/topolvm.io_logicalvolumes.yaml"):
        if hashlib.sha256((provider / relative).read_bytes()).hexdigest() != provenance["outputs"][relative]:
            raise RuntimeError("a reservation provider output differs from its build receipt")
    generated_crd = images.REPO_ROOT / "infra/helm/platform/files/topolvm/logicalvolumes.yaml"
    if generated_crd.read_bytes() != (provider / "config/crd/bases/topolvm.io_logicalvolumes.yaml").read_bytes():
        raise RuntimeError("the installed volume schema differs from the source-built provider")
    settings = yaml.safe_load((images.REPO_ROOT / "infra/helm/platform/values.yaml").read_text())["topolvm"]["image"]
    base = f"{settings['repository']}:{settings['reference']}"
    context = workdir / "reservation-provider-image"
    context.mkdir()
    shutil.copy2(provider / "build/hypertopolvm", context / "hypertopolvm")
    tag = f"rehearsal.local/topolvm-reservations:{run_id}"
    _derive(context, base, tag, "COPY --chmod=0755 hypertopolvm /hypertopolvm\n",
            labels=docker_labels(run_id, image=True))
    return tag


def trust_rehearsal_ca(tag: str, ca_path: Path, workdir: Path, *, run_id: str | None = None) -> str:
    """Add only the disposable trust root to a source-built Cloud image."""
    trusted = tag + "-trusted"
    context = workdir / trusted.rsplit(":", 1)[1]
    context.mkdir()
    shutil.copy(ca_path, context / "ca.crt")
    _derive(context, tag, trusted,
            "USER 0:0\nCOPY ca.crt /tmp/rehearsal-ca.crt\n"
            "RUN cat /tmp/rehearsal-ca.crt >> /etc/ssl/certs/ca-certificates.crt && rm /tmp/rehearsal-ca.crt\n"
            "USER 10001:10001\n", labels=docker_labels(run_id, image=True) if run_id else None)
    return trusted


def build_gateway_image(run_id: str, workdir: Path, substrate_source: Path, *, mode: str) -> str:
    tag = f"{GATEWAY_REPOSITORY}:{run_id}"
    if mode == "dockerfile":
        run(
            ["docker", "build", *docker_labels(run_id, image=True), "--file", "Dockerfile.exomem-gateway", "--tag", tag, "."],
            cwd=substrate_source,
            timeout=3600,
        )
        return tag
    context = workdir / "gateway-host"
    if context.exists():
        shutil.rmtree(context)
    context.mkdir(parents=True)
    shutil.copy(substrate_source / "package.json", context / "package.json")
    shutil.copy(substrate_source / "package-lock.json", context / "package-lock.json")
    # The upstream production-dependencies stage, run on the host.
    run(["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=context, timeout=1800)
    shutil.rmtree(context / "node_modules" / "next", ignore_errors=True)
    (context / "gateway").mkdir()
    shutil.copy(substrate_source / "dist/exomem-gateway/index.cjs", context / "gateway/index.cjs")
    (context / "Dockerfile").write_text(
        f"FROM {_node_image_from(substrate_source)}\n"
        "WORKDIR /app\n"
        "COPY --chown=1000:1000 node_modules ./node_modules\n"
        "COPY --chown=1000:1000 gateway/index.cjs ./gateway/index.cjs\n"
        "EXPOSE 8080\n"
        "ENV EXOMEM_GATEWAY_PORT=8080\n"
        "USER 1000:1000\n"
        'CMD ["node", "gateway/index.cjs"]\n',
        encoding="utf-8",
    )
    (context / ".dockerignore").write_text("package.json\npackage-lock.json\n", encoding="utf-8")
    run(["docker", "build", *docker_labels(run_id, image=True), "--tag", tag, str(context)], timeout=900)
    return tag


def _node_image_from(substrate_source: Path) -> str:
    """The final stage's base, read from the upstream Dockerfile itself."""

    lines = (substrate_source / "Dockerfile.exomem-gateway").read_text(encoding="utf-8").splitlines()
    finals = [line.split()[1] for line in lines if line.startswith("FROM ")]
    return finals[-1]


def build_cellctl_image(run_id: str, workdir: Path, *, mode: str) -> str:
    """cellctl's own image plus one layer: the rehearsal entry module and boto3.

    `dockerfile` builds infra/cellctl/Dockerfile unchanged and adds the layer
    with no network step: boto3 is resolved on the host for the image's
    Python and copied into its venv. `host` assembles the same venv layout on
    the pinned Python base from a host resolution, for machines whose `docker
    build` cannot reach PyPI with a trusted CA; the report records which ran.
    """

    tag = f"{CELLCTL_REPOSITORY}:{run_id}"
    context = workdir / "cellctl-image"
    if context.exists():
        shutil.rmtree(context)
    extra = context / "extra"
    extra.mkdir(parents=True)
    packages = ["boto3>=1.35,<2"] if mode == "dockerfile" else [str(CELLCTL_ROOT), "boto3>=1.35,<2"]
    run(
        [
            "uv", "pip", "install", "--quiet", "--target", str(extra),
            "--python-version", "3.12", "--python-platform", "x86_64-manylinux_2_28",
            "--no-cache", *packages,
        ],
        timeout=900,
    )
    shutil.copy(ENTRY_MODULE, extra / "rehearsal_cellctl.py")
    shutil.copy(CUSTODIAN_ENTRY_MODULE, extra / "rehearsal_custodian.py")
    site = "/opt/cellctl/lib/python3.12/site-packages"
    if mode == "dockerfile":
        base = f"{CELLCTL_REPOSITORY}:{run_id}-base"
        run(["docker", "build", *docker_labels(run_id, image=True), "--tag", base, str(CELLCTL_ROOT)], timeout=1800)
        # Only packages the base venv lacks, so nothing cellctl locks is replaced.
        present = set(
            run(["docker", "run", "--rm", *docker_labels(run_id), "--entrypoint", "ls", base, site]).stdout.split()
        )
        # Metadata goes by distribution name too, so a resolve that picks a
        # different version of something the base has leaves no second
        # dist-info claiming it.
        base_dists = {_dist_name(name) for name in present if name.endswith(".dist-info")}
        for entry in list(extra.iterdir()):
            if entry.name in present or (entry.name.endswith(".dist-info") and _dist_name(entry.name) in base_dists):
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        dockerfile = f"FROM {base}\nCOPY extra {site}\n"
    else:
        dockerfile = (
            f"FROM {PYTHON_IMAGE}\n"
            f"COPY extra {site}\n"
            f"ENV PATH=/usr/local/bin:$PATH PYTHONPATH={site} PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            "USER 1000:1000\n"
        )
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    run(["docker", "build", *docker_labels(run_id, image=True), "--tag", tag, str(context)], timeout=900)
    return tag


def _dist_name(dist_info: str) -> str:
    """`Foo_Bar-1.2.dist-info` -> `foo-bar` (PEP 503 normalization)."""
    return re.sub(r"[-_.]+", "-", dist_info.split("-", 1)[0]).lower()


def load_into_k3s(k3s_container: str, tag: str) -> str:
    """Imports a host image into K3s and returns `<repository>@sha256:<digest>`.

    Digest references are what the cellctl admission policy admits and what
    the design pins everywhere; the digest is the one containerd reports for
    the imported manifest.
    """

    repository = tag.rsplit(":", 1)[0]
    run(
        ["bash", "-c", "set -o pipefail; docker save \"$1\" | docker exec --interactive \"$2\" ctr --namespace k8s.io images import -", "_", tag, k3s_container],
        timeout=1800,
    )
    listing = run(["docker", "exec", k3s_container, "ctr", "--namespace", "k8s.io", "images", "ls"]).stdout
    digest = None
    source_ref = None
    for line in listing.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0].endswith(tag):
            source_ref, digest = fields[0], fields[2]
            break
    if digest is None or source_ref is None:
        raise RuntimeError(f"{tag} did not appear in K3s after import")
    reference = f"{repository}@{digest}"
    run(["docker", "exec", k3s_container, "ctr", "--namespace", "k8s.io", "images", "tag", "--force", source_ref, reference])
    return reference
