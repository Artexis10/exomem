"""Optional post-commit guidance and bounded reconstruction, never a writer.

The canonical command owner calls ``after_commit`` outside its content guard.
Recovery only re-reads point/index evidence. It never invokes the original
mutation, changes its identity, or treats unavailable evidence as an empty KB.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    deferred_index,
    envelope,
    mutation_terminal,
    vocabulary_notifications,
    vocabulary_projection,
    vocabulary_recovery,
    vocabulary_review,
)
from .vocabulary_state import VocabularyState
from .vocabulary_workflow import _hash


def _sync(state: str, reason: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"state": state}
    if reason:
        result["reason"] = reason
    if state in {"warming", "unavailable"}:
        result["recovery"] = {"tool": "review_memory", "args": {"mode": "vocabulary"}}
    return result


def _project(vault_root: Path, path: str, continuation: str | None = None) -> dict[str, Any]:
    projected = vocabulary_projection.for_write(vault_root, path=path, continuation=continuation)
    if projected["status"] == "warming":
        return {
            "sync": _sync("warming", "source_discovery_pending"),
            "items": [],
            "continuation": projected["continuation"],
        }
    if projected["status"] != "current":
        return {"sync": _sync("unavailable", "projection_unavailable"), "items": []}
    if any(signal.get("eligibility") == "unavailable" for signal in projected.get("signals", [])):
        return {"sync": _sync("unavailable", "origin_independence_unavailable"), "items": []}
    return {
        "sync": _sync("current"),
        "items": projected["items"],
        "continuation": projected.get("continuation"),
    }


def after_commit(vault_root: Path, result: Any) -> Any:
    """Preserve the canonical terminal even if any optional-guidance step fails."""
    if (
        not isinstance(result, Mapping)
        or result.get("_terminal") != mutation_terminal._TERMINAL_MARKER
        or result.get("version") != mutation_terminal._TERMINAL_VERSION
        or result.get("state") != "committed"
        or result.get("ok") is not True
    ):
        return result
    leaf = result.get("leaf_result")
    path = (
        deferred_index._safe_markdown_rel_path(leaf.get("path"))
        if isinstance(leaf, Mapping)
        else None
    )
    if path is None:
        return result
    try:
        key = _hash([result.get("request_id"), result.get("receipt_id"), path])
        vocabulary_recovery.enqueue(vault_root, key, path)
        job = vocabulary_recovery.Job(key, path, 0, None)
        if not vocabulary_recovery.claim(vault_root, job):
            return {**result, "vocabulary_sync": _sync("warming", "source_discovery_pending")}
        # Capture a recovery locator before optional policy/projection reads.
        # An off envelope still emits no unsolicited guidance; explicit review
        # can later inspect the committed page under its current permissions.
        if envelope.resolved()["classes"]["structural_suggestions"]["disposition"] == "off":
            return result
        projected = _project(vault_root, path)
        guidance: dict[str, Any] = {"vocabulary_sync": projected["sync"]}
        if projected["sync"]["state"] in {"warming", "current"}:
            notice = (
                vocabulary_notifications.take_advisory(vault_root, projected["items"])
                if projected["sync"]["state"] == "current" else None
            )
            if notice:
                guidance.update(vocabulary_advisory=notice, _vocabulary_vault=str(vault_root))

            vocabulary_recovery.complete(
                vault_root, job, continuation=projected["continuation"]
            )
        return {**result, **guidance}
    except Exception:  # noqa: BLE001 - optional guidance cannot change a committed outcome
        return {**result, "vocabulary_sync": _sync("unavailable", "guidance_unavailable")}


def recover(vault_root: Path, *, limit: int = 4) -> dict[str, Any]:
    """Reconstruct at most four visible pending projections on explicit review."""
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("VOCABULARY_LIMIT_INVALID: recovery budget must be 1 to 4")
    owner = VocabularyState(vault_root)
    processed = 0
    unavailable = False
    warming = False
    for job in vocabulary_recovery.page(vault_root, limit=limit):
        if not vocabulary_recovery.claim(vault_root, job):
            continue
        # A fixed window bounds disclosure work even when every job is hidden.
        # Rotation is private: hidden rows cannot affect public counts/cursors.
        if not vocabulary_review._visible(
            vault_root, {"target_versions": {job.path: "pending"}, "evidence": []}
        ):
            continue
        try:
            projected = _project(vault_root, job.path, job.continuation)
            if projected["sync"]["state"] == "unavailable":
                unavailable = True
                continue
            warming = warming or projected["sync"]["state"] == "warming" or bool(
                projected["continuation"]
            )
            for item in projected["items"]:
                owner.observe(item)
            vocabulary_recovery.complete(
                vault_root, job, continuation=projected["continuation"]
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 - recovery never repeats content mutation
            unavailable = True
            if isinstance(exc, ValueError) and str(exc).startswith("VOCABULARY_CONTINUATION_STALE"):
                vocabulary_recovery.reset_cursor(vault_root, job)
    return {
        "state": "unavailable" if unavailable else "warming" if warming else "current",
        "processed": processed,
        "coverage": "bounded-pass",
    }


def public_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    """Re-evaluate disclosure on cached notices; never release internal roots."""
    if result.get("state") != "committed":
        return {}
    public = {}
    sync = result.get("vocabulary_sync")
    if isinstance(sync, Mapping) and sync.get("state") in {"current", "warming", "unavailable"}:
        public["vocabulary_sync"] = _sync(
            sync["state"],
            {"unavailable": "guidance_unavailable", "warming": "source_discovery_pending"}.get(
                sync["state"]
            ),
        )
    notice = result.get("vocabulary_advisory")
    root = result.get("_vocabulary_vault")
    if not isinstance(notice, Mapping) or not isinstance(root, str):
        return public
    try:
        item = VocabularyState(Path(root)).get(notice["ref"])
        if item["fingerprint"] == notice["fingerprint"] and vocabulary_review._visible(
            Path(root), item
        ):
            from .vocabulary_signals import compact_advisory

            public["vocabulary_advisory"] = compact_advisory(
                ref=item["ref"], family=item["family"], fingerprint=item["fingerprint"]
            )
    except Exception:  # noqa: BLE001 - optional guidance fails closed without failing a receipt
        pass
    return public
