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

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import dreamer_families, dreamer_store, review_state

log = logging.getLogger(__name__)

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
    # Visibility before anything else, and on every path: a withheld item is
    # answered exactly like an id that does not exist, before any revalidation,
    # fingerprint comparison or write. The release filter is built for an
    # absent id too, so the two take the same work.
    keep = _keep(Path(vault_root))
    row = _row(Path(vault_root), cid)
    if row is None or serve(row, keep=keep) is None:
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


# ----------------------------------------------------------------------
# the activation carrier
# ----------------------------------------------------------------------

#: No two deliveries on one vault within this, whatever their callers: one
#: person is often two callers (a hook door and a connector).
VAULT_SPACING_SECONDS = 600
#: At most this many items per delivery key per UTC day.
PER_KEY_DAILY_CAP = 3
#: A second delivery of one `(id, fingerprint)` waits this long after the first
#: and goes to another caller; after it the item is held (the worker's
#: `dreamer_families.MAX_DELIVERIES`).
SECOND_DELIVERY_AFTER_SECONDS = 7 * 86400
#: An item is offered whole or not at all; it never exceeds this many characters.
MAX_ITEM_CHARS = 400
#: Candidates tried per session start before the carrier gives up.
MAX_TRIED = 4
#: Keys remembered for the session-start rule; the oldest is forgotten first.
LEDGER_CAP = 512
#: In-process deliveries kept for the held check until the worker records them.
PENDING_CAP = 256

_monotonic = time.monotonic
_wall = time.time

_DELIVERY_LOCK = threading.Lock()
_LAST_SEEN: OrderedDict[tuple[str, ...], float] = OrderedDict()
_VAULT_LAST: dict[str, float] = {}
_KEY_DAY: dict[tuple[str, ...], tuple[str, int]] = {}
_PENDING: list[tuple[str, str, str, float]] = []


def reset_delivery_state() -> None:
    with _DELIVERY_LOCK:
        _LAST_SEEN.clear()
        _VAULT_LAST.clear()
        _KEY_DAY.clear()
        _PENDING.clear()


def delivery_key(vault_root: Path, session: str | None) -> tuple[str, ...] | None:
    """Who is asking, for the session-start rule, or None when nobody can be keyed.

    U6's `session` when the caller sent a valid one (the hook door), else the
    capture sweep's ledger key: the process for stdio, CLI and REST, the stable
    principal or bearer scope over HTTP, and None for stateless HTTP. The raw
    session is never kept, only a hash of it.
    """
    from . import capture_sweep, query_log

    vault = str(vault_root)
    if isinstance(session, str) and 0 < len(session) <= query_log.SESSION_MAX_CHARS:
        digest = hashlib.sha256(f"{vault}\0{session}".encode("utf-8", "surrogatepass")).hexdigest()[
            :24
        ]
        return ("session", digest, vault)
    key = capture_sweep.ledger_key(Path(vault_root))
    return tuple(str(part) for part in key) if key is not None else None


