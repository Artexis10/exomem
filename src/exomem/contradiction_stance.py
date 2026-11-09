"""Authored contradictions and the competing-alternatives pair stance.

Two things live here, both about a PAIR of pages rather than a single note:

- `asserted_pairs()` / `structural_pair()` read the typed graph for relationships
  the author wrote down — an edge that opposes one page to another, or two edges
  that settle one question. An authored opposing edge is the strongest
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import epistemic_graph as epistemic_graph_module
from . import relation_registry
from . import review_state as review_state_module
from .vault import content_hash

log = logging.getLogger(__name__)

#: The reason codes `structural_pair` returns. They name why a pair is declared and are
#: shown to callers; they do not select relations.
REASON_OPPOSED = "contradicts"
REASON_SETTLED_TOGETHER = "answers_same_question"
STANCE_ACTION = "competing"

# Namespaces the pair identity so it can never collide with an attention,
# activation, relation-queue, or adoption review id in the shared state file.
_PAIR_NAMESPACE = "competing"


# ----------------------------- pair identity -----------------------------


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Order-independent, `.md`-normalized identity for an unordered page pair."""
    left = epistemic_graph_module._page_rel(str(a or ""))
    right = epistemic_graph_module._page_rel(str(b or ""))
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


def annotate_reasons(
    vault_root: Path,
    reasons: list[dict] | None,
    *,
    store: review_state_module.ReviewStateStore,
    payload: dict[str, Any] | None = None,
    refs: dict[str, str] | None = None,
) -> dict[tuple[str, str], review_state_module.ReviewDecision | None]:
    """Tag each contradiction reason with its pair's stance, and return the map.

    Without this a drifted item serializes as an ordinary open item with two
    reasons and no hint that one of them is already dispositioned and silently
    muting a write-time warning. Each reason gains `pair_ref` — the handle that
    addresses that pair's stance directly — and, when a stance is recorded,
    `stance`.

    MUST be called after the item's `signal_fingerprint` is computed:
    `review_state.fingerprint` reads only `category`, `meta.signal_version`,
    `detail`, and `related_paths`, so these keys cannot feed back into review
    identity, but computing the fingerprint first keeps that independent of
    ordering rather than of a field list.
    """
    resolved: dict[
        tuple[str, str],
        tuple[tuple[str, str] | None, review_state_module.ReviewDecision | None],
    ] = {}
    for reason in reasons or []:
        if str(reason.get("category") or "") != "corpus_contradictions":
            continue
        paths = sorted(set(reason.get("related_paths") or []))
        if len(paths) != 2:
            continue
        pair = pair_key(paths[0], paths[1])
        if pair[0] == pair[1]:
            continue
        if pair not in resolved:
            identity = pair_identity(vault_root, *pair, refs=refs)
            decision = None
            if identity is not None:
                found = store.decision(identity[0], identity[1], payload=payload)
                if found is not None and found.action == STANCE_ACTION:
                    decision = found
            resolved[pair] = (identity, decision)
        identity, decision = resolved[pair]
        if identity is not None:
            reason["pair_ref"] = review_state_module.review_ref(identity[0])
        if decision is not None:
            reason["stance"] = STANCE_ACTION
    return {pair: decision for pair, (_identity, decision) in resolved.items()}


def clear_orphan_stance(
    vault_root: Path, *, ref: str
) -> dict[str, Any] | None:
    """Clear a competing stance addressed directly by its own pair ref, or None.

    A stance whose pair has drifted off every queue item is otherwise unreachable:
    `clear_stance` walks the pairs an ITEM currently carries, and an orphan is on
    none of them, while the record keeps suppressing the write-time warning. The
    pair ref returned by the original stance write addresses it directly.

    Returns None when `ref` is not a review reference or carries no stance record,
    so the caller can fall through to its ordinary not-found error.
    """
    try:
        review_id = review_state_module.parse_review_ref(ref)
    except ValueError:
        return None
    store = review_state_module.ReviewStateStore(Path(vault_root))
    payload = store.load()
    prefix = f"{review_id}:"
    stanced = [
        record
        for key, record in payload["records"].items()
        if key.startswith(prefix)
        and isinstance(record, dict)
        and str(record.get("action") or "") == STANCE_ACTION
    ]
    if not stanced:
        return None
    fingerprint_value = str(stanced[0].get("fingerprint") or "")
    result = store.apply(review_id, fingerprint_value, action="reopen")
    result["cleared"] = STANCE_ACTION
    return result


