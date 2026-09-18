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

**Continuity.** The token is this module's third job, and it is deliberately not
state: base64 of a compact JSON naming the sidecar's own identity, the
role-registry hash, the index generation, the served anchor refs and the roles.
The server trusts nothing in it beyond matching its own identity and roles hash,
re-validates every ref against the current index, and only ever QUALIFIES an
anchor the current turn already reached. It is minted from the packet as served —
after the egress guard — so it can never carry a ref that guard removed.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import context_roles, sidecar_store, working_set, working_set_index

log = logging.getLogger(__name__)

CACHE_SIZE = 16

#: The token's wire version. A payload of any other version is not decoded at
#: all: a token is a hint, and guessing at an unknown shape is worse than
#: starting the sequence again.
CONTINUITY_VERSION = 1

#: What `generation.continuity` reports. `absent` is the ordinary first state of
#: every session, not a failure.
CONTINUITY_APPLIED = "applied"
CONTINUITY_STALE = "stale"
CONTINUITY_ABSENT = "absent"

#: Ceilings on an inbound token, all three of them cheap refusals rather than
#: expensive parses. The token is a string a CALLER chose: it is not evidence
#: that this server ever minted one, so every dimension it can grow in has to be
#: bounded before any work is spent on it. The length bound matches the hook's
#: own (`_ACTIVATION_TOKEN_MAX_CHARS`) and is checked before the base64 decode,
#: so a megabytes-long argument costs a comparison instead of a megabytes-long
#: allocation on the request thread. The ref and role ceilings sit far above
#: anything a real packet produces — `working_set.MAX_ANCHORS` is 6 — so they
#: refuse the absurd without ever refusing the legitimate.
CONTINUITY_MAX_CHARS = 8192
CONTINUITY_MAX_REFS = 32
CONTINUITY_MAX_ROLES = 32

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
#: One inline-build lock per vault root. The managed path single-flights through
#: `_BUILDS` and a background thread; the unmanaged path has no thread to join,
#: so two concurrent cold reads would otherwise both walk the vault and the
#: loser's write would find the rows already there.
_INLINE_LOCKS: dict[Path, threading.Lock] = {}


def _inline_lock(root: Path) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _INLINE_LOCKS.get(root)
        if lock is None:
            lock = threading.Lock()
            _INLINE_LOCKS[root] = lock
        return lock


def retrieval_digest(retrieval_paths: frozenset[str] | set[str] | None) -> str:
    """A stable digest of the retrieval refs the release plane admitted."""
    if not retrieval_paths:
        return "none"
    joined = "\n".join(sorted(str(path) for path in retrieval_paths))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def continuity_digest(token: str | None) -> str:
    """A stable digest of a continuity token, for the packet cache key."""
    text = str(token or "").strip()
    if not text:
        return "none"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cache_key(
    *,
    freshness_key: Any,
    index_generation: int,
    roles_hash: str,
    turn: str,
    max_chars: int,
    retrieval_paths: frozenset[str] | set[str] | None = None,
    continuity: str | None = None,
    anchor: str | None = None,
) -> tuple:
    """The packet identity.

    `retrieval_paths` is part of it because `retrieval` is a CONTACT evidence
    kind — it can resolve an anchor on its own half of the two-kinds rule — and
    the release plane filters those refs per audience. Keying without them served
    one audience's compiled packet to another, which is the same class of defect
    the recall cache avoids by computing decisions after `find()` returns.

    `continuity` and `anchor` are part of it for the same reason one step on
    (design D8): both change which anchors resolve and at what status, so a
    request carrying either must never be handed a packet compiled without it,
    and two different overrides of one turn must not collide. The token enters as
    a digest rather than whole, so a long-lived session cannot grow the key.

    `purpose` is deliberately not a parameter: it may widen or narrow what an
    audience sees, so a purpose-keyed cache would be a second, weaker copy of
    the release plane (design D7).
    """
    return (
        tuple(freshness_key) if isinstance(freshness_key, (list, tuple)) else str(freshness_key),
        int(index_generation),
        str(roles_hash),
        str(turn),
        int(max_chars),
        retrieval_digest(retrieval_paths),
        continuity_digest(continuity),
        str(anchor or ""),
    )


# --------------------------------------------------------------------------- #
# Continuity: index identity, the codec, minting and validation
# --------------------------------------------------------------------------- #


