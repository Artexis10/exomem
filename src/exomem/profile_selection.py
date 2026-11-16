"""Which domain profiles a turn touches: retrieval restricted to the caller's profile pages.

Two readings score each profile page against the turn, and a page is selected when either one clears its floor.
The semantic reading searches the recall embedding index with the turn vector the activation pass already holds;
it encodes nothing. The lexical reading is one BM25 query over the same pages, read from the lexical catalogue at
the checkpoint the activation request already holds, so it walks no vault. The floors and the number of domains
kept belong to the vault's activation conventions, never to this module.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import lexstore, working_set_runtime

log = logging.getLogger(__name__)

SEMANTIC = "semantic"
LEXICAL = "lexical"
NONE = "none"


@dataclass(frozen=True, slots=True)
class LexicalBasis:
    """The freshness and lexical checkpoint the activation request reads its catalogue at.

    The keyword reading queries the catalogue at exactly this checkpoint. Without one it does not run, and it
    never works one out from the vault.
    """

    freshness: tuple
    checkpoint: Any


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
    semantic_floor: float, lexical_floor: float, restricted: bool, lexical: LexicalBasis | None = None,
) -> Selection:
    """The pages among `paths` that clear a floor for `turn`, ranked by semantic plus lexical score.

    `paths` are the caller's own profile pages, so neither reading can name a page the caller cannot see. A
    restricted caller's lexical statistics are taken over those pages alone, so a hidden page moves no score.
    """
    pages = set(paths)
    if not pages or not turn.strip():
        return Selection((), NONE)
    semantic = _semantic_scores(root, pages, turn_vector)
    keywords = _lexical_scores(root, pages, turn, restricted=restricted, basis=lexical)
    if semantic is None and keywords is None:
        return Selection((), NONE)
    by_meaning, by_words = semantic or {}, keywords or {}
    chosen = [
        path for path in pages
        if by_meaning.get(path, float("-inf")) >= semantic_floor or by_words.get(path, float("-inf")) >= lexical_floor
    ]
    chosen.sort(key=lambda path: (-(by_meaning.get(path, 0.0) + by_words.get(path, 0.0)), path))
    return Selection(tuple(chosen), LEXICAL if semantic is None else SEMANTIC)


def _semantic_scores(root: Path, pages: set[str], turn_vector: Any) -> dict[str, float] | None:
    """The best chunk score of each page the vector reaches, or None when the reading did not run.

    It scores the profile pages' rows of the resident recall matrix against the turn vector the activation pass
    encoded, so its cost follows the profile pages, not the vault. It never encodes and never loads a matrix on
    this thread.
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
        return index.best_scores_for_paths(query, pages)
    except recall_space.CorpusMatrixCold:
        return None
    except Exception:  # noqa: BLE001 - a failed vector reading leaves the lexical one standing, and the evidence says so
        log.debug("profile vector reading unavailable", exc_info=True)
        return None


def _lexical_scores(
    root: Path, pages: set[str], turn: str, *, restricted: bool, basis: LexicalBasis | None,
) -> dict[str, float] | None:
    """The BM25 score of each page that shares a content word with the turn, or None when the reading did not run.

    The query reads the maintained catalogue at the request's checkpoint (`allow_delta=False`: it applies no
    catch-up and walks nothing). A catalogue that is not ready at that checkpoint, or no checkpoint at all, is a
    reading that did not run, never a walk or a second corpus build.
    """
    words = working_set_runtime.content_words(turn)
    if not words:
        return {}
    if basis is None:
        return None
    try:
        result = lexstore.search_bm25_result(
            root, words, len(pages), scope="kb", freshness=basis.freshness, recall_checkpoint=basis.checkpoint,
            allowed_paths=pages, allow_delta=False,
            # A restricted caller's statistics cover the pages it may read only, so a hidden page moves no score.
            admitted_paths=pages if restricted else None,
        )
    except Exception:  # noqa: BLE001 - a failed query leaves the vector reading standing, and the evidence says so
        log.debug("profile lexical reading unavailable", exc_info=True)
        return None
    if not result.readiness.complete or result.value is None:
        return None
    return {path: float(score) for path, score in result.value}
