"""Local voice-profile store — operational infra, NOT note content.

Persists enrolled speaker voiceprints in a single JSON file under the external
per-vault state root, beside the embedding sidecar.  It is NOT a queryable
markdown sidecar and is never indexed — it is server state, the same class as
the embedding sidecar.

Schema: `{ "<name>": {"centroid": [floats], "threshold": float, "samples": int,
"is_self": bool, "updated": iso8601} }`. Enrolling the same name again folds the new sample
into a running-average centroid.

Pure (numpy + stdlib). Soft: a missing or corrupt store reads as empty (never raises on read).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from exomem.speaker_attribution import Profile

from . import reserved_paths, state_paths

DEFAULT_THRESHOLD = 0.40
_STORE_READ_LIMIT = 16 * 1024 * 1024


def voice_profiles_path(vault_root: Path) -> Path:
    """Per-machine voice-profile store, beside the embedding sidecars (operational infra)."""
    return state_paths.vault_state_dir(vault_root) / ".voice_profiles.json"


def load_store(
    path: Path,
    *,
    vault_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Raw store dict. Missing or corrupt/unreadable file → empty dict (never raises)."""
    try:
        if vault_root is not None and path == voice_profiles_path(vault_root):
            with reserved_paths._subsystem_authority_scope("voice_profiles"):
                raw = reserved_paths._read_owner_bytes(
                    vault_root,
                    path,
                    "voice-profile-store",
                    limit=_STORE_READ_LIMIT,
                )
            text = raw.decode("utf-8")
        else:
            text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_profiles(
    path: Path,
    *,
    vault_root: Path | None = None,
) -> dict[str, Profile]:
    """Store → `{name: Profile}` for `attribute_clusters`. Skips malformed entries."""
    profiles: dict[str, Profile] = {}
    for name, rec in load_store(path, vault_root=vault_root).items():
        if not isinstance(rec, dict):
            continue
        centroid = rec.get("centroid")
        if not centroid:
            continue
        threshold = float(rec.get("threshold", DEFAULT_THRESHOLD))
        profiles[name] = Profile(name=name, centroid=np.asarray(centroid, dtype=float),
                                 threshold=threshold)
    return profiles


def _write_store(
    path: Path,
    store: dict[str, dict[str, Any]],
    *,
    vault_root: Path | None = None,
) -> None:
    encoded = json.dumps(store, indent=2, sort_keys=True).encode("utf-8")
    if vault_root is not None and path == voice_profiles_path(vault_root):
        state_paths.ensure_vault_state_dir(vault_root)
        with reserved_paths._subsystem_authority_scope("voice_profiles"):
            reserved_paths._publish_owner_bytes(
                vault_root,
                path,
                "voice-profile-store",
                encoded,
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)


def save_profile(
    path: Path,
    name: str,
    centroid: np.ndarray,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    is_self: bool = False,
    vault_root: Path | None = None,
) -> dict[str, Any]:
    """Enroll/extend a profile. A repeat name folds `centroid` into the running-average centroid
    and increments `samples`. Returns the stored record."""
    centroid = np.asarray(centroid, dtype=float).ravel()
    store = load_store(path, vault_root=vault_root)
    existing = store.get(name)
    if isinstance(existing, dict) and existing.get("centroid"):
        prev = np.asarray(existing["centroid"], dtype=float).ravel()
        n = int(existing.get("samples", 1))
        merged = (prev * n + centroid) / (n + 1)
        samples = n + 1
        is_self = bool(existing.get("is_self", False) or is_self)
        threshold = float(existing.get("threshold", threshold))
    else:
        merged = centroid
        samples = 1
    rec = {
        "centroid": [float(x) for x in merged],
        "threshold": float(threshold),
        "samples": samples,
        "is_self": bool(is_self),
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    store[name] = rec
    _write_store(path, store, vault_root=vault_root)
    return rec


def remove_profile(
    path: Path,
    name: str,
    *,
    vault_root: Path | None = None,
) -> bool:
    """Delete a profile. Returns True if it existed."""
    store = load_store(path, vault_root=vault_root)
    if name in store:
        del store[name]
        _write_store(path, store, vault_root=vault_root)
        return True
    return False


def list_profiles(
    path: Path,
    *,
    vault_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Summaries (no centroid) for the CLI, sorted by name."""
    out = []
    for name, rec in load_store(path, vault_root=vault_root).items():
        if not isinstance(rec, dict):
            continue
        out.append({
            "name": name,
            "samples": int(rec.get("samples", 0)),
            "is_self": bool(rec.get("is_self", False)),
            "threshold": float(rec.get("threshold", DEFAULT_THRESHOLD)),
            "updated": rec.get("updated"),
        })
    return sorted(out, key=lambda r: r["name"])