def pairs_from_reasons(reasons: list[dict] | None) -> list[tuple[str, str]]:
    """EVERY distinct contradiction pair a review item carries, in pair order.

    Both contradiction lanes anchor a pair on `min(a, b)`, so a note that is the
    alphabetically-first endpoint of two conflicts collapses into ONE attention item
    carrying two reasons — the ordinary "this conflicts with these two older ones"
    shape. Returning only a lone pair would leave such an item unstanceable and,
    worse, leave an existing stance un-clearable once a second pair drifted onto the
    same anchor while it kept muting the write-time warning. Callers handle the list.
    """
    pairs = {
        pair_key(paths[0], paths[1])
        for reason in (reasons or [])
        if str(reason.get("category") or "") == "corpus_contradictions"
        for paths in [sorted(set(reason.get("related_paths") or []))]
        if len(paths) == 2
    }
    return sorted(pair for pair in pairs if pair[0] != pair[1])


def record_stance(
    vault_root: Path,
    *,
    reasons: list[dict] | None,
    until: str | None = None,
    why: str | None = None,
) -> list[dict[str, Any]]:
    """Persist the competing-alternatives stance on every pair the item carries.

    The triage surface addresses an ITEM, and an item can legitimately carry more
    than one conflict, so "these are rivals I keep" applies to each of them. Every
    pair record stays independently fingerprint-bound, so editing one rival reopens
    only the pairs that note participates in.
    """
    pairs = pairs_from_reasons(reasons)
    if not pairs:
        raise ValueError(
            "INVALID_REVIEW_ACTION: `competing` records that two notes are rivals "
            "worth keeping, so it applies only to a review item carrying at least "
            "one contradiction pair"
        )
    store = review_state_module.ReviewStateStore(Path(vault_root))
    recorded: list[dict[str, Any]] = []
    for pair in pairs:
        identity = pair_identity(vault_root, *pair)
        if identity is None:
            raise ValueError(
                "REVIEW_ITEM_CHANGED: one side of the pair is no longer readable; "
                "refresh the worklist and inspect the item again"
            )
        applied = store.apply(
            identity[0], identity[1], action=STANCE_ACTION, until=until, why=why
        )
        recorded.append(
            {
                "paths": list(pair),
                "ref": applied["ref"],
                "fingerprint": identity[1],
                "decision": applied["decision"],
            }
        )
    return recorded


def clear_stance(vault_root: Path, *, reasons: list[dict] | None) -> None:
    """Drop every recorded stance on a review item's pairs. Never raises.

    `ReviewStateStore.apply(action="reopen")` clears by review id, not by
    fingerprint, so this also releases a stance recorded against an earlier content
    version — which is exactly the state a drifted pair would otherwise be stuck in.
    """
    store = review_state_module.ReviewStateStore(Path(vault_root))
    for pair in pairs_from_reasons(reasons):
        identity = pair_identity(vault_root, *pair)
        if identity is None:
            continue
        store.apply(identity[0], identity[1], action="reopen")


# ----------------------------- authored graph edges -----------------------------


def _index(vault_root: Path) -> epistemic_graph_module.EpistemicGraphIndex:
    return epistemic_graph_module.EpistemicGraphIndex(Path(vault_root))


def _core_keys_with(behaviour: str) -> list[str]:
    """The core relations with `behaviour`; the graph reaches an extension through its parent."""
    return sorted(relation_registry.core_registry().keys_with(behaviour))


@dataclass(frozen=True)
class DeclaredEdges:
    """One snapshot of the authored edges that declare two pages as rivals.

    Built from exactly TWO indexed queries for the whole vault, then answered from
    memory. The earlier per-anchor form re-ran an unnarrowed edge query per page,
    which is O(pages x edges) and reaches the retrieve/inject path through deep
    packs.
    """

    opposed: frozenset[tuple[str, str]]       # `pair_key` form
    settled: dict[str, frozenset[str]]        # page -> the targets it settles


