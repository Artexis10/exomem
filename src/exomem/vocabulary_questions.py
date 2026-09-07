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
) -> dict:
    """Persist one question-bound consideration without selecting or authorizing meaning."""
    family_descriptor(family)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise ValueError("VOCABULARY_QUESTION_INVALID: question must be 1 to 2000 characters")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("VOCABULARY_QUESTION_INVALID: canonical page path is required")

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
    current = make_item(
        family=family,
        signal="agent-meaning-question",
        targets={anchor_ref: anchor["content_hash"]},
        evidence=[Evidence(anchor_ref, anchor["content_hash"], "agent-meaning-question")],
        registry_hashes=vocabulary_review.registry_hashes(vault_root),
        projection_status="current",
        paths={anchor_ref: anchor_path},
        question=query,
    )
    item = VocabularyState(vault_root).observe(current)
    return {
        "item": item,
        "context_route": {"tool": "review_item_context", "ref": current.ref},
    }


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
