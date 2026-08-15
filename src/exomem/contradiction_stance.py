"""Authored contradictions and the competing-alternatives pair stance.

Two things live here, both about a PAIR of pages rather than a single note:

- `asserted_pairs()` / `structural_pair()` read the typed graph for relationships
  the author wrote down — a `contradicts` edge between two pages, or two `answers`
  edges into one question. An authored `contradicts` edge is the strongest
  contradiction signal a vault can carry (the author stated the conflict in their
  own words), and it is the source of the `corpus_contradictions` queue's asserted
  entries.
- the competing-alternatives stance — a durable "rivals; keep both" disposition on
  one pair, recorded in the SAME `.review-state.json` store, with the same record
  shape and the same `review_id:fingerprint` key, as `dismiss` and `snooze`.

The stance is keyed on the pair, not on a queue item, for one concrete reason: a
queue item's identity folds in every category that flagged its anchor, so the same
pair would key differently depending on whether the anchor also happened to be stale
that day — and the write-time draft check in `corpus_aware` knows only two paths, so
it could never reconstruct such an id. A pair-derived id is reachable from both.

The pair fingerprint folds in BOTH endpoints' on-disk content, so editing either
rival changes the fingerprint, the stored record stops matching, and the pair
resurfaces as open — the same honest-resurfacing property a fingerprint-bound
dismissal already has.

ALTITUDE: everything here is measurement and disposition recording. Nothing judges
which rival is right, ranks them against each other, merges, supersedes, or
auto-dismisses. The reader decides; the server only remembers what they decided.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import epistemic_graph as epistemic_graph_module
from . import review_state as review_state_module
from .vault import content_hash

log = logging.getLogger(__name__)

CONTRADICTS = "contradicts"
ANSWERS = "answers"
STANCE_ACTION = "competing"

# Namespaces the pair identity so it can never collide with an attention,
# activation, relation-queue, or adoption review id in the shared state file.
_PAIR_NAMESPACE = "competing"


# ----------------------------- pair identity -----------------------------


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Order-independent, `.md`-normalized identity for an unordered page pair."""
    left = epistemic_graph_module._with_md(str(a or ""))
    right = epistemic_graph_module._with_md(str(b or ""))
    return (left, right) if left <= right else (right, left)


def pair_item_id(ref_left: str, ref_right: str) -> str:
    """Stable review id for a pair. Refs MUST arrive in `pair_key` (path) order."""
    return review_state_module.item_id(
        f"{_PAIR_NAMESPACE}:{ref_left}|{ref_right}"
    )


def pair_signal_version(signal_left: str, signal_right: str) -> str:
    """A version that changes whenever EITHER endpoint's content changes."""
    return content_hash(
        f"{_PAIR_NAMESPACE}\n{signal_left}\n{signal_right}"
    )[:16]


def pair_fingerprint(
    *,
    ref_left: str,
    ref_right: str,
    signal_left: str,
    signal_right: str,
) -> str:
    """The pair's signal fingerprint, built through the shared review-state hasher."""
    return review_state_module.fingerprint(
        target_ref=ref_left,
        categories=[_PAIR_NAMESPACE],
        reasons=[
            {
                "category": _PAIR_NAMESPACE,
                "meta": {
                    "signal_version": pair_signal_version(signal_left, signal_right)
                },
            }
        ],
        related_refs=[ref_right],
    )


def page_signal_version(vault_root: Path, rel_path: str) -> str | None:
    """The on-disk content version of one page, or None when it is not readable."""
    from . import audit as audit_module
    from . import find as find_module

    root = Path(vault_root)
    try:
        page = find_module._CACHE.get(root / rel_path, root)
    except OSError:
        return None
    if page is None:
        return None
    return audit_module._page_signal_version(page)


def pair_identity(
    vault_root: Path,
    a: str,
    b: str,
    *,
    refs: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """`(review_id, fingerprint)` for one pair, or None when either page is gone.

    `refs` lets a caller that already resolved memory refs (the attention surface
    does) hand them in rather than paying a second reference-index lookup.
    """
    left, right = pair_key(a, b)
    signal_left = page_signal_version(vault_root, left)
    signal_right = page_signal_version(vault_root, right)
    if signal_left is None or signal_right is None:
        return None
    if refs is None or left not in refs or right not in refs:
        refs = review_state_module.refs_for_paths(vault_root, [left, right])
    ref_left = str(refs.get(left) or left)
    ref_right = str(refs.get(right) or right)
    return (
        pair_item_id(ref_left, ref_right),
        pair_fingerprint(
            ref_left=ref_left,
            ref_right=ref_right,
            signal_left=signal_left,
            signal_right=signal_right,
        ),
    )


# ----------------------------- stance read / write -----------------------------


def pair_decision(
    vault_root: Path,
    a: str,
    b: str,
    *,
    store: review_state_module.ReviewStateStore | None = None,
    payload: dict[str, Any] | None = None,
    refs: dict[str, str] | None = None,
) -> review_state_module.ReviewDecision | None:
    """The recorded competing stance for this pair at its CURRENT signal, else None."""
    identity = pair_identity(vault_root, a, b, refs=refs)
    if identity is None:
        return None
    store = store or review_state_module.ReviewStateStore(Path(vault_root))
    decision = store.decision(identity[0], identity[1], payload=payload)
    if decision is None or decision.action != STANCE_ACTION:
        return None
    return decision


def is_competing(
    vault_root: Path,
    a: str,
    b: str,
    *,
    store: review_state_module.ReviewStateStore | None = None,
    payload: dict[str, Any] | None = None,
    refs: dict[str, str] | None = None,
) -> bool:
    return (
        pair_decision(vault_root, a, b, store=store, payload=payload, refs=refs)
        is not None
    )


def pair_from_reasons(reasons: list[dict] | None) -> tuple[str, str] | None:
    """The single contradiction pair a review item carries, or None.

    A stance is meaningless without exactly one counterpart, so an item flagged
    over several distinct pairs (or none) yields None and the caller refuses.
    """
    pairs = {
        pair_key(paths[0], paths[1])
        for reason in (reasons or [])
        if str(reason.get("category") or "") == "corpus_contradictions"
        for paths in [sorted(set(reason.get("related_paths") or []))]
        if len(paths) == 2
    }
    if len(pairs) != 1:
        return None
    return next(iter(pairs))


def record_stance(
    vault_root: Path,
    *,
    reasons: list[dict] | None,
    until: str | None = None,
    why: str | None = None,
) -> dict[str, Any]:
    """Persist the competing-alternatives stance for a review item's pair."""
    pair = pair_from_reasons(reasons)
    if pair is None:
        raise ValueError(
            "INVALID_REVIEW_ACTION: `competing` records that two notes are rivals "
            "worth keeping, so it applies only to a review item carrying exactly "
            "one contradiction pair"
        )
    identity = pair_identity(vault_root, *pair)
    if identity is None:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: one side of the pair is no longer readable; "
            "refresh the worklist and inspect the item again"
        )
    result = review_state_module.ReviewStateStore(Path(vault_root)).apply(
        identity[0], identity[1], action=STANCE_ACTION, until=until, why=why
    )
    result["pair"] = list(pair)
    result["pair_ref"] = review_state_module.review_ref(identity[0])
    return result


