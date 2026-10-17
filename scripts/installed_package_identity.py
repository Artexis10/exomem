"""Content-free package provenance shared by installed acceptance tools."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path


def package_identity(package_mode: str, *, role: str) -> dict:
    import exomem

    origin = Path(exomem.__file__).resolve()
    distribution = importlib.metadata.distribution("exomem")
    installed_origin = Path(distribution.locate_file("exomem/__init__.py")).resolve()
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    installed = origin == installed_origin and not direct_url.get("dir_info", {}).get("editable")
    if package_mode == "installed" and not installed:
        raise ValueError("non-editable installed package required")
    revision = direct_url.get("vcs_info", {}).get("commit_id")
    if package_mode == "source":
        result = subprocess.run(
            ["git", "-C", str(origin.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        revision = result.stdout.strip() if result.returncode == 0 else None
    return {
        "mode": package_mode,
        "version": exomem.__version__,
        "distribution_version": distribution.version,
        "origin": "non-editable-distribution" if installed else "source-tree",
        "origin_sha256": hashlib.sha256(str(origin).encode()).hexdigest(),
        "source_revision": revision,
        "role": role,
    }
