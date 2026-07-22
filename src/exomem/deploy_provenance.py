"""Report how the running server was installed.

The deploy question "what version is running, and from where" was previously
unanswerable from the server itself: `/health` reported a version and nothing
else, so identifying the actual deploy target meant inspecting service-manager
configuration. That gap let a service run from a standalone wheel-backed venv
while the nearby checkout looked authoritative.

Two constraints shape this module:

* **Never import torch.** The build tag comes from distribution metadata. The
  probe must stay fast and must not fail on a broken ML stack.
* **Never leak host layout to the public surface.** `/health` is unauthenticated
  and publicly reachable, so absolute paths are gated behind ``include_local``
  and surface only on the local CLI.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from typing import Any

# Import-name -> extra it belongs to. `find_spec` does not execute module code,
# so probing these stays cheap even for the heavy ML packages.
_EXTRA_PROBES: dict[str, tuple[str, ...]] = {
    "embeddings": ("sentence_transformers", "torch"),
    "media": ("faster_whisper", "fitz"),
    "vision": ("PIL",),
}

# Local version tags that mark an accelerator build. Default PyPI wheels carry
# no local tag, which on Windows means CPU-only.
_ACCEL_TAGS = ("+cu", "+rocm", "+xpu")


def _package_version() -> str:
    try:
        return version("exomem")
    except Exception:  # noqa: BLE001 — provenance must never raise
        return "unknown"


def _install_source_and_root() -> tuple[str, Path | None]:
    """Classify the install as an editable checkout or an installed wheel.

    pip/uv record `direct_url.json` for direct installs; `dir_info.editable`
    marks an editable one. Anything we cannot classify reports ``unknown``
    rather than guessing, since a wrong answer here is what caused the original
    confusion.
    """
    try:
        dist = distribution("exomem")
    except PackageNotFoundError:
        return "unknown", None
    except Exception:  # noqa: BLE001
        return "unknown", None

    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        raw = None

    if raw:
        try:
            info = json.loads(raw)
            if info.get("dir_info", {}).get("editable"):
                url = info.get("url", "")
                root = None
                if url.startswith("file://"):
                    # Windows file URLs carry a drive-rooted path — lstrip the
                    # leading slash so Path() gets a drive-rooted path.
                    candidate = url[len("file://") :]
                    if len(candidate) > 2 and candidate[0] == "/" and candidate[2] == ":":
                        candidate = candidate[1:]
                    root = Path(candidate)
                return "editable", root
        except Exception:  # noqa: BLE001
            pass

    # No direct_url, or a non-editable one: an installed wheel.
    return "wheel", None


def _revision(root: Path | None) -> str | None:
    """Short git revision, only meaningful for an editable checkout."""
    if root is None or not root.exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 — a missing git binary is not an error here
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _torch_build() -> str | None:
    """Installed torch build tag, read from metadata without importing torch.

    Returns the full local version (``2.12.0+cu132``) so an accelerated wheel is
    distinguishable from the default PyPI CPU wheel (``2.13.0``).
    """
    try:
        return version("torch")
    except Exception:  # noqa: BLE001 — torch is an optional extra
        return None


def _is_accelerated(build: str | None) -> bool:
    if not build:
        return False
    return any(tag in build for tag in _ACCEL_TAGS)


def _extras_present() -> list[str]:
    present: list[str] = []
    for extra, modules in _EXTRA_PROBES.items():
        try:
            if all(importlib.util.find_spec(m) is not None for m in modules):
                present.append(extra)
        except Exception:  # noqa: BLE001 — a broken finder must not fail the probe
            continue
    return present


def provenance(include_local: bool = False) -> dict[str, Any]:
    """Describe this installation.

    Args:
        include_local: include host-identifying detail (interpreter and package
            paths). Only ever true on the local CLI surface — never on the
            unauthenticated ``/health`` route.
    """
    source, root = _install_source_and_root()
    build = _torch_build()

    report: dict[str, Any] = {
        "version": _package_version(),
        "install_source": source,
        "revision": _revision(root) if source == "editable" else None,
        "torch": build,
        "accelerated": _is_accelerated(build),
        "extras": _extras_present(),
    }

    if include_local:
        report["interpreter"] = sys.executable
        report["package_path"] = str(Path(__file__).parent)
        report["checkout"] = str(root) if root else None

    return report
