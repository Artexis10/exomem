"""Upkeep: the served side of the dreamer's proposals.

The dreamer (`dreamer.py`) detects bounded structural upkeep in the background
and keeps it in its disposable sidecar. This module is everything a request
may do with it: the `exomem://review/upkeep/<id>` namespace, the explicit
`review_memory(mode="upkeep")` list, one item's bounded revalidation, its
context excerpts, and its triage. Link items reuse the relation queue's refs,
so they are triaged and accepted through the relation namespace; their upkeep
ref names the same candidate for context.

Nothing here scans the vault. An item is revalidated from its own subject and
evidence pages only, never through the attention union or an audit, and every
count is taken after egress and triage filtering: a withheld page never
appears, not even as a number.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import dreamer_families, dreamer_store, review_state

UPKEEP_PREFIX = "exomem://review/upkeep/"
DEFAULT_REVIEW_LIMIT = 10
MAX_REVIEW_LIMIT = 50
_REVIEW_STATES = frozenset({"open", "all", "snoozed", "dismissed"})
_TRIAGE_ACTIONS = frozenset({"dismiss", "snooze", "reopen"})

#: Delivery order among families: continuity first, then facts about entities,
#: then connections, then naming, self-description and conventions.
FAMILY_ORDER: tuple[str, ...] = (
    "upkeep_fold",
    dreamer_families.HYDRATION_FAMILY,
    dreamer_families.LINK_FAMILY,
    "upkeep_alias",
    "upkeep_profile",
    "upkeep_convention",
)

#: Closed per-kind labels. UI strings, not matching lists.
LABELS: dict[str, str] = {
    dreamer_families.LINK_KIND: "Two notes could be connected",
    dreamer_families.HYDRATION_KIND: "Facts about an entity live on other pages",
}

_LINK_WHY: dict[str, str] = {
    "shared_sources": "both notes cite the same source and are not connected",
    "shared_open_question": "both notes carry the same open question and are not connected",
    "shared_resolution_target": "both notes answer the same target and are not connected",
    "unit_relation_lift": "this note's own units relate to the other note; the page does not",
}

#: Evidence entries shown per served item.
SHOWN_EVIDENCE = 3

#: Excerpt budgets for `review_item_context`.
CONTEXT_SUBJECT_CHARS = 4000
CONTEXT_EVIDENCE_CHARS = 600


def is_upkeep_ref(value: str) -> bool:
    return str(value or "").strip().startswith(UPKEEP_PREFIX)


def upkeep_ref(cid: str) -> str:
    return f"{UPKEEP_PREFIX}{cid}"


def parse_upkeep_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(UPKEEP_PREFIX):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: expected {UPKEEP_PREFIX}<id>")
    cid = raw[len(UPKEEP_PREFIX) :].lower()
    if len(cid) != 24 or any(char not in "0123456789abcdef" for char in cid):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: invalid upkeep reference {value!r}")
    return cid


def _not_found(ref: str) -> ValueError:
    return ValueError(f"REVIEW_ITEM_NOT_FOUND: no current upkeep item for {ref}")


def _changed(ref: str) -> ValueError:
    return ValueError(
        f"REVIEW_ITEM_CHANGED: the upkeep proposal changed; review {ref} again before acting"
    )


def _family_rank(family: str) -> int:
    return FAMILY_ORDER.index(family) if family in FAMILY_ORDER else len(FAMILY_ORDER)


def _keep(vault_root: Path):
    """This audience's release predicate; None on an ungoverned vault."""
    from .governance import egress

    return egress.release_walk_filter(Path(vault_root))


def _visible(keep, path: str) -> bool:
    return keep is None or bool(keep(path))


def _why(row: dict[str, Any], visible_origins: int) -> str:
    kind = str(row.get("kind") or "")
    if kind == dreamer_families.LINK_KIND:
        method = str((row.get("measures") or {}).get("method") or "")
        return _LINK_WHY.get(method, "the two notes share structure and are not connected")
    if kind == dreamer_families.HYDRATION_KIND:
        return (
            f"{visible_origins} independent sources added facts that link here "
            "after it was last updated"
        )
    return "a structural pattern suggests this change"


