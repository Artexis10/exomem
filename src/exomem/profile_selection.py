"""Which domain profiles a turn touches: retrieval restricted to the caller's profile pages.

Two readings score each profile page against the turn, and a page is selected when either one clears its floor.
The semantic reading searches the recall embedding index with the turn vector the activation pass already holds;
it encodes nothing. The lexical reading is one BM25 query over the same pages. The floors and the number of
domains kept belong to the vault's activation conventions, never to this module.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bm25, working_set_runtime

log = logging.getLogger(__name__)

SEMANTIC = "semantic"
LEXICAL = "lexical"
NONE = "none"

# A resource bound on the chunk rows one restricted vector search returns, not a vault choice. A profile page is
# a few chunks, so this covers the rows of a vault with hundreds of profile pages; the search clamps it to the
# rows the pages actually have.
VECTOR_ROWS_MAX = 2048


@dataclass(frozen=True, slots=True)
class Selection:
    """The profile pages the turn selected, strongest first, and the evidence that was read to select them.

    `evidence` is `semantic` when the vector reading ran (the lexical reading ran as well), `lexical` when it did
    not, because no turn vector existed or the recall matrix was not resident, and `none` when neither ran.
    """

    pages: tuple[str, ...]
    evidence: str


def select(
    root: Path, paths: Collection[str], turn: str, turn_vector: Any, *,
    semantic_floor: float, lexical_floor: float, restricted: bool,
) -> Selection:
    """The pages among `paths` that clear a floor for `turn`, ranked by semantic plus lexical score.

    `paths` are the caller's own profile pages, so neither reading can name a page the caller cannot see. A
    restricted caller's lexical statistics are taken over those pages alone, so a hidden page moves no score.
    """
    pages = set(paths)
    if not pages or not turn.strip():
        return Selection((), NONE)
    semantic = _semantic_scores(root, pages, turn_vector)
    lexical = _lexical_scores(root, pages, turn, restricted=restricted)
    if semantic is None and lexical is None:
        return Selection((), NONE)
    by_meaning, by_words = semantic or {}, lexical or {}
    chosen = [
        path for path in pages
        if by_meaning.get(path, float("-inf")) >= semantic_floor or by_words.get(path, float("-inf")) >= lexical_floor
    ]
    chosen.sort(key=lambda path: (-(by_meaning.get(path, 0.0) + by_words.get(path, 0.0)), path))
    return Selection(tuple(chosen), LEXICAL if semantic is None else SEMANTIC)


def _semantic_scores(root: Path, pages: set[str], turn_vector: Any) -> dict[str, float] | None:
    """The best chunk score of each page the vector reaches, or None when the reading did not run.

    It reads the resident recall matrix against the turn vector the activation pass encoded. It never encodes,
    never loads a matrix on this thread and never reads a page outside `pages`.
    """
    if turn_vector is None or os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return None
    import numpy as np

    from . import embeddings, readiness, recall_space

    try:
        index = embeddings.peek_embedding_index(root)
        fingerprint = embeddings.activation_fingerprint()
        if index is None or fingerprint is None or readiness.should_defer("embeddings"):
            return None
        managed = readiness.runtime_managed()
        # A one-shot process never loads the matrix, so a cold one is expected there; a service warms it.
        if not managed and index.resident_matrix() is None:
            return None
        identity = index.identity
        if identity is None or identity.fingerprint != fingerprint:
            return None
        query = np.asarray(turn_vector, dtype=np.float32).reshape(-1)
        if not np.all(np.isfinite(query)) or float(np.linalg.norm(query)) == 0.0:
            return None
        hits = index.select_chunks(query, VECTOR_ROWS_MAX, allowed_paths=pages, defer_cold_load=True)
    except recall_space.CorpusMatrixCold:
        return None
    except Exception:  # noqa: BLE001 - a failed vector reading leaves the lexical one standing, and the evidence says so
        log.debug("profile vector reading unavailable", exc_info=True)
        return None
    best: dict[str, float] = {}
    for path, _chunk, _text, score in hits:
        best[path] = max(float(score), best.get(path, float("-inf")))
    return best


def _lexical_scores(root: Path, pages: set[str], turn: str, *, restricted: bool) -> dict[str, float] | None:
    """The BM25 score of each page that shares a content word with the turn, or None when the query failed."""
    words = working_set_runtime.content_words(turn)
    if not words:
        return {}
    try:
        hits = bm25.search(
            root, words, len(pages), scope="kb", allowed_paths=pages,
            # A restricted caller's statistics cover the pages it may read only, so a hidden page moves no score.
            admitted_paths=pages if restricted else None,
        )
    except Exception:  # noqa: BLE001 - a failed query leaves the vector reading standing, and the evidence says so
        log.debug("profile lexical reading unavailable", exc_info=True)
        return None
    return {path: float(score) for path, score in hits}