def clear_stance(vault_root: Path, *, reasons: list[dict] | None) -> None:
    """Drop any recorded stance for a review item's pair. Best-effort, never raises."""
    pair = pair_from_reasons(reasons)
    if pair is None:
        return
    identity = pair_identity(vault_root, *pair)
    if identity is None:
        return
    review_state_module.ReviewStateStore(Path(vault_root)).apply(
        identity[0], identity[1], action="reopen"
    )


# ----------------------------- authored graph edges -----------------------------


def _index(vault_root: Path) -> epistemic_graph_module.EpistemicGraphIndex:
    return epistemic_graph_module.EpistemicGraphIndex(Path(vault_root))


def asserted_pairs(
    vault_root: Path,
    *,
    index: epistemic_graph_module.EpistemicGraphIndex | None = None,
) -> list[tuple[str, str]]:
    """Deduped, unordered page pairs joined by an authored `contradicts` edge.

    `contradicts` is symmetric, so one authored bullet yields ONE pair, ordered by
    path exactly like a proximity pair. A disabled, missing, or warming graph index
    yields `[]` — an explicit absence, never a fabricated result.
    """
    try:
        idx = index or _index(vault_root)
        participants = idx.relation_participants([CONTRADICTS])
        if participants.status != "available" or not participants.paths:
            return []
        pairs: set[tuple[str, str]] = set()
        for path in sorted(participants.paths):
            anchored = idx.relation_participants([CONTRADICTS], anchor=path)
            if anchored.status != "available":
                continue
            for other in anchored.paths:
                if other != path:
                    pairs.add(pair_key(path, other))
        return sorted(pairs)
    except Exception as error:  # noqa: BLE001 — a graph miss never breaks review
        log.debug("asserted contradiction lookup failed: %s", error)
        return []


def structural_pair(
    vault_root: Path,
    a: str,
    b: str,
    *,
    index: epistemic_graph_module.EpistemicGraphIndex | None = None,
) -> str | None:
    """Why the author already declared this pair as rivals, or None.

    Returns `"contradicts"` for an authored contradiction edge between the two, and
    `"answers_same_question"` when both pages answer one common target. Either way
    the write-time proximity warning would only tell the author what they typed.
    """
    left, right = pair_key(a, b)
    if left == right:
        return None
    try:
        idx = index or _index(vault_root)
        contra = idx.relation_participants([CONTRADICTS], anchor=left)
        if contra.status == "available" and right in contra.paths:
            return CONTRADICTS
        left_answers = idx.relation_participants(
            [ANSWERS], anchor=left, direction="outbound"
        )
        right_answers = idx.relation_participants(
            [ANSWERS], anchor=right, direction="outbound"
        )
        if (
            left_answers.status == "available"
            and right_answers.status == "available"
            and (left_answers.paths & right_answers.paths)
        ):
            return "answers_same_question"
    except Exception as error:  # noqa: BLE001 — a graph miss never breaks a write
        log.debug("structural pair lookup failed for (%s, %s): %s", left, right, error)
    return None


def declared_pairs(vault_root: Path, self_path: str, others: list[str]) -> set[str]:
    """The subset of `others` already declared a rival pair with `self_path`.

    Declared means the reader recorded a competing stance, or the pages already
    carry an authored `contradicts` edge, or both answer one question. Used by the
    write-time near-duplicate and overlap warnings, which have nothing to add once
    the relationship is on the page. Best-effort: any failure declares nothing, so a
    warning is never silently lost to an infrastructure problem.
    """
    if not self_path or not others:
        return set()
    try:
        idx = _index(vault_root)
        store = review_state_module.ReviewStateStore(Path(vault_root))
        payload = store.load()
        declared: set[str] = set()
        for other in others:
            if not other:
                continue
            left, right = pair_key(self_path, other)
            if left == right:
                continue  # the draft's own page is never its own rival
            if structural_pair(vault_root, self_path, other, index=idx) is not None:
                declared.add(other)
                continue
            if is_competing(
                vault_root, self_path, other, store=store, payload=payload
            ):
                declared.add(other)
        return declared
    except Exception as error:  # noqa: BLE001 — never break a write
        log.debug("declared-pair filter failed for %s: %s", self_path, error)
        return set()
