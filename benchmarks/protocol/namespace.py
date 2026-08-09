"""Deterministic, provider-safe per-case namespace names."""

from __future__ import annotations

import hashlib
import re


def derive_namespace(run_id: str, case_id: str, provider_kind: str = "container") -> str:
    """Return a <=100 character alphanumeric-dash namespace per case."""

    prefix_by_provider = {
        "container": "bench", "tag": "tag", "filesystem": "fs", "generic": "bench",
        "exomem": "exo", "basic-memory": "basic", "supermemory": "super", "hybrid-rag": "hybrid",
    }
    prefix = prefix_by_provider.get(provider_kind, "bench")
    safe_run = re.sub(r"[^a-zA-Z0-9]+", "-", run_id).strip("-").lower()[:32] or "run"
    digest = hashlib.sha256(f"{run_id}\0{case_id}\0{provider_kind}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{safe_run}-{digest}"[:100]