def index_identity_from_token(token: Any) -> str:
    """`(epoch, generation, instance)` -> the sidecar's identity, or `""`.

    The generation is deliberately left out: it moves on every vault write, and
    an identity that changed on every capture would discard continuity before it
    was ever used. `instance` is the random value stamped once when the sidecar's
    meta table was created, so it differs per vault and per rebuilt sidecar and
    survives a process restart because it lives in sqlite.

    `instance == 0` is a legacy sidecar with no stamp — a value every such vault
    would share. An identity that cannot tell two vaults apart must not be used
    to accept a token from either, so it reports no identity at all and every
    token then reads as stale. Fail-closed is right here: this is an identity
    check, not an eventually-consistent lookup.
    """
    try:
        epoch, _generation, instance = (int(part) for part in token)
    except (TypeError, ValueError):
        return ""
    if not instance:
        return ""
    return f"{epoch}:{instance}"


def index_identity(index: working_set_index.WorkingSetIndex) -> str:
    """The identity of an already-open index."""
    try:
        return index_identity_from_token(index.token())
    except Exception:  # noqa: BLE001 - no identity means no continuity, never a failure
        log.debug("activation index identity unavailable", exc_info=True)
        return ""


def identity_for(vault_root: Path) -> str:
    """The identity of a vault's activation sidecar, read without preparing it.

    Deliberately not `WorkingSetIndex(root).token()`: that opens the index and
    runs the schema preparation, and the caller that mints a token has already
    paid for that once this request. This is one read-only query against a file
    that must already exist.
    """
    if working_set_index.disabled():
        return ""
    path = working_set_index.sidecar_path(Path(vault_root))
    if not path.exists():
        return ""
    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error:
        return ""
    try:
        return index_identity_from_token(sidecar_store.read_meta_token(conn))
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def encode_continuity(
    *,
    identity: str,
    roles_hash: str,
    generation: int,
    refs: Iterable[str],
    roles: Iterable[str],
) -> str:
    """Base64 of the compact JSON payload. No secret, and no signature.

    There is nothing to sign: every field is re-checked against the serving
    state, and a forged token can at most name refs the current turn already
    reached by a contact kind.
    """
    payload = {
        "v": CONTINUITY_VERSION,
        "identity": str(identity),
        "roles_hash": str(roles_hash),
        "generation": int(generation),
        "refs": sorted({str(ref) for ref in refs if str(ref)}),
        # RESERVED. The delta requires the selected roles in the token and they
        # are carried and validated, but nothing reads them back yet: continuity
        # qualifies anchors, and the roles a turn fills are re-selected from that
        # turn's own cues every time. They are here so a later tier can ask what
        # the previous turn actually looked at without a token format change —
        # deliberately kept, not dead weight to be tidied away.
        "roles": [str(role) for role in roles if str(role)],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    # `surrogatepass`, symmetrically with `decode_continuity`. A vault path reaches
    # Python through filesystem decoding, so a filename with invalid UTF-8 arrives
    # as a lone surrogate and strict encoding raises on it — turning one awkward
    # filename in the packet into a failed mint. The hooks' own digests already
    # take this posture. Encoding with it and decoding without would be worse than
    # either: tokens minted and then called stale, continuity lost undiagnosed.
    return (
        base64.urlsafe_b64encode(raw.encode("utf-8", "surrogatepass"))
        .decode("ascii")
        .rstrip("=")
    )


def decode_continuity(token: str | None) -> dict[str, Any] | None:
    """The token's fields, or `None` when it is not a token this server wrote.

    Every refusal is `None`, which the caller reports as `stale`. Nothing in here
    may raise, because the argument is attacker-chosen and the delta says an
    undecodable token is IGNORED — a token that could fail the operation instead
    would hand a stranger a switch for turning activation off.

    The exception list is wider than it looks like it needs to be, and each entry
    is a shape that got through the obvious one:

    * `ArithmeticError` — `"generation": 1e400` parses as `inf`, and `int(inf)`
      raises OverflowError, which is not a ValueError.
    * `RecursionError` — JSON nested past the interpreter's stack limit raises it
      out of `json.loads`, and it is a RuntimeError, not a ValueError.
    """
    text = str(token or "").strip()
    if not text or len(text) > CONTINUITY_MAX_CHARS:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        payload = json.loads(raw.decode("utf-8", "surrogatepass"))
    except (
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
        ArithmeticError,
        RecursionError,
    ):
        return None
    if not isinstance(payload, dict) or payload.get("v") != CONTINUITY_VERSION:
        return None
    identity = payload.get("identity")
    roles_hash = payload.get("roles_hash")
    refs = payload.get("refs")
    roles = payload.get("roles")
    if not isinstance(identity, str) or not isinstance(roles_hash, str):
        return None
    if not isinstance(refs, list) or not isinstance(roles, list):
        return None
    if len(refs) > CONTINUITY_MAX_REFS or len(roles) > CONTINUITY_MAX_ROLES:
        return None
    try:
        generation = int(payload.get("generation") or 0)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return {
        "identity": identity,
        "roles_hash": roles_hash,
        "generation": generation,
        "refs": [ref for ref in refs if isinstance(ref, str) and ref],
        "roles": [role for role in roles if isinstance(role, str) and role],
    }


def read_continuity(
    token: str | None, *, identity: str, roles_hash: str
) -> tuple[frozenset[str], str]:
    """`(refs to qualify, reported state)` for one inbound token.

    Every generation of the same index is accepted: the generation moves on every
    vault write, so a token that died on each capture would never be used. The
    refs are re-validated later, by matching against the candidates this turn
    actually produced — a ref the vault has since retired matches nothing and is
    dropped without a word.
    """
    if not str(token or "").strip():
        return frozenset(), CONTINUITY_ABSENT
    try:
        payload = decode_continuity(token)
    except Exception:  # noqa: BLE001 - a caller's string must not fail the request
        # Belt to the codec's braces. `decode_continuity` enumerates the shapes
        # that are known to escape a ValueError guard; this catches the next one
        # nobody has thought of yet, because the cost of getting it wrong is a
        # 500 on an argument a stranger controls, and the correct answer to any
        # unreadable token is the same single word.
        log.debug("continuity token could not be read; ignoring", exc_info=True)
        return frozenset(), CONTINUITY_STALE
    if payload is None:
        return frozenset(), CONTINUITY_STALE
    if not identity or payload["identity"] != identity:
        return frozenset(), CONTINUITY_STALE
    if payload["roles_hash"] != str(roles_hash):
        return frozenset(), CONTINUITY_STALE
    return frozenset(payload["refs"]), CONTINUITY_APPLIED


def mint_continuity(packet: Mapping[str, Any], *, identity: str) -> str:
    """The token for a packet AS SERVED, or `""` when there is nothing to carry.

    The caller passes the packet the guard returned, which is the whole point:
    an anchor the guard removed is not in `anchors[]`, so it cannot reach the
    token, and the next turn cannot be handed back a ref this audience may not
    see as if the server had found it.
    """
    if not identity or packet.get("abstained"):
        return ""
    anchors = [item for item in packet.get("anchors") or () if isinstance(item, Mapping)]
    refs = [str(item.get("ref") or "") for item in anchors]
    refs = [ref for ref in refs if ref]
    if not refs:
        return ""
    generation = packet.get("generation")
    generation = generation if isinstance(generation, Mapping) else {}
    roles = [
        str(role.get("id") or "")
        for role in packet.get("roles") or ()
        if isinstance(role, Mapping)
    ]
    # The mint cannot raise. It runs at the very end of a read that has already
    # succeeded and crossed the egress guard, so anything that fails here must
    # cost the turn its token and nothing else: a packet the caller has earned
    # must not be lost to the encoding of a ref.
    try:
        return encode_continuity(
            identity=identity,
            roles_hash=str(generation.get("roles_hash") or ""),
            generation=int(generation.get("index_generation") or 0),
            refs=refs,
            roles=[role for role in roles if role],
        )
    except Exception:  # noqa: BLE001 - a token is an optimisation, never a promise
        log.debug("continuity token could not be minted; serving without", exc_info=True)
        return ""


def reset_caches_for_tests() -> None:
    """Drop the packet cache and any in-flight build marker."""
    with _CACHE_LOCK:
        _PACKET_CACHE.clear()
        _BUILDS.clear()
        _INLINE_LOCKS.clear()


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
        with _inline_lock(root.absolute()):
            # Re-check under the lock: the thread that held it may have built the
            # catalogue while this one waited, and rebuilding on top of that would
            # pay for the walk twice and bump the generation for nothing.
            if not index.anchors():
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
    max_chars: int,
    purpose: str | None = None,
    timings: Any = None,
    retrieval_paths: frozenset[str] = frozenset(),
    freshness_key: Any = "",
    index_stale: bool = False,
    continuity: str | None = None,
    anchor: str | None = None,
) -> dict[str, Any]:
    """Compile (or reuse) one unguarded packet. Never raises: it abstains instead."""
    root = Path(vault_root)
    limit = working_set.clamp_budget(max_chars)
    registry = context_roles.load_roles(root)
    stamp = _key_text(freshness_key)
    with working_set._span(timings, "working_set.index"):
        state, index, stale = ensure_index(root, freshness_stamp=stamp)
    index_stale = index_stale or stale
    if state != READY or index is None:
        return working_set.abstained_packet(
            reason=state,
            max_chars=limit,
            generation={
                "freshness_key": _key_text(freshness_key),
                "index_generation": 0,
                "continuity": unevaluated_continuity(continuity),
                **registry.generation_block(),
            },
        )

    # Outside the try that wraps `compile_packet`, so it gets an abstain-never-raise
    # shape of its own: `serve` promises never to raise, and everything the token
    # touches — the identity read, the codec — is either sqlite or a caller's
    # string. An unreadable token degrades to `stale`, which is what the delta
    # says it must do.
    try:
        continuity_refs, continuity_state = read_continuity(
            continuity,
            identity=index_identity(index),
            roles_hash=registry.roles_hash,
        )
    except Exception:  # noqa: BLE001 - the whole operation is additive and abstains
        log.warning("continuity evaluation failed; ignoring the token", exc_info=True)
        continuity_refs, continuity_state = frozenset(), CONTINUITY_STALE
    key = cache_key(
        freshness_key=freshness_key,
        index_generation=index.generation(),
        roles_hash=registry.roles_hash,
        turn=turn,
        max_chars=limit,
        retrieval_paths=retrieval_paths,
        continuity=continuity,
        anchor=anchor,
    )
    cache_identity = (str(root.absolute()), key)
    with _CACHE_LOCK:
        cached = _PACKET_CACHE.get(cache_identity)
        if cached is not None:
            _PACKET_CACHE.move_to_end(cache_identity)
    if cached is not None:
        return copy.deepcopy(cached)

    try:
        packet = working_set.compile_packet(
            root,
            turn=turn,
            max_chars=limit,
            purpose=purpose,
            timings=timings,
            retrieval_paths=retrieval_paths,
            index=index,
            freshness_key=_key_text(freshness_key),
            continuity_refs=continuity_refs,
            anchor=anchor,
        )
    except Exception:  # noqa: BLE001 - the whole operation is additive and abstains
        log.warning("activation compilation failed; abstaining", exc_info=True)
        return working_set.abstained_packet(
            reason="unavailable",
            max_chars=limit,
            generation={
                "freshness_key": _key_text(freshness_key),
                "index_generation": index.generation(),
                # The token WAS evaluated on this path, so its real state is
                # reported rather than the pre-index placeholder.
                "continuity": continuity_state,
                **registry.generation_block(),
            },
        )
    packet["generation"]["continuity"] = continuity_state
    if index_stale:
        packet["generation"]["index_stale"] = True
    with _CACHE_LOCK:
        _PACKET_CACHE[cache_identity] = copy.deepcopy(packet)
        _PACKET_CACHE.move_to_end(cache_identity)
        while len(_PACKET_CACHE) > CACHE_SIZE:
            _PACKET_CACHE.popitem(last=False)
    return packet


def _key_text(freshness_key: Any) -> str:
    if isinstance(freshness_key, (list, tuple)):
        return ":".join(str(part) for part in freshness_key)
    return str(freshness_key or "")


def unevaluated_continuity(continuity: str | None) -> str:
    """The state for a packet that abstained before the token could be checked.

    A token passed to a request that never reached an index was ignored, and
    `stale` is what the contract calls an ignored token. Claiming `applied` would
    be a lie and claiming `absent` would hide that the caller sent one.
    """
    return CONTINUITY_ABSENT if not str(continuity or "").strip() else CONTINUITY_STALE
