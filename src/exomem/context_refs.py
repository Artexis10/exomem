"""Stable context references for adoption/review surfaces.

These refs are labels for agents and manifests, not a new path-resolution API.
Existing vault-relative paths remain the authoritative tool inputs.
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote


SCHEME = "exomem"


def _clean_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _encode(path: str) -> str:
    return quote(_clean_path(path), safe="/-._~")


def vault_ref(path: str) -> str:
    """Reference a vault-relative file or folder."""
    return f"{SCHEME}://vault/{_encode(path)}"


def source_ref(path: str) -> str:
    """Reference a governed source path, with a stable no-extension form."""
    clean = _clean_path(path)
    if clean.lower().endswith(".md"):
        clean = clean[:-3]
    return f"{SCHEME}://source/{_encode(clean)}"


def manifest_ref(path: str) -> str:
    """Reference an adoption manifest path."""
    return f"{SCHEME}://manifest/{_encode(path)}"


def proposal_ref(sources: list[str]) -> str:
    """Reference an in-response compile proposal derived from source paths."""
    normalized = "\n".join(_clean_path(s) for s in sources)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{SCHEME}://proposal/{digest}"
