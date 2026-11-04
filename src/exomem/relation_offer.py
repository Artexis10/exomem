"""Specific-relation offers for a generic edge between two notes (task 2.7).

`relation_barrier` stops a generic edge to an entity before it commits. A generic edge
between two notes stays writable: it counts as connectivity, and the typed-edge warning
applies. Once such a write has committed, this module turns the options that
`relation_options.fitting_options` found into routes the caller can follow: each route is one
hash-guarded `edit_note` call that swaps the generic predicate of that bullet for the chosen
key, so the page ends with one edge rather than two.

The agent can decline an offer for good through the write-advisory dismissal. Its identity is
the pair of pages, so a dismissal survives a later change to the registry.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import call_spans, markdown_relations, relation_barrier, relation_options

log = logging.getLogger(__name__)

#: The write-advisory family a dismissal is recorded against.
FAMILY = "specific_relation_options"
#: A response lists at most this many options, whatever the number of edges it offers for.
MAX_OPTIONS = relation_options.MAX_OPTIONS
#: A bullet longer than this is not echoed back into a response.
MAX_BULLET_CHARS = 300
WHY = "Replace the generic relation with a specific one."


def review(source_ref: str, target_ref: str) -> tuple[str, str]:
    """`(review id, fingerprint)` of one page pair, for the write-advisory dismissal door.

    Both depend on the pair alone, in either order: the generic relation is symmetric, so a
    dismissal holds whichever page carries the edge.
    """
    from . import review_state

    target = f"{FAMILY}:{'|'.join(sorted((source_ref, target_ref)))}"
    return (
        review_state.item_id(target),
        review_state.fingerprint(
            target_ref=target, categories=[FAMILY], reasons=[], related_refs=[]
        ),
    )


@call_spans.timed("post_commit.specific_options")
def specific_options(
    vault_root: Path,
    offers: Sequence[relation_barrier.Offer],
    *,
    path: str,
    source: str,
) -> list[dict[str, Any]]:
    """The options a committed page `source` offers, at most `MAX_OPTIONS` across its edges.

    An edge is offered only when its bullet is one `## Relations` line that occurs once in the
    page, so the replacement touches that edge alone. A dismissed or snoozed pair, a quieted
    family and `structural_suggestions` set to `off` withhold their offers. Never raises: a
    fault here costs the caller an offer, never the write.
    """
    if not offers:
        return []
    try:
        return _specific_options(Path(vault_root), offers, path, source)
    except Exception:  # noqa: BLE001 -- an offer never breaks a commit
        log.debug("specific relation options failed (non-fatal)", exc_info=True)
        return []


def _specific_options(
    root: Path, offers: Sequence[relation_barrier.Offer], path: str, source: str
) -> list[dict[str, Any]]:
    from . import corpus_aware, envelope, review_state, vault

    if envelope.active().get("structural_suggestions") == "off":
        return []
    store = review_state.ReviewStateStore(root)
    payload = store.load()
    if review_state.disposition_for(FAMILY, payload=payload) != "normal":
        return []
    candidates = markdown_relations.scan_markdown_relations(source).candidates
    written = {(c.kind, c.target) for c in candidates if c.canonical and c.grammar_valid}
    expected_hash = vault.content_hash(source)
    options: list[dict[str, Any]] = []
    stamps: list[tuple[str, str, str]] = []
    for offer in offers:
        fact = offer.fact
        refs = (fact.logical_source_ref, fact.logical_target_ref)
        bullet = _bullet(candidates, source, fact.raw_relation, fact.raw_target)
        if bullet is None or not all(refs):
            continue
        review_id, fingerprint = review(*refs)
        # Only an open pair is offered; a dismissed or snoozed one stays quiet.
        if store.effective_state(review_id, fingerprint, payload=payload)[0] != "open":
            continue
        offered = 0
        for option in offer.options:
            key = option["key"]
            # A bullet with that predicate to that target already exists, so a swap would leave two.
            if len(options) == MAX_OPTIONS or (key, bullet.target) in written:
                continue
            options.append(
                {
                    "target": fact.logical_target_path,
                    "relation": key,
                    "description": option["description"],
                    "review": corpus_aware.write_advisory_ref(review_id),
                    "fingerprint": fingerprint,
                    "route": {
                        "tool": "edit_note",
                        "args": {
                            "path": path,
                            "why": WHY,
                            "operation": {
                                "kind": "replace_string",
                                "old_string": bullet.raw,
                                "new_string": _retyped(bullet, key),
                                "expected_hash": expected_hash,
                            },
                        },
                    },
                }
            )
            offered += 1
        if offered:
            stamps.append((review_id, fingerprint, FAMILY))
    # The stamp is what lets a dismissal through the write-advisory door name the family.
    if stamps:
        review_state.record_surfaced(root, stamps, surface="write", known=payload)
    return options


def _bullet(
    candidates: Sequence[markdown_relations.MarkdownRelationCandidate],
    source: str,
    raw_relation: str,
    raw_target: str,
) -> markdown_relations.MarkdownRelationCandidate | None:
    """The one `## Relations` bullet that authored this edge, or None when it cannot be told apart."""
    found = [
        candidate
        for candidate in candidates
        if candidate.canonical
        and candidate.grammar_valid
        and candidate.raw_kind == raw_relation
        and candidate.target == raw_target
    ]
    if len(found) != 1 or len(found[0].raw) > MAX_BULLET_CHARS or source.count(found[0].raw) != 1:
        return None
    return found[0]


def _retyped(bullet: markdown_relations.MarkdownRelationCandidate, key: str) -> str:
    """The bullet with its predicate swapped for `key`; marker, spacing and link stay as written."""
    marker = markdown_relations.bullet_marker_length(bullet.raw)
    return bullet.raw[:marker] + key + bullet.raw[marker + len(bullet.raw_kind or "") :]
