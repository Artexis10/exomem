"""Adapt already-computed entity lifecycle evidence to vocabulary consideration.

The lifecycle owner detects identities and chooses promotion/hydration/ambiguity.
This adapter only binds its current bounded evidence and exposes the existing
curation route. It never repeats the audit or selects a meaning or entity.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import curation, vocabulary_review
from .vocabulary_state import VocabularyState
from .vocabulary_workflow import Evidence, WorkItem, _hash, make_item

MAX_CANDIDATES = 3


def refresh(vault_root: Path, reference: str) -> WorkItem:
    """Revalidate explicit review against the lifecycle owner's current signal."""
    from . import attention

    try:
        candidate = attention.entity_candidate_by_ref(vault_root, reference)
    except ValueError as exc:
        raise ValueError(
            "VOCABULARY_DECISION_STALE: refresh the changed entity consideration"
        ) from exc
    return from_candidate(vault_root, candidate.as_dict())


def from_candidate(vault_root: Path, candidate: Mapping[str, Any]) -> WorkItem:
    """Consume the provider's exact review binding and point-read its pages."""
    registries = vocabulary_review.registry_hashes(vault_root)
    binding = curation._entity_candidate_binding(
        SimpleNamespace(**candidate), registry_fallback=registries["entity_types"]
    )
    context_paths = list(dict.fromkeys(
        context["path"] for context in binding["first_disconnected_context_batch"]
    ))
    target_paths = list(dict.fromkeys(binding["target_refs"]))
    if len(context_paths) > 8 or len(target_paths) > 8 or not context_paths:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: bounded lifecycle evidence required")
    pages = {
        path: vocabulary_review._read(vault_root, {}, path)
        for path in dict.fromkeys([*context_paths, *target_paths])
    }
    # Promotion has no existing entity; its reviewed source pages bind the
    # proposed creation. Hydration additionally binds each exact entity target.
    targets = {path: pages[path]["content_hash"] for path in [*context_paths, *target_paths]}
    currency = {
        "provider": "entity-lifecycle/v1",
        "review_ref": binding["review_ref"],
        "review_fingerprint": binding["review_fingerprint"],
        "candidate_state": binding["candidate_state"],
        "signal_version": binding["signal_version"],
        "binding_hash": _hash(binding),
    }
    return make_item(
        family="entity-instance/v1",
        signal="entity-lifecycle",
        logical_identity=binding["review_ref"],
        targets=targets,
        evidence=[Evidence(path, pages[path]["content_hash"]) for path in context_paths],
        registry_hashes=registries,
        projection_status="current",
        projection_currency=currency,
    )


def annotate_report(vault_root: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Publish at most three considerations from the category pass just served."""
    candidates = [
        row for row in report.get("items", [])
        if "entity_recurrence" in row.get("categories", [])
    ]
    if not candidates:
        return report
    owner = VocabularyState(vault_root)
    for row in candidates[:MAX_CANDIDATES]:
        try:
            item = from_candidate(vault_root, row)
            observed = owner.observe(item)
            state = observed["projection"]["currency"]["candidate_state"]
            row["vocabulary"] = {
                "ref": item.ref,
                "fingerprint": item.fingerprint,
                "recommended_outcome": {
                    "promotion": "propose-new", "hydration": "enrich", "ambiguous": "defer",
                }[state],
                "work_item_route": {
                    "tool": "maintain_memory",
                    "args": {
                        "mode": "curation", "curation_action": "work-item",
                        "review_ref": observed["logical_identity"],
                    },
                },
                "permission": "consideration does not authorize mutation",
            }
        except (OSError, ValueError, KeyError):
            # Optional integration must not conceal the original lifecycle row.
            row["vocabulary"] = {"state": "unavailable", "reason": "provider_evidence_unavailable"}
    if len(candidates) > MAX_CANDIDATES:
        report["vocabulary_coverage"] = {
            "returned": MAX_CANDIDATES,
            "omitted": len(candidates) - MAX_CANDIDATES,
            "source": "current-attention-page",
        }
    return report
