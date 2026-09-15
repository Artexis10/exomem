"""Runtime seam for the context compiler: readiness, single-flight, caching.

Two jobs, both about not doing expensive work on a request thread.

**Index readiness.** A managed runtime never builds the activation index inline:
it schedules one single-flight background build and abstains with
`index_warming` for that request only, exactly as the entity registry warms for
the referents stage. An unmanaged runtime (a local CLI, a desk-side server) has
no background worker to wait for, so it builds inline within the request budget —
the honest trade for a single-user install.

**Packet caching.** Keyed on the recall freshness key, the index generation and
the role-registry hash, so a governed write that adds an alias invalidates the
packet that missed it. The cache holds the UNGUARDED packet and every request
re-runs the egress guard against its own principal: caching a guarded packet
would serve one audience's release decisions to the next, which is the mistake
design D2 exists to prevent on the recall path.

`purpose` is deliberately absent from the key. It may widen or narrow what an
audience sees, so a purpose-keyed cache would be a second, weaker copy of the
release plane.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import context_roles, working_set, working_set_index

log = logging.getLogger(__name__)

CACHE_SIZE = 16

READY = "ready"
#: The abstention reason a managed runtime returns while the derived index is
#: still cold. Spelled `index_warming` in the packet because a reader needs to
#: know WHICH thing is warming; "warming" alone reads as the whole server.
WARMING = "index_warming"
UNAVAILABLE = "unavailable"
DISABLED = "disabled"

_CACHE_LOCK = threading.Lock()
_PACKET_CACHE: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
_BUILDS: set[Path] = set()


def cache_key(
    *,
    freshness_key: Any,
    index_generation: int,
    roles_hash: str,
    turn: str,
    budget_chars: int,
) -> tuple:
    """The packet identity. `purpose` is deliberately not a parameter (design D7)."""
    return (
        tuple(freshness_key) if isinstance(freshness_key, (list, tuple)) else str(freshness_key),
        int(index_generation),
        str(roles_hash),
        str(turn),
        int(budget_chars),
    )


def reset_caches_for_tests() -> None:
    """Drop the packet cache and any in-flight build marker."""
    with _CACHE_LOCK:
        _PACKET_CACHE.clear()
        _BUILDS.clear()


def ensure_index(
    vault_root: Path, *, freshness_stamp: str = ""
) -> tuple[str, working_set_index.WorkingSetIndex | None, bool]:
    """Return a usable, current index — or say why there is not one yet.

    The third element is `index_stale`: True when the catalogue is behind the
    request's freshness key and this runtime could not bring it forward inside
    the request. The packet then SAYS so rather than implying it was built from
    the current vault state.
    """
    if working_set_index.disabled():
        return DISABLED, None, False
    root = Path(vault_root)
    index = working_set_index.WorkingSetIndex(root)
    if not index.available():
        return DISABLED, None, False
    if not index.anchors():
        if _managed():
            _schedule_build(root)
            return WARMING, None, False
        try:
            index.rebuild(freshness_stamp=freshness_stamp)
        except Exception:  # noqa: BLE001 - a failed build abstains, never breaks a read
            log.warning("activation index inline build failed", exc_info=True)
            return UNAVAILABLE, None, False
        return READY, index, False
    if freshness_stamp and index.freshness_stamp() != freshness_stamp:
        refreshed = refresh_index(index, freshness_stamp=freshness_stamp)
        return READY, index, not refreshed
    return READY, index, False


def refresh_index(
    index: working_set_index.WorkingSetIndex, *, freshness_stamp: str = ""
) -> bool:
    """Bring a stale index up to the current vault state; False when it could not.

    A managed runtime hands the walk to the background thread, so the request
    thread reports `index_stale` rather than paying for work it did not budget
    for. An unmanaged runtime has no background worker to wait for and does the
    update inline — the honest trade for a single-user install.
    """
    if _managed():
        _schedule_build(index.vault_root)
        return False
    try:
        index.update(freshness_stamp=freshness_stamp or None)
    except Exception:  # noqa: BLE001 - staleness is reported, never raised
        log.debug("activation index refresh failed", exc_info=True)
        return False
    return True


def _managed() -> bool:
    try:
        from . import readiness

        return bool(readiness.runtime_managed())
    except Exception:  # noqa: BLE001 - an unknown runtime is treated as unmanaged
        return False


def _schedule_build(vault_root: Path) -> None:
    """Single-flight a cold or stale build away from the request thread."""
    root = Path(vault_root).absolute()
    with _CACHE_LOCK:
        if root in _BUILDS:
            return
        _BUILDS.add(root)

    def _warm() -> None:
        try:
            working_set_index.WorkingSetIndex(root).update()
        except Exception:  # noqa: BLE001 - the optional stage stays soft-failing
            log.warning("activation index background build failed", exc_info=True)
        finally:
            with _CACHE_LOCK:
                _BUILDS.discard(root)

    thread = threading.Thread(target=_warm, name="exomem-working-set-warm", daemon=True)
    try:
        thread.start()
    except Exception:  # noqa: BLE001 - a thread-start failure cannot fail a read
        with _CACHE_LOCK:
            _BUILDS.discard(root)
        log.warning("activation index background build could not start", exc_info=True)


def serve(
    vault_root: Path,
    *,
    turn: str,
    budget_chars: int,
    purpose: str | None = None,
    timings: Any = None,
    retrieval_paths: frozenset[str] = frozenset(),
    freshness_key: Any = "",
    index_stale: bool = False,
) -> dict[str, Any]:
    """Compile (or reuse) one unguarded packet. Never raises: it abstains instead."""
    root = Path(vault_root)
    limit = working_set.clamp_budget(budget_chars)
    registry = context_roles.load_roles(root)
    stamp = _key_text(freshness_key)
    with working_set._span(timings, "working_set.index"):
        state, index, stale = ensure_index(root, freshness_stamp=stamp)
    index_stale = index_stale or stale
    if state != READY or index is None:
        return working_set.abstained_packet(
            reason=state,
            budget_chars=limit,
            generation={
                "freshness_key": _key_text(freshness_key),
                "index_generation": 0,
                **registry.generation_block(),
            },
        )

    key = cache_key(
        freshness_key=freshness_key,
        index_generation=index.generation(),
        roles_hash=registry.roles_hash,
        turn=turn,
        budget_chars=limit,
    )
    identity = (str(root.absolute()), key)
    with _CACHE_LOCK:
        cached = _PACKET_CACHE.get(identity)
        if cached is not None:
            _PACKET_CACHE.move_to_end(identity)
    if cached is not None:
        return copy.deepcopy(cached)

    try:
        packet = working_set.compile_packet(
            root,
            turn=turn,
            budget_chars=limit,
            purpose=purpose,
            timings=timings,
            retrieval_paths=retrieval_paths,
            index=index,
            freshness_key=_key_text(freshness_key),
        )
    except Exception:  # noqa: BLE001 - the whole operation is additive and abstains
        log.warning("activation compilation failed; abstaining", exc_info=True)
        return working_set.abstained_packet(
            reason="unavailable",
            budget_chars=limit,
            generation={
                "freshness_key": _key_text(freshness_key),
                "index_generation": index.generation(),
                **registry.generation_block(),
            },
        )
    if index_stale:
        packet["generation"]["index_stale"] = True
    with _CACHE_LOCK:
        _PACKET_CACHE[identity] = copy.deepcopy(packet)
        _PACKET_CACHE.move_to_end(identity)
        while len(_PACKET_CACHE) > CACHE_SIZE:
            _PACKET_CACHE.popitem(last=False)
    return packet


def _key_text(freshness_key: Any) -> str:
    if isinstance(freshness_key, (list, tuple)):
        return ":".join(str(part) for part in freshness_key)
    return str(freshness_key or "")
