"""Deterministic, provider-safe per-case namespace names."""

from __future__ import annotations

import hashlib
import re


_PREFIX_BY_PROVIDER = {
    "container": "bench", "tag": "tag", "filesystem": "fs", "generic": "bench",
    "exomem": "exo", "basic-memory": "basic", "supermemory": "super", "hybrid-rag": "hybrid",
}
_DIGEST_LENGTH = 24


def derive_namespace(run_id: str, case_id: str, provider_kind: str = "container") -> str:
    """Return a <=100 character alphanumeric-dash namespace per case."""

    if provider_kind == "exomem":
        # MemoryBench owns this external tag grammar. Both implementations
        # contain the derived directory below a distinct run-owned root.
        return hashlib.sha256(f"{case_id}-{run_id}".encode("utf-8")).hexdigest()[:24]
    prefix = _PREFIX_BY_PROVIDER.get(provider_kind, "bench")
    safe_run = re.sub(r"[^a-zA-Z0-9]+", "-", run_id).strip("-").lower()[:32] or "run"
    digest = hashlib.sha256(f"{run_id}\0{case_id}\0{provider_kind}".encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{prefix}-{safe_run}-{digest}"[:100]


def namespace_pattern(provider_kind: str = "container") -> str:
    """The run-invariant shape of :func:`derive_namespace` for a provider kind.

    Equivalence comparison checks the derivation scheme, not the literal name:
    the literal embeds the run id, so two honest runs of the same dataset could
    never match on it. The literal per-case namespace is recorded in the run
    manifest, where it belongs.
    """

    if provider_kind == "exomem":
        return "exomem-container-tag-sha256-24hex"
    return f"{_PREFIX_BY_PROVIDER.get(provider_kind, 'bench')}-run-{_DIGEST_LENGTH}hex"
