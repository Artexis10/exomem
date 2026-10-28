"""The frozen release arm of a paired latency gate.

Resolve the latest release, extract its source, and install its locked
runtime in one owned environment, so a gate can measure that release beside
the working tree. The recall and MCP read latency gates share these helpers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class MeasurementRefused(ValueError):
    """A closed failure code; the summary schema owns phase and policy values."""

    def __init__(self, code: str, *, phase: str | None = None):
        super().__init__(code)
        self.code, self.phase = code, phase


def latest_release_tag(root: Path) -> str:
    """Name the most recent release of the repository at `root`."""
    import subprocess

    tag = subprocess.check_output(
        ["gh", "release", "view", "--json", "tagName", "--jq", ".tagName"], text=True,
        cwd=root, stderr=subprocess.DEVNULL,
    ).strip()
    if not tag or tag.startswith("-"):
        raise MeasurementRefused("latest-release-tag-unavailable")
    return tag


def extract_release(root: Path, tag: str, scratch: Path) -> Path:
    """Extract the tagged tree of the repository at `root` into `scratch/base`."""
    import subprocess

    base = scratch / "base"
    base.mkdir()
    archive = scratch / "release.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", tag], cwd=root, stdout=stream, check=True)
    # Git owns this archive; the checkout contains no external tar input.
    subprocess.run(["tar", "-xf", str(archive), "-C", str(base)], check=True)
    return base


def runtime_lock_digest(source: Path, *, phase: str | None = None) -> str:
    import hashlib

    try:
        return hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest()
    except OSError as error:
        raise MeasurementRefused("runtime-lock-unavailable", phase=phase) from error


def runtime_provenance(source: Path, startup: dict, *, prefix: str | None = None) -> dict:
    """Check the private interpreter prefix before accepting public provenance."""
    if Path(sys.prefix if prefix is None else prefix).resolve() != Path(startup["runtime_prefix"]).resolve():
        raise MeasurementRefused("runtime-prefix-mismatch", phase="worker-startup")
    digest = runtime_lock_digest(source, phase="worker-startup")
    if digest != startup["runtime_lock_digest"]:
        raise MeasurementRefused("runtime-lock-changed", phase="worker-startup")
    return {"runtime_lock_digest": digest, "runtime_policy": startup["runtime_policy"]}


def prepare_tag_runtime(base: Path, scratch: Path) -> Path:
    """Read the immutable tag lock into one owned environment, outside timers."""
    import subprocess

    phase = "tag-runtime-preparation"
    try:
        digest = runtime_lock_digest(base)
        runtime = (scratch / "tag-runtime").absolute()
        # UV_ is uv's operation namespace; this fixed recipe retains only its cache location.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
        if "UV_CACHE_DIR" in os.environ:
            environment["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
        environment.pop("VIRTUAL_ENV", None)
        environment["UV_PROJECT_ENVIRONMENT"] = str(runtime)
        # Frozen reads use the lock's sources; historical writer settings do not apply.
        result = subprocess.run(
            ["uv", "--no-config", "sync", "--project", str(base.absolute()),
             "--frozen", "--no-default-groups", "--extra", "embeddings-onnx",
             "--no-python-downloads", "--python", sys.executable], cwd=base, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if runtime_lock_digest(base) != digest:
            raise MeasurementRefused("tag-runtime-lock-changed", phase=phase)
        if result.returncode:
            raise MeasurementRefused("tag-runtime-install-failed", phase=phase)
        python = runtime / "bin" / "python"
        if not python.is_file():
            raise MeasurementRefused("tag-runtime-python-unavailable", phase=phase)
        return python
    except MeasurementRefused as error:
        raise MeasurementRefused(error.code, phase=phase) from error
    except OSError as error:
        raise MeasurementRefused("tag-runtime-install-failed", phase=phase) from error