def serve(
    row: dict[str, Any],
    *,
    keep,
    state: str = "open",
    delivered_before: int = 0,
    disposition: str = "normal",
) -> dict[str, Any] | None:
    """One candidate as the wire item, or None when egress leaves too little.

    The subject must be released. Withheld evidence is dropped, and the item
    is withheld whole when the family's evidence minimum no longer holds on
    what remains: a link needs its other endpoint, hydration needs two
    independent origins.
    """
    subject = str(row.get("subject_path") or "")
    if not subject or not _visible(keep, subject):
        return None
    evidence = [
        item
        for item in (row.get("evidence") or [])
        if str(item.get("path") or "") and _visible(keep, str(item.get("path")))
    ]
    kind = str(row.get("kind") or "")
    others = [item for item in evidence if item.get("path") != subject]
    origins = {str(item.get("origin")) for item in others if item.get("origin")}
    if kind == dreamer_families.LINK_KIND:
        if not any(item.get("role") == "target" for item in others):
            return None
    elif kind == dreamer_families.HYDRATION_KIND:
        if len(origins) < dreamer_families.HYDRATION_MIN_ORIGINS:
            return None
    subject_entry = next((item for item in evidence if item.get("path") == subject), {})
    ref = str(row.get("ref") or upkeep_ref(str(row["id"])))
    route = _visible_route(row.get("route") or {}, keep)
    dispose_args: dict[str, Any] = {"ref": ref}
    if kind == dreamer_families.LINK_KIND:
        dispose_args["source_path"] = subject
    return {
        "ref": ref,
        "family": str(row.get("family") or ""),
        "fingerprint": str(row.get("fingerprint") or ""),
        "kind": kind,
        "label": LABELS.get(kind, "Upkeep"),
        "subject": {
            "ref": str(row.get("subject_ref") or subject_entry.get("ref") or ""),
            "title": subject_entry.get("title"),
        },
        "evidence": [
            {"ref": str(item.get("ref") or ""), "title": item.get("title")}
            for item in others[:SHOWN_EVIDENCE]
        ],
        "evidence_count": len(others),
        "why": _why(row, len(origins)),
        "disposition": {
            "state": state,
            "delivered_before": int(delivered_before),
            **({"family": disposition} if disposition != "normal" else {}),
        },
        "route": route,
        "context_route": {
            "tool": "review_item_context",
            "args": {"ref": upkeep_ref(str(row["id"]))},
        },
        "dispose": {
            "tool": "triage_memory",
            "actions": ["dismiss", "snooze"],
            "args": dispose_args,
        },
        "permission": dreamer_families.PERMISSION,
    }


def _visible_route(route: dict[str, Any], keep) -> dict[str, Any]:
    args = dict(route.get("args") or {})
    if isinstance(args.get("paths"), list):
        args["paths"] = [path for path in args["paths"] if _visible(keep, str(path))]
    return {"tool": route.get("tool"), "args": args}


def _payload(vault_root: Path) -> dict[str, Any] | None:
    try:
        return review_state.ReviewStateStore(vault_root).load()
    except ValueError:
        return None


def _decision_state(vault_root: Path, row: dict[str, Any], payload: dict[str, Any]) -> str:
    state, _decision = review_state.ReviewStateStore(vault_root).effective_state(
        str(row["id"]), str(row["fingerprint"]), payload=payload
    )
    return state


def _deliveries(view: dreamer_store.StoreView) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for cid, fingerprint, _caller, _at in view.deliveries:
        counts[(cid, fingerprint)] = counts.get((cid, fingerprint), 0) + 1
    return counts


