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
  install of `infra/cellctl`, plus `cellctl_entry.py`.

Nothing here runs a network step inside `docker build` except the two
upstream Dockerfiles themselves.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import images
from .shell import run

CELL_REPOSITORY = "rehearsal.local/exomem-cell"
# The platform chart schema admits only this gateway repository. The image
# is built locally and loaded into K3s; nothing is pushed or pulled.
GATEWAY_REPOSITORY = "ghcr.io/substrate-systems/substrate-gateway"
CELLCTL_REPOSITORY = "rehearsal.local/exomem-cellctl"
PYTHON_IMAGE = "python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
CELLCTL_ROOT = images.REPO_ROOT / "infra/cellctl"
ENTRY_MODULE = Path(__file__).with_name("cellctl_entry.py")

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
        _derive(workdir / "cell-v2", v1, v2, "LABEL io.exomem.rehearsal.release=v2\n")
    else:
        for tag in (v1, v2):
            run(
                [
                    "docker", "build", "--target", "cloud", "--tag", tag,
                    "--build-arg", f"EXOMEM_RELEASE_BUILD_TIME={_now_iso()}",
                    str(images.REPO_ROOT),
                ],
                timeout=3600,
            )
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
    )
    return v1, v2, broken


def _derive(context: Path, base: str, tag: str, body: str) -> None:
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(f"FROM {base}\n{body}", encoding="utf-8")
    run(["docker", "build", "--tag", tag, str(context)], timeout=900)


def build_gateway_image(run_id: str, workdir: Path, substrate_source: Path, *, mode: str) -> str:
    tag = f"{GATEWAY_REPOSITORY}:{run_id}"
    if mode == "dockerfile":
        run(
            ["docker", "build", "--file", "Dockerfile.exomem-gateway", "--tag", tag, "."],
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
    run(["docker", "build", "--tag", tag, str(context)], timeout=900)
    return tag


def _node_image_from(substrate_source: Path) -> str:
    """The final stage's base, read from the upstream Dockerfile itself."""

    lines = (substrate_source / "Dockerfile.exomem-gateway").read_text(encoding="utf-8").splitlines()
    finals = [line.split()[1] for line in lines if line.startswith("FROM ")]
    return finals[-1]


def build_cellctl_image(run_id: str, workdir: Path) -> str:
    tag = f"{CELLCTL_REPOSITORY}:{run_id}"
    context = workdir / "cellctl-image"
    if context.exists():
        shutil.rmtree(context)
    site = context / "site"
    site.mkdir(parents=True)
    run(
        [
            "uv", "pip", "install", "--quiet", "--target", str(site),
            "--python-version", "3.12", "--python-platform", "x86_64-manylinux_2_28",
            "--no-cache", str(CELLCTL_ROOT), "boto3>=1.35,<2",
        ],
        timeout=900,
    )
    shutil.copy(ENTRY_MODULE, site / "rehearsal_cellctl.py")
    (context / "Dockerfile").write_text(
        f"FROM {PYTHON_IMAGE}\n"
        "COPY site /opt/cellctl\n"
        "ENV PYTHONPATH=/opt/cellctl PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
        "USER 1000:1000\n"
        'CMD ["python3", "-m", "rehearsal_cellctl"]\n',
        encoding="utf-8",
    )
    run(["docker", "build", "--tag", tag, str(context)], timeout=900)
    return tag


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