def declared_edges(
    vault_root: Path,
    *,
    index: epistemic_graph_module.EpistemicGraphIndex | None = None,
) -> DeclaredEdges:
    """Read both declaring behaviours in two queries. Empty on any unavailability."""
    try:
        idx = index or _index(vault_root)
        opposing = idx.relation_edges(_core_keys_with("opposes"))
        settling = idx.relation_edges(_core_keys_with("settles"))
        pairs = (
            frozenset(pair_key(src, dst) for src, dst in opposing.edges)
            if opposing.status == "available"
            else frozenset()
        )
        settled: dict[str, set[str]] = {}
        if settling.status == "available":
            for src, dst in settling.edges:
                settled.setdefault(src, set()).add(dst)
        return DeclaredEdges(
            opposed=frozenset(pair for pair in pairs if pair[0] != pair[1]),
            settled={page: frozenset(targets) for page, targets in settled.items()},
        )
    except Exception as error:  # noqa: BLE001 — a graph miss never breaks a write
        log.debug("declared-edge snapshot failed: %s", error)
        return DeclaredEdges(opposed=frozenset(), settled={})


def asserted_pairs(
    vault_root: Path,
    *,
    index: epistemic_graph_module.EpistemicGraphIndex | None = None,
) -> list[tuple[str, str]]:
    """Deduped, unordered page pairs joined by an authored edge that opposes them.

    An opposing relation is symmetric, so one authored bullet yields ONE pair, ordered by
    path exactly like a proximity pair. One indexed query for the whole vault. A
    disabled, missing, or warming graph index yields `[]` — an explicit absence,
    never a fabricated result.
    """
    try:
        idx = index or _index(vault_root)
        result = idx.relation_edges(_core_keys_with("opposes"))
        if result.status != "available":
            return []
        pairs = {pair_key(src, dst) for src, dst in result.edges}
        return sorted(pair for pair in pairs if pair[0] != pair[1])
    except Exception as error:  # noqa: BLE001 — a graph miss never breaks review
        log.debug("asserted contradiction lookup failed: %s", error)
        return []


def structural_pair(
    vault_root: Path,
    a: str,
    b: str,
    *,
    edges: DeclaredEdges | None = None,
    index: epistemic_graph_module.EpistemicGraphIndex | None = None,
) -> str | None:
    """Why the author already declared this pair as rivals, or None.

    Returns `REASON_OPPOSED` for an authored edge that opposes the two, and
    `REASON_SETTLED_TOGETHER` when both pages settle one common target. Either way
    the write-time proximity warning would only tell the author what they typed.
    Pass a prepared `edges` snapshot to answer many candidates without re-querying.
    """
    left, right = pair_key(a, b)
    if left == right:
        return None
    snapshot = edges if edges is not None else declared_edges(vault_root, index=index)
    if (left, right) in snapshot.opposed:
        return REASON_OPPOSED
    if snapshot.settled.get(left, frozenset()) & snapshot.settled.get(right, frozenset()):
        return REASON_SETTLED_TOGETHER
    return None


class DeclaredPairFilter:
    """Memoized "has the author already declared this pair?" predicate.

    Declared means the reader recorded a competing stance, or the pages already
    carry an authored edge that opposes them, or both settle one question. Used by the
    write-time near-duplicate and overlap warnings, which have nothing to add once
    the relationship is on the page.

    Everything is built LAZILY on the first candidate and shared for the rest of the
    call, so a write with no candidate pays nothing and a write with several pays two
    graph queries and one state read in total rather than per candidate. Best-effort:
    any failure declares nothing, so a warning is never silently lost to an
    infrastructure problem.
    """

    def __init__(self, vault_root: Path, self_path: str | None):
        self.vault_root = Path(vault_root)
        self.self_path = str(self_path) if self_path else None
        self._edges: DeclaredEdges | None = None
        self._store: review_state_module.ReviewStateStore | None = None
        self._payload: dict[str, Any] | None = None
        self._cache: dict[str, bool] = {}

    def __call__(self, other: str) -> bool:
        if not self.self_path or not other:
            return False
        cached = self._cache.get(other)
        if cached is not None:
            return cached
        declared = False
        try:
            left, right = pair_key(self.self_path, other)
            if left != right:  # the draft's own page is never its own rival
                if self._edges is None:
                    self._edges = declared_edges(self.vault_root)
                    self._store = review_state_module.ReviewStateStore(self.vault_root)
                    self._payload = self._store.load()
                declared = (
                    structural_pair(
                        self.vault_root, self.self_path, other, edges=self._edges
                    )
                    is not None
                    or is_competing(
                        self.vault_root,
                        self.self_path,
                        other,
                        store=self._store,
                        payload=self._payload,
                    )
                )
        except Exception as error:  # noqa: BLE001 — never break a write
            log.debug("declared-pair check failed for %s: %s", other, error)
            declared = False
        self._cache[other] = declared
        return declared