def _caller_hash(key: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(key).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _session_start(key: tuple[str, ...], now: float) -> bool:
    """Note this activation; True when it opens a session for `key`."""
    from . import capture_sweep

    with _DELIVERY_LOCK:
        last = _LAST_SEEN.pop(key, None)
        _LAST_SEEN[key] = now
        while len(_LAST_SEEN) > LEDGER_CAP:
            _LAST_SEEN.popitem(last=False)
    return last is None or now - last >= capture_sweep.QUIET_SECONDS


def _utc_day(wall: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(wall))


def _caps_allow(key: tuple[str, ...], vault: str, now: float, wall: float) -> bool:
    with _DELIVERY_LOCK:
        last = _VAULT_LAST.get(vault)
        if last is not None and now - last < VAULT_SPACING_SECONDS:
            return False
        day, count = _KEY_DAY.get(key, ("", 0))
        return not (day == _utc_day(wall) and count >= PER_KEY_DAILY_CAP)


def _note_delivery(
    key: tuple[str, ...], vault: str, row: dict[str, Any], now: float, wall: float
) -> None:
    today = _utc_day(wall)
    with _DELIVERY_LOCK:
        _VAULT_LAST[vault] = now
        day, count = _KEY_DAY.get(key, ("", 0))
        _KEY_DAY[key] = (today, count + 1 if day == today else 1)
        if len(_KEY_DAY) > LEDGER_CAP:
            _KEY_DAY.pop(next(iter(_KEY_DAY)))
        _PENDING.append((str(row["id"]), str(row["fingerprint"]), _caller_hash(key), wall))
        del _PENDING[:-PENDING_CAP]


def pending_deliveries() -> list[tuple[str, str, str, float]]:
    """In-process deliveries the worker has not recorded yet (a copy)."""
    with _DELIVERY_LOCK:
        return list(_PENDING)


def forget_deliveries(recorded: list[tuple[str, str, str, float]]) -> None:
    """Drop deliveries the worker has now written to the sidecar."""
    done = set(recorded)
    with _DELIVERY_LOCK:
        _PENDING[:] = [entry for entry in _PENDING if entry not in done]


def item_text(item: dict[str, Any]) -> str:
    """The item's prose as the packet budget counts it."""
    subject = (item.get("subject") or {}).get("title") or ""
    return (
        f"{item.get('label') or ''}: {subject} — {item.get('why') or ''} [{item.get('ref') or ''}]"
    )


def _packet_paths(packet: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for section in ("recent_context", "anchors"):
        for entry in packet.get(section) or ():
            if isinstance(entry, dict) and entry.get("path"):
                path = str(entry["path"])
                paths.add(path if path.endswith(".md") else path + ".md")
    return paths


def _row_paths(row: dict[str, Any]) -> set[str]:
    return {str(item.get("path")) for item in row.get("evidence") or () if item.get("path")} | {
        str(row.get("subject_path") or "")
    }


def _signatures_live(vault_root: Path, row: dict[str, Any]) -> bool:
    """Every stored evidence signature still equals the live one."""
    from . import dreamer_delta, freshness

    evidence = [item for item in row.get("evidence") or () if item.get("path")]
    live = freshness.live_signatures(
        vault_root, dreamer_delta.SCOPE, [Path(vault_root) / str(item["path"]) for item in evidence]
    )
    if live is None:
        return False
    return all(
        signature is not None and dreamer_store.encode_sig(signature) == item.get("sig")
        for item, signature in zip(evidence, live, strict=True)
    )


def _earlier(view: dreamer_store.StoreView, row: dict[str, Any]) -> list[tuple[str, float]]:
    """Earlier deliveries of this `(id, fingerprint)`: stored plus in-process."""
    cid, fingerprint = str(row["id"]), str(row["fingerprint"])
    seen = [
        (caller, at)
        for rid, fp, caller, at in (*view.deliveries, *pending_deliveries())
        if rid == cid and fp == fingerprint
    ]
    return sorted(set(seen), key=lambda pair: pair[1])


def for_packet(vault_root: Path, packet: dict[str, Any], *, session: str | None = None) -> None:
    """Attach at most one upkeep item to a session-start packet. Never raises.

    Live only in the process that runs the worker (the managed service), which
    is where deliveries are remembered and then recorded. Off, nothing is read
    and nothing is attached. Any error attaches nothing.
    """
    from . import dreamer

    try:
        if not dreamer.delivering():
            return
        _attach(Path(vault_root), packet, session=session, dreamer=dreamer)
    except Exception:  # noqa: BLE001 - upkeep never breaks an activation
        log.debug("upkeep carrier failed (non-fatal)", exc_info=True)
        packet.pop("upkeep", None)


def _attach(vault_root: Path, packet: dict[str, Any], *, session: str | None, dreamer) -> None:
    from . import envelope, working_set
    from .governance import egress

    if (packet.get("abstention") or {}).get("reason") == "disabled":
        return
    key = delivery_key(vault_root, session)
    if key is None:
        return
    now, wall = _monotonic(), _wall()
    if not _session_start(key, now):
        return
    if envelope.active().get("structural_suggestions") == "off":
        return
    block: dict[str, Any] = {}
    failure = dreamer.failure()
    if failure is not None:
        block = {"status": "failed", "since": failure.get("since")}
    vault = str(vault_root)
    if not working_set.budget_exhausted("working_set.upkeep") and _caps_allow(
        key, vault, now, wall
    ):
        chosen = _choose(vault_root, packet, key, wall, egress=egress)
        if chosen is not None:
            row, item = chosen
            block["items"] = [item]
            budget = packet.setdefault("budget", {})
            budget["used_chars"] = int(budget.get("used_chars") or 0) + len(item_text(item))
            _note_delivery(key, vault, row, now, wall)
    if block:
        packet["upkeep"] = block


def _choose(
    vault_root: Path, packet: dict[str, Any], key: tuple[str, ...], wall: float, *, egress
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    view = dreamer_store.read_view(vault_root)
    if view is None:
        return None
    rows = deliverable_rows(vault_root, view)
    if not rows:
        return None
    recent = _packet_paths(packet)
    rows.sort(
        key=lambda row: (
            0 if recent & _row_paths(row) else 1,
            _family_rank(str(row.get("family") or "")),
            float(row.get("settled_at") or 0.0),
            str(row["id"]),
        )
    )
    budget = packet.get("budget") or {}
    room = int(budget.get("limit_chars") or 0) - int(budget.get("used_chars") or 0)
    caller = _caller_hash(key)
    with egress.disclosure_boundary(vault_root, "upkeep_advisory"):
        keep = _keep(vault_root)
        for row in rows[:MAX_TRIED]:
            earlier = _earlier(view, row)
            if len(earlier) >= dreamer_families.MAX_DELIVERIES:
                continue
            if earlier:
                first_caller, first_at = earlier[0]
                if first_caller == caller or wall - first_at < SECOND_DELIVERY_AFTER_SECONDS:
                    continue
            if not _signatures_live(vault_root, row):
                continue
            item = serve(row, keep=keep, delivered_before=len(earlier))
            if item is None:
                continue
            text = item_text(item)
            if len(text) > MAX_ITEM_CHARS or len(text) > room:
                continue
            return row, item
    return None