def review(
    vault_root: Path,
    *,
    state: str = "open",
    categories: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """The explicit, bounded upkeep list. Counts are taken after egress and triage."""
    from . import dreamer
    from .governance import egress

    state = str(state or "open")
    if state not in _REVIEW_STATES:
        raise ValueError(f"INVALID_REVIEW_STATE: state must be one of {sorted(_REVIEW_STATES)}")
    bound = DEFAULT_REVIEW_LIMIT if limit is None else max(0, min(int(limit), MAX_REVIEW_LIMIT))
    wanted = set(categories or [])
    base: dict[str, Any] = {
        "mode": "upkeep",
        "mutated": False,
        "setting": dreamer.setting(),
        "items": [],
        "families": {},
        "evidence_complete": {},
        "integrity": {},
        "truncated": False,
    }
    view = dreamer_store.read_view(Path(vault_root))
    if view is None:
        return {**base, "status": "unavailable"}
    payload = _payload(Path(vault_root))
    if payload is None:
        return {**base, "status": "review_state_unavailable"}
    delivered = _deliveries(view)
    collected: list[tuple[tuple, dict[str, Any]]] = []
    families: dict[str, int] = {}
    integrity: dict[str, int] = {}
    with egress.disclosure_boundary(Path(vault_root), "upkeep_review"):
        keep = _keep(Path(vault_root))
        for row in view.candidates:
            if row.get("state") != "open":
                continue
            family = str(row.get("family") or "")
            if wanted and family not in wanted:
                continue
            disposition = review_state.disposition_for(family, payload=payload)
            if disposition == "off":
                continue
            decision = _decision_state(Path(vault_root), row, payload)
            served = serve(
                row,
                keep=keep,
                state=decision,
                delivered_before=delivered.get((str(row["id"]), str(row["fingerprint"])), 0),
                disposition=disposition,
            )
            if served is None:
                continue
            if decision == "open":
                families[family] = families.get(family, 0) + 1
            if state != "all" and decision != state:
                continue
            key = (
                _family_rank(family),
                float(row.get("settled_at") or row.get("refreshed_at") or 0.0),
                str(row["id"]),
            )
            collected.append((key, served))
        for category, paths in view.integrity:
            if all(_visible(keep, path) for path in paths):
                integrity[category] = integrity.get(category, 0) + 1
    collected.sort(key=lambda pair: pair[0])
    complete = view.health.get("evidence_complete") or {}
    return {
        **base,
        "status": "available",
        "items": [item for _key, item in collected[:bound]],
        "families": families,
        "evidence_complete": {
            family: bool(flag)
            for family, flag in complete.items()
            if not wanted or family in wanted
        },
        "integrity": integrity,
        "truncated": len(collected) > bound,
    }


def _row(vault_root: Path, cid: str) -> dict[str, Any] | None:
    view = dreamer_store.read_view(Path(vault_root))
    if view is None:
        return None
    return next(
        (row for row in view.candidates if row["id"] == cid and row.get("state") == "open"),
        None,
    )


def _current(vault_root: Path, ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The stored row and its revalidated current proposal, both under egress.

    Revalidation reads only the candidate's own subject and evidence pages (and
    bounded graph lookups) through the family's `propose`. Nothing is written:
    the worker records the refreshed row on its next pass.
    """
    cid = parse_upkeep_ref(ref)
    row = _row(Path(vault_root), cid)
    if row is None:
        raise _not_found(ref)
    ctx = dreamer_families.Context(
        vault_root=Path(vault_root), store=None, conn=None, now=time.time()
    )
    try:
        proposal = dreamer_families.propose(ctx, row)
    except dreamer_families.Deferred as exc:
        raise ValueError(
            "REVIEW_REFRESH_REQUIRED: the upkeep proposal cannot be revalidated right "
            f"now; try {ref} again shortly"
        ) from exc
    finally:
        ctx.close()
    if proposal is None:
        raise _not_found(ref)
    return row, {**row, **proposal}


def item(vault_root: Path, ref: str, *, expected_fingerprint: str | None = None) -> dict[str, Any]:
    """One upkeep item, revalidated, or REVIEW_ITEM_CHANGED / REVIEW_ITEM_NOT_FOUND."""
    from .governance import egress

    _row_stored, current = _current(Path(vault_root), ref)
    if expected_fingerprint and expected_fingerprint != current["fingerprint"]:
        raise _changed(ref)
    payload = _payload(Path(vault_root))
    if payload is None:
        raise ValueError("REVIEW_STATE_INVALID: the review state cannot be read")
    with egress.disclosure_boundary(Path(vault_root), "upkeep_item"):
        served = serve(
            current,
            keep=_keep(Path(vault_root)),
            state=_decision_state(Path(vault_root), current, payload),
            disposition=review_state.disposition_for(current["family"], payload=payload),
        )
    if served is None:
        raise _not_found(ref)
    return {"mode": "item", "mutated": False, "item": served, "upkeep_ref": ref}


def context(
    vault_root: Path,
    ref: str,
    *,
    expected_fingerprint: str | None = None,
    max_body_chars: int = CONTEXT_SUBJECT_CHARS,
    max_related_pages: int = 8,
) -> dict[str, Any]:
    """Bounded excerpts of one item's subject and evidence pages, under egress."""
    from . import get_page as get_page_module
    from .governance import egress
    from .review_context import _bounded

    vault_root = Path(vault_root)
    _stored, current = _current(vault_root, ref)
    if expected_fingerprint and expected_fingerprint != current["fingerprint"]:
        raise _changed(ref)
    subject_limit = max(0, min(int(max_body_chars), CONTEXT_SUBJECT_CHARS))
    related_limit = max(0, min(int(max_related_pages), dreamer_store.EVIDENCE_CAP))
    with egress.disclosure_boundary(vault_root, "upkeep_context"):
        keep = _keep(vault_root)
        served = serve(current, keep=keep)
        if served is None:
            raise _not_found(ref)

        def excerpt(path: str, limit: int) -> dict[str, Any]:
            try:
                page = get_page_module.get_page(vault_root, path=path)
            except Exception:  # noqa: BLE001 - a vanished page is reported, not fatal
                return {"missing": True}
            text, truncated = _bounded(page.body, limit)
            return {
                "title": getattr(page, "title", None),
                "content_hash": page.content_hash,
                "excerpt": text,
                "truncated": truncated,
            }

        subject_path = str(current["subject_path"])
        evidence = [
            item
            for item in current.get("evidence") or []
            if item.get("path") != subject_path and _visible(keep, str(item.get("path")))
        ][:related_limit]
        return {
            "mode": "upkeep",
            "mutated": False,
            "ref": ref,
            "fingerprint": current["fingerprint"],
            "expected_fingerprint": expected_fingerprint,
            "item": served,
            "subject": {
                "path": subject_path,
                "ref": served["subject"]["ref"],
                **excerpt(subject_path, subject_limit),
            },
            "evidence": [
                {
                    "path": str(item["path"]),
                    "ref": str(item.get("ref") or ""),
                    "role": item.get("role"),
                    **excerpt(str(item["path"]), CONTEXT_EVIDENCE_CHARS),
                }
                for item in evidence
            ],
            "route": served["route"],
            "permission": dreamer_families.PERMISSION,
        }


def triage(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Record a fingerprint-bound dismiss, snooze or reopen for one upkeep item."""
    from . import dreamer

    action = str(action or "").strip().lower()
    if action not in _TRIAGE_ACTIONS:
        raise ValueError(f"INVALID_REVIEW_ACTION: upkeep items accept {sorted(_TRIAGE_ACTIONS)}")
    _stored, current = _current(Path(vault_root), ref)
    if expected_fingerprint and expected_fingerprint != current["fingerprint"]:
        raise _changed(ref)
    result = review_state.ReviewStateStore(vault_root).apply(
        str(current["id"]),
        str(current["fingerprint"]),
        action=action,
        until=until,
        why=why,
        family=str(current["family"]),
    )
    dreamer.note_disposition(str(current["id"]), str(current["fingerprint"]))
    result["ref"] = ref
    result["family"] = current["family"]
    result["path"] = current["subject_path"]
    return result


def deliverable_rows(vault_root: Path, view: dreamer_store.StoreView) -> list[dict[str, Any]]:
    """The candidates a carrier may offer, before egress and ordering.

    The worker precomputed `deliverable`; this re-checks the two things that
    can move between its passes. A disposition made in this process stops
    delivery at once. A review-state file newer than the worker's precompute
    (another process decided something) is re-read once, and each candidate's
    decision is re-checked against it.
    """
    from . import dreamer

    token = dreamer_families.review_state_token(Path(vault_root))
    payload: dict[str, Any] | None = None
    loaded = False
    out: list[dict[str, Any]] = []
    for row in view.candidates:
        if row.get("state") != "open" or not row.get("deliverable"):
            continue
        cid, fingerprint = str(row["id"]), str(row["fingerprint"])
        if dreamer.disposed(cid, fingerprint):
            continue
        if row.get("deliverable_token") != token:
            if not loaded:
                payload = _payload(Path(vault_root))
                loaded = True
            if payload is None:
                return []
            if _decision_state(Path(vault_root), row, payload) != "open":
                continue
            if review_state.disposition_for(str(row["family"]), payload=payload) != "normal":
                continue
        out.append(row)
    return out
