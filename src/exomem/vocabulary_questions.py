"""Explicit agent meaning questions anchored to one governed page read."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import memory_refs
from .vocabulary_state import VocabularyState
from .vocabulary_workflow import Evidence, family_descriptor, make_item


def submit(
    vault_root: Path,
    *,
    path: str,
    query: str,
    family: str,
    relation_ref: str | None = None,
) -> dict:
    """Persist one question-bound consideration without selecting or authorizing meaning."""
    family_descriptor(family)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise ValueError("VOCABULARY_QUESTION_INVALID: question must be 1 to 2000 characters")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("VOCABULARY_QUESTION_INVALID: canonical page path is required")
    if relation_ref is not None and family != "relation-type/v1":
        raise ValueError("VOCABULARY_QUESTION_INVALID: relation_ref requires relation-type/v1")

    from . import vocabulary_review

    requested_path = path.strip()
    anchor = vocabulary_review._read(
        vault_root,
        {"paths": {requested_path: requested_path}},
        requested_path,
    )
    anchor_path = anchor["path"]
    anchor_ref = _canonical_ref(
        vault_root,
        path=anchor_path,
        content_hash=anchor["content_hash"],
        frontmatter=anchor.get("frontmatter", {}),
    ) or anchor_path
    targets = {anchor_ref: anchor["content_hash"]}
    evidence = [Evidence(anchor_ref, anchor["content_hash"], "agent-meaning-question")]
    paths = {anchor_ref: anchor_path}
    application_route = None
    if relation_ref is not None:
        from . import relation_queue

        resolved = relation_queue.resolve_candidate(
            vault_root, relation_ref, source_path=anchor_path
        )
        candidate = resolved.candidate
        if candidate.get("from") != anchor_path:
            raise ValueError("VOCABULARY_QUESTION_INVALID: relation candidate must start at path")
        source_page = resolved.source_page
        if (
            source_page is None
            or getattr(source_page, "snapshot_hash", None) != anchor["content_hash"]
        ):
            raise ValueError(
                "VOCABULARY_EVIDENCE_UNAVAILABLE: refresh the relation candidate"
            )
        review_store = relation_queue.review_state_module.ReviewStateStore(vault_root)

        def require_open(payload) -> None:
            authored = relation_queue._current_candidate_is_authored(
                vault_root, source_page, candidate
            )
            if authored is None:
                raise relation_queue._refresh_required(relation_ref)
            reason, _ = relation_queue._classify_candidate(
                vault_root,
                source_page,
                candidate,
                store=review_store,
                state_payload=payload,
                exact_refs=resolved.identity_refs,
                authored=(
                    {
                        (
                            str(candidate.get("relation_type") or ""),
                            relation_queue.epistemic_graph_module._with_md(
                                str(candidate.get("to") or "")
                            ),
                        )
                    }
                    if authored
                    else set()
                ),
            )
            if reason is not None:
                raise ValueError(
                    "VOCABULARY_QUESTION_INVALID: relation candidate is no longer eligible; "
                    "refresh the relation queue"
                )

        require_open(review_store.load())
        target_path = candidate.get("to")
        if not isinstance(target_path, str) or not target_path:
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: refresh the relation candidate")
        target = vocabulary_review._read(
            vault_root,
            {"paths": {target_path: target_path}},
            target_path,
        )
        target_ref = _canonical_ref(
            vault_root,
            path=target["path"],
            content_hash=target["content_hash"],
            frontmatter=target.get("frontmatter", {}),
        ) or target["path"]
        targets[target_ref] = target["content_hash"]
        evidence.append(Evidence(target_ref, target["content_hash"], "agent-meaning-question"))
        paths[target_ref] = target["path"]
        application_route = {
            "tool": "connect_memory",
            "operation": "accept-relation",
            "ref": relation_ref,
            "path": anchor_path,
            "expected_fingerprint": resolved.fingerprint,
            "expected_hash": anchor["content_hash"],
            "requested_relation": None,
            "instructions": (
                "Record a reuse or propose-new decision for this paired item, then set "
                "requested_relation before invoking this route."
            ),
        }
    current = make_item(
        family=family,
        signal="agent-meaning-question",
        targets=targets,
        evidence=evidence,
        registry_hashes=vocabulary_review.registry_hashes(vault_root),
        projection_status="current",
        paths=paths,
        question=query,
        logical_identity=(f"relation-question:{relation_ref}" if relation_ref is not None else None),
        projection_currency=(
            {
                "candidate_ref": relation_ref,
                "candidate_fingerprint": resolved.fingerprint,
                "candidate_source_path": anchor_path,
                "candidate_target_path": target["path"],
            }
            if relation_ref is not None
            else None
        ),
    )
    item = VocabularyState(vault_root).observe(
        current,
        validator=require_open if relation_ref is not None else None,
    )
    result = {
        "item": item,
        "context_route": {"tool": "review_item_context", "ref": current.ref},
    }
    if application_route is not None:
        application_route["vocabulary_ref"] = current.ref
        application_route["vocabulary_fingerprint"] = current.fingerprint
        result["application_route"] = application_route
    return result


def _canonical_ref(
    vault_root: Path,
    *,
    path: str,
    content_hash: str,
    frontmatter: object,
) -> str | None:
    """Return a memory URI only when the current graph proves unique ownership."""
    if not isinstance(frontmatter, dict):
        return None
    identity = memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD))
    if identity is None:
        return None
    from . import epistemic_graph

    snapshot = epistemic_graph.EpistemicGraphIndex(vault_root)._open_read_snapshot()
    if snapshot is None:
        return None
    try:
        rows = snapshot.execute(
            "SELECT path, source_hash FROM graph_nodes "
            "WHERE kind = 'file' AND exomem_id = ? LIMIT 2",
            (identity,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        snapshot.close()
    if len(rows) != 1 or tuple(rows[0]) != (path, content_hash):
        return None
    return memory_refs.memory_ref(identity)
