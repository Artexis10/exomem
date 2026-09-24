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
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import context_roles, sidecar_store, working_set, working_set_index, working_set_resolve

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
    continuity_refs: frozenset[str] | set[str] = frozenset(),
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
    `continuity_refs` are the token's refs THIS audience may see
    (`visible_continuity_refs`), for the reason `retrieval_paths` is here: one
    token read by two audiences is two packets.

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
        tuple(sorted(str(ref) for ref in continuity_refs)),
        str(anchor or ""),
        bool(os.environ.get("EXOMEM_DISABLE_EMBEDDINGS")),
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
    minted_ns: int | None = None,
) -> str:
    """Base64 of the compact JSON payload. No secret, and no signature.

    There is nothing to sign: every field is re-checked against the serving
    state, and a forged token can at most name refs the current turn already
    reached by a contact kind — or, on a turn that only points back, refs the
    vault still holds as anchors, which is what the turn asked for.

    `minted_ns` is when the packet was served, wall clock. It is what lets a
    later referential turn tell a token that is still the latest thing that
    happened from one the user has since moved on from (`working_set.
    hot_profile`). Omitted, the token reads as it did before the field
    existed, and its refs lead the profile unconditionally.
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
    if minted_ns is not None:
        payload["minted_ns"] = int(minted_ns)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    # `surrogatepass`, symmetrically with `decode_continuity`. A vault path reaches
    # Python through filesystem decoding, so a filename with invalid UTF-8 arrives
    # as a lone surrogate and strict encoding raises on it — turning one awkward
    # filename in the packet into a failed mint. The hooks' own digests already
    # take this posture. Encoding with it and decoding without would be worse than
    # either: tokens minted and then called stale, continuity lost undiagnosed.
    return (
        base64.urlsafe_b64encode(raw.encode("utf-8", "surrogatepass")).decode("ascii").rstrip("=")
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
    # Optional, and never a reason to refuse: a token minted before the field
    # existed has none, and a malformed one is read as absent, which is the
    # older token's behaviour rather than a stale token's.
    minted = payload.get("minted_ns")
    minted_ns = minted if isinstance(minted, int) and not isinstance(minted, bool) and minted > 0 else None
    return {
        "identity": identity,
        "roles_hash": roles_hash,
        "generation": generation,
        "refs": [ref for ref in refs if isinstance(ref, str) and ref],
        "roles": [role for role in roles if isinstance(role, str) and role],
        "minted_ns": minted_ns,
    }


def continuity_minted_ns(token: str | None) -> int | None:
    """When the packet behind `token` was served, or `None` when the token
    does not say (minted before the field existed) or cannot be read. Never
    raises: the argument is a caller's string."""
    try:
        payload = decode_continuity(token)
    except Exception:  # noqa: BLE001 - a caller's string must not fail the request
        return None
    return payload.get("minted_ns") if payload else None


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


#: The anchor statuses a token carries forward: what the packet served.
_MINTED_STATUSES = frozenset({"resolved", working_set_resolve.RETRIEVAL_CARRIED_STATUS})


def mint_continuity(packet: Mapping[str, Any], *, identity: str) -> str:
    """The token for a packet AS SERVED, or `""` when there is nothing to carry.

    The caller passes the packet the guard returned, which is the whole point:
    an anchor the guard removed is not in `anchors[]`, so it cannot reach the
    token, and the next turn cannot be handed back a ref this audience may not
    see as if the server had found it.

    Only `resolved` anchors, never `partial` ones. `anchors[]` also lists a
    turn's `partial` candidates, purely so the agent can see them; a candidate
    merely LISTED is not evidence this conversation is about it, and carrying
    its ref forward would let a later turn's `continuity` qualifier alone
    promote a candidate no turn ever resolved -- exactly the widening the
    resolver's soundness rule exists to stop.

    A carried page (`retrieval_carried`) is minted too: it is the one page
    the packet served, not a listed candidate, and "continue" after it can
    only resume it if the token names it. Its ref is its path, which is what
    `working_set.continuity_page` resumes; a later turn's `continuity` still
    only qualifies an anchor that turn reached.
    """
    if not identity or packet.get("abstained"):
        return ""
    anchors = [
        item
        for item in packet.get("anchors") or ()
        if isinstance(item, Mapping) and item.get("status") in _MINTED_STATUSES
    ]
    refs = [str(item.get("ref") or "") for item in anchors]
    refs = [ref for ref in refs if ref]
    if not refs:
        return ""
    generation = packet.get("generation")
    generation = generation if isinstance(generation, Mapping) else {}
    roles = [
        str(role.get("id") or "") for role in packet.get("roles") or () if isinstance(role, Mapping)
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
            minted_ns=time.time_ns(),
        )
    except Exception:  # noqa: BLE001 - a token is an optimisation, never a promise
        log.debug("continuity token could not be minted; serving without", exc_info=True)
        return ""


def visible_continuity_refs(
    vault_root: Path,
    rows: Sequence[Any],
    refs: frozenset[str],
    *,
    purpose: str | None = None,
) -> frozenset[str]:
    """The refs of a valid token that name something the CURRENT principal
    may see: an index row, by its reported ref or its path, or an eligible
    compiled page (`working_set._eligible_agent_page`), whose page the
    release plane releases to this audience (`egress.quick_page_visible`).

    Every other ref is dropped here — one naming nothing, one naming raw
    material or a retired page, and one naming a page this audience may not
    see — before the cache key and the compile are derived from them, so a
    withheld ref and a missing one reach everything downstream as the same
    nothing. Decided before the compile rather than inside it because the
    packet cache is not keyed on the principal: a visibility decision made
    inside a compiled packet would be served to the next audience that
    passes the same token. The token itself is still passed, and still
    leads the hot profile when every ref went (`continuity_passed`), exactly
    as a token naming nothing does.

    Bounded by the token's own refs (at most `CONTINUITY_MAX_REFS`), each
    checked against the rows the request already holds and, failing that,
    one cached page read; the release decision is the one the guard reuses.
    """
    if not refs:
        return frozenset()
    from .governance import egress

    kept: set[str] = set()
    for ref in refs:
        named = frozenset({ref})
        paths = [
            str(row.path)
            for row in rows
            if getattr(row, "path", "") and working_set_resolve.names_row(named, row)
        ]
        if not paths:
            page = working_set._eligible_agent_page(vault_root, ref)
            paths = [page] if page is not None else []
        if paths and all(
            egress.quick_page_visible(vault_root, path, purpose=purpose) for path in paths
        ):
            kept.add(ref)
    return frozenset(kept)


def reset_caches_for_tests() -> None:
    """Drop the packet cache, any in-flight build marker, and the manifests the
    index published for request threads."""
    with _CACHE_LOCK:
        _PACKET_CACHE.clear()
        _BUILDS.clear()
        _INLINE_LOCKS.clear()
    working_set_index.reset_collection_manifests_for_tests()


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
    if _managed():
        # Imported here: the unmanaged CLI starts a process per call and never
        # reaches this gate, so it should not pay for the module at import.
        from . import reserved_paths

        if not reserved_paths.identity_catalogue_ready(root):
            # A cold private-identity inventory is built by a whole-vault walk
            # under the all-domain identity gate, and every governed read below
            # needs it. An interactive turn abstains and the walk runs in the
            # background.
            reserved_paths.schedule_identity_catalogue_warm(root)
            return WARMING, None, False
    if not index.readable():
        return UNAVAILABLE, None, False
    if not index.anchors():
        if _managed():
            _schedule_build(root, freshness_stamp=freshness_stamp)
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


def lexical_evidence(
    vault_root: Path, turn: str, rows, *, limit: int, freshness=None, recall_checkpoint=None
):
    """Rank only anchor pages in the maintained full-page text index.

    This is own-page retrieval evidence, not a second vote for title overlap.
    Unavailable FTS never falls back to a corpus walk or foreground repair.
    """
    from . import find_types, lexstore

    by_path = {row.path: row for row in rows if row.path}
    if not by_path:
        return [], "available"
    try:
        # A full-page match on the same single name word is not a second fact.
        # Require two distinct content units (words or unspaced runs); exact
        # aliases still resolve alone.
        content_turn = " ".join(
            token for token in working_set_index.tokens_of(working_set_index.normalize(turn))
            if token not in working_set_index.STOPWORDS
        )
        result = lexstore.search_bm25_result(
            vault_root,
            content_turn,
            min(limit, len(by_path)),
            scope="kb",
            freshness=freshness,
            allowed_paths=set(by_path),
            allow_delta=False,
            min_matched_terms=2,
            recall_checkpoint=recall_checkpoint,
        )
        if not result.readiness.complete:
            return [], result.readiness.status
        hits = [
            find_types.Hit(
                path=path,
                type=None,
                scope=None,
                title=by_path[path].title,
                updated="",
                excerpt="",
                bm25_rank=rank,
            )
            for rank, (path, _score) in enumerate(result.value or (), 1)
            if path in by_path
        ]
        return hits, "available"
    except Exception:  # noqa: BLE001 - one optional evidence lane
        log.debug("activation lexical evidence unavailable", exc_info=True)
        return [], "unavailable"


#: The floor on how many scored hits the carry recall asks for. The real
#: number is `working_set.carry_fetch_size(pages)`, which grows with the
#: corpus so the window stays wider than the rarity gate can admit; this
#: name is the default for a caller that has no page count in hand.
RETRIEVAL_CARRY_LIMIT = working_set.RETRIEVAL_CARRY_FETCH
#: Knowledge-base folders holding raw captured material rather than compiled
#: conclusions. Read off `working_set_index` rather than restated here: that
#: module already refuses to make an anchor of anything inside them, for the
#: same reason this refuses to carry one, and two spellings of "raw material"
#: would be one spelling too many.
CARRY_EXCLUDED_FOLDERS: frozenset[str] = working_set_index._RAW_MATERIAL_FOLDERS


def _is_raw_material(path: str) -> bool:
    """Is `path` a captured source or a preserved piece of evidence?

    A carried packet serves compiled conclusions. A source is what a
    conclusion was drawn FROM and evidence is what one is checked AGAINST;
    serving either as though it were durable memory is exactly the step the
    compile stage exists to stand between.

    Matched the SAME way `working_set_index` matches it — the first segment
    under the knowledge-base folder — so an ordinary note folder someone
    happened to name `Sources` is not caught by it and the two modules cannot
    disagree about what raw material is. Backslashes are folded first, so a
    row written on Windows is recognised too.
    """
    from .kbdir import kb_prefix

    inside = str(path).replace("\\", "/").removeprefix(kb_prefix())
    return inside.split("/", 1)[0] in CARRY_EXCLUDED_FOLDERS


def _is_navigation_page(path: str) -> bool:
    """Is `path` an `index.md` or `log.md`, at any directory level?

    The same predicate the recent-context block and the find corpus use
    (`find_corpus.NAVIGATION_BASENAMES`), so the modules cannot disagree
    about what navigation is. A navigation page lists the titles of the
    pages it navigates to, so it matches any turn that names one of them by
    its title — it is never itself the page a turn named.
    """
    from . import find_corpus

    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name.casefold() in find_corpus.NAVIGATION_BASENAMES


def content_words(turn: str) -> str:
    """The turn's own content WORDS, unstemmed, stopwords dropped.

    What the ranking query is given. `search_bm25_result` tokenises and
    stems whatever it receives, so it must receive words: handing it stems
    stems them a SECOND time, and Snowball does not settle in one pass —
    237 of 6,225 words in this repository change again ("collapse" ->
    "collaps" -> "collap"), and the MATCH is then issued for terms no page
    contains.
    """
    return " ".join(
        token
        for token in working_set_index.tokens_of(working_set_index.normalize(turn))
        if token not in working_set_index.STOPWORDS
    )


def content_stems(turn: str) -> tuple[str, ...]:
    """The turn's own content stems, in order, deduplicated.

    The SAME normalisation, stopword filter and stemmer the catalogue
    indexed its pages with, so a frequency measured here and a page ranked
    there are talking about the same word. Used for the rarity lookup and
    the corroboration list, which compare against the catalogue's STORED
    stems directly; never for the ranking query, which stems what it is
    given (see `content_words`). Query side: an accented word is its surface
    form only, so rarity is read on what the turn wrote, not on the folded
    variant the index also stores.
    """
    from . import bm25 as bm25_module

    return tuple(dict.fromkeys(bm25_module.tokenize(content_words(turn), query=True)))


#: What ends a proximity window. Sentence-ending punctuation and a line
#: break; a comma deliberately does not, being punctuation inside a phrase
#: rather than between two of them. The split reads the raw turn, so each
#: script's own sentence end is named: the Devanagari danda and double
#: danda, the Greek question mark, the Arabic question mark and full stop,
#: the Armenian full stop, and the CJK full stop and fullwidth ! and ?.
_SENTENCE_BREAK = re.compile(
    "[.!?;\n\r।॥;؟۔։。！？]+"
)
#: The joiners `working_set_index.tokens_of` admits inside a term. The parts
#: they join are separate words of one compound.
_TOKEN_JOINERS = re.compile(r"['\-]+")


def adjacent_rare_pairs(
    turn: str,
    rare_terms: Sequence[str],
    *,
    window: int | None = None,
) -> tuple[tuple[str, str], ...]:
    """Pairs of distinctive stems the turn said close enough together to be
    reading as one name, measured on the turn's OWN token positions.

    Distance is counted over the raw normalised tokens — stopwords included
    — because that is the distance a reader sees: "the lisbon harbour
    window" is a phrase and "flying to lisbon ... around the harbour" is
    not, and dropping the function words in between would make them look
    alike.

    A SENTENCE BOUNDARY ends the window however few tokens straddle it.
    "I am flying out to lisbon next week. The harbour was shut" puts the
    two words three tokens apart and they are still two sentences about two
    things; measured, that pairing carried a harbour ledger at 18.78. A
    comma is not a boundary — "the girvan, slot question" is one phrase
    with punctuation in it. The window measures token distance within a
    sentence; it does not measure intent, and nothing here reads meaning.

    One raw token may carry several stems ("girvan-slot", "o'brien"), and
    all of them are placed at that token's position: a compound is the
    phrase said as tightly as a phrase can be said. But a pair needs two
    WORDS: the stems of one word ("jätka" and its folded variant) are one
    thing said once, never a phrase with itself. The raw token is split on
    its joiners first, so the parts of a joined compound still pair at
    distance zero.

    An UNSPACED RUN (Han, kana, Hangul, Thai and the other bigram-indexed
    scripts) contributes nothing: the carry stays off for those scripts.
    A run's bigrams sit at one token position, and two runs side by side
    share particles and endings (日は, です, 니다) with every page in their
    script, so pairing them named pages the turn never mentioned —
    "明日は、散歩です" carried a weather note — and cost |A|x|B| pair
    groups, a minute of activation for a long Japanese turn.

    Each pair is returned once, sorted, so the caller's query sees a stable
    set.
    """
    from . import bm25 as bm25_module

    span = working_set.RETRIEVAL_CARRY_RARE_WINDOW if window is None else int(window)
    wanted = {str(term) for term in rare_terms}
    if len(wanted) < 2:
        return ()
    pairs: set[tuple[str, str]] = set()
    # Split the RAW text: `normalize` folds case and width but keeps the
    # punctuation, and splitting per sentence is what keeps a window from
    # reaching across one.
    unit_id = 0
    for sentence in _SENTENCE_BREAK.split(str(turn)):
        placed: list[tuple[int, int, str]] = []
        for index, token in enumerate(
            working_set_index.tokens_of(working_set_index.normalize(sentence))
        ):
            for part in _TOKEN_JOINERS.split(token):
                for unit in bm25_module.token_units(part, query=True):
                    unit_id += 1
                    if unit.run:
                        continue
                    for stem in dict.fromkeys(unit.stems):
                        if stem in wanted:
                            placed.append((index, unit_id, stem))
        for position, (left_at, left_unit, left) in enumerate(placed):
            for right_at, right_unit, right in placed[position + 1 :]:
                if right_at - left_at > span:
                    break
                if left != right and left_unit != right_unit:
                    first, second = sorted((left, right))
                    pairs.add((first, second))
    return tuple(sorted(pairs))


def rare_turn_terms(
    vault_root: Path,
    stems: Sequence[str],
    *,
    freshness=None,
    recall_checkpoint=None,
) -> tuple[tuple[str, ...], int, str]:
    """`(distinctive stems, indexed pages, readiness status)` for `stems`.

    One document-frequency lookup per stem over the maintained catalogue,
    bounded by the turn's own content words and measured on the same
    `fts`/`pages` join the ranking uses. Measured at 34.8 ms for a four-stem
    turn and 41.6 ms for a twenty-seven-stem turn against a 1,539-page
    knowledge base, with the request's own recall checkpoint supplied — the
    per-term cost is small and nearly all of it is the one readiness proof.
    """
    from . import lexstore

    # Navigation pages are not counted: an index or a log repeats the titles
    # it lists, so a folder-level one beside the vault's own pushed a title
    # word past the cap and the page named by its title was never carried.
    # Raw material is not counted either, nor counted as a page: four
    # captured sessions that discussed a page did the same to its title.
    result = lexstore.term_document_frequencies(
        vault_root,
        stems,
        scope="kb",
        freshness=freshness,
        allow_delta=False,
        recall_checkpoint=recall_checkpoint,
        exclude_navigation=True,
        exclude_raw_material=True,
    )
    if not result.readiness.complete:
        return (), 0, result.readiness.status
    frequencies, corpus_pages = result.value or ({}, 0)
    cap = working_set.rare_document_cap(corpus_pages)
    rare = tuple(stem for stem in stems if int(frequencies.get(stem, 0)) <= cap)
    return rare, int(corpus_pages), "available"


def carry_candidates(
    vault_root: Path,
    turn: str,
    *,
    limit: int | None = None,
    freshness=None,
    recall_checkpoint=None,
) -> tuple[tuple[tuple[str, float], ...], str]:
    """Pages this turn NAMED, for a turn that resolved no anchor at all.
    Returns `(hits, readiness status)`.

    Three deliberate differences from `lexical_evidence`, which is otherwise
    the same sqlite query:

    * **No `allowed_paths`.** Anchor-restricted recall is what makes a
      decision living in an ordinary research note unreachable — it is not an
      anchor, so it is not in the catalogue the query is confined to, so the
      turn abstains however plainly its own words name the page.
    * **Corroboration is counted over DISTINCTIVE stems only, and where
      they sit matters.** Counting it over all of them asks "did several of
      the turn's words occur here", which is co-occurrence: a two-line stub
      titled "Meeting notes" sharing "meeting", "pending" and "decision"
      with an ordinary turn passed that test and was served as durable
      memory. Narrowing to the stems that are rare in THIS corpus is most
      of the answer, but not all of it — two genuinely distinctive words
      nine tokens apart are still two things a speaker mentioned, not a
      name. A page qualifies on a PHRASE and on nothing else: both stems
      of some pair the turn said within `RETRIEVAL_CARRY_RARE_WINDOW`
      tokens. The ranking still sees the whole turn; only the gate narrows.
    * **The score is kept.** `lexical_evidence` discards it because evidence
      there is categorical. Carrying needs to compare two survivors, which a
      rank cannot express.

    Fewer than `RETRIEVAL_CARRY_MIN_RARE_TERMS` distinctive stems means no
    hit could qualify, so the ranking query is not made at all — the
    cheapest refusal is the one that never asks. A corpus smaller than
    `RETRIEVAL_CARRY_MIN_PAGES` refuses for the same reason one step back:
    rarity measured against a vault that holds no ordinary prose says only
    that the vault is small.

    What comes back IS the set of pages this turn named, which is why the
    caller can decide on the COUNT rather than on a score. Raw-material
    hits and retired pages are dropped before the caller ever sees them: a
    captured source or a preserved piece of evidence is not a candidate,
    and a superseded note answers to the same phrase as the note that
    superseded it, so leaving it in would read as two named pages.

    Everything else is the existing bounded contract: the maintained
    catalogue only, no foreground delta (`allow_delta=False`), no corpus
    walk, no directory enumeration, and an incomplete catalogue reported
    rather than repaired.
    """
    from . import lexstore

    try:
        stems = content_stems(turn)
        if len(stems) < working_set.RETRIEVAL_CARRY_MIN_RARE_TERMS:
            return (), "available"
        rare, corpus_pages, state = rare_turn_terms(
            vault_root,
            stems,
            freshness=freshness,
            recall_checkpoint=recall_checkpoint,
        )
        if state != "available":
            return (), state
        if corpus_pages < working_set.RETRIEVAL_CARRY_MIN_PAGES:
            # Rarity needs a corpus. The page count came back with the
            # frequencies, so this costs nothing beyond the lookup already
            # made, and it refuses before the ranking query.
            return (), "available"
        if len(rare) < working_set.RETRIEVAL_CARRY_MIN_RARE_TERMS:
            return (), "available"
        # Rarity says a word is name-shaped; proximity says the turn used it
        # to NAME something. Two distinctive words said together are a
        # phrase; the same two nine tokens apart are two things the speaker
        # mentioned. No phrase, no candidate — a page that enumerates many
        # things contains any few of them, so scattering is what tells a
        # list from a name.
        pairs = adjacent_rare_pairs(turn, rare)
        if not pairs:
            return (), "available"
        result = lexstore.search_bm25_result(
            vault_root,
            # The turn's WORDS, not its stems: this query stems what it is
            # given, and stemming a stem is not a no-op.
            content_words(turn),
            # Wider than the rarity gate can admit, which grows with the
            # corpus: a window a run of retired rows can fill is a window
            # that decides "one named page or two" by where it ends.
            working_set.carry_fetch_size(corpus_pages) if limit is None else limit,
            scope="kb",
            freshness=freshness,
            allow_delta=False,
            corroboration_tokens=list(rare),
            corroboration_groups=[list(pair) for pair in pairs],
            recall_checkpoint=recall_checkpoint,
            # Inside the query, so the LIMIT counts only rows that can be
            # candidates: twelve captures that repeat the turn filled the
            # window on their own when they were cut after it.
            exclude_navigation=True,
            exclude_raw_material=True,
        )
        if not result.readiness.complete:
            return (), result.readiness.status
        # Raw material, navigation pages and retired pages are dropped
        # BEFORE the caller counts what the turn named. The query already
        # left the first two out; the check stays here too, where the path
        # is read the way the index reads it (backslashes folded). A superseded note
        # and the note that superseded it answer to the same phrase, so
        # leaving it in would read as two named pages and refuse every
        # revised page in the vault; an index or a log repeats every title
        # in the vault, so leaving one in did exactly that to every turn that
        # named a page by its title.
        return (
            tuple(
                (str(path), float(score))
                for path, score in (result.value or ())
                if not _is_raw_material(path)
                and not _is_navigation_page(path)
                and working_set._is_current_page(vault_root, str(path))
            ),
            "available",
        )
    except Exception:  # noqa: BLE001 - the carry is additive; it abstains, never raises
        log.debug("activation carry recall unavailable", exc_info=True)
        return (), "unavailable"


def refresh_index(index: working_set_index.WorkingSetIndex, *, freshness_stamp: str = "") -> bool:
    """Bring a stale index up to the current vault state; False when it could not.

    A managed runtime hands the walk to the background thread, so the request
    thread reports `index_stale` rather than paying for work it did not budget
    for. An unmanaged runtime has no background worker to wait for and does the
    update inline — the honest trade for a single-user install.
    """
    if _managed():
        _schedule_build(index.vault_root, freshness_stamp=freshness_stamp)
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


def _schedule_build(vault_root: Path, *, freshness_stamp: str = "") -> None:
    """Single-flight a cold or stale build away from the request thread.

    The build records the freshness key it was scheduled for, exactly as the
    unmanaged inline update does. Without it the catalogue never stops reading
    as stale: every request reports `index_stale` and, with no build in flight,
    schedules another whole-vault walk. The walk starts after the key was read,
    so what it writes is at least as new as the key it records; a vault that
    moved again in between simply reads as stale once more. A build already in
    flight keeps its own key, and the next request decides whether that was
    enough.
    """
    root = Path(vault_root).absolute()
    with _CACHE_LOCK:
        if root in _BUILDS:
            return
        _BUILDS.add(root)

    def _warm() -> None:
        try:
            working_set_index.WorkingSetIndex(root).update(freshness_stamp=freshness_stamp or None)
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
    lexical_state: str = "not_requested",
    evidence_token: tuple[int, int, int] | None = None,
    freshness_snapshot: Any = None,
    lexical_seconds: float = 0.0,
) -> dict[str, Any]:
    """Compile (or reuse) one unguarded packet. Never raises: it abstains instead.

    `lexical_seconds` is what this request's own lexical pass measured,
    passed through to the carry so it can ask the budget for a reserve its
    stage can actually be paid for out of.
    """
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

    def evidence_changed() -> dict | None:
        if evidence_token is None or index.token() == evidence_token:
            return None
        return working_set.abstained_packet(
            reason=UNAVAILABLE,
            max_chars=limit,
            generation={
                "freshness_key": stamp,
                "index_generation": index.generation(),
                "index_stale": True,
                "lexical_evidence": "stale",
                "continuity": unevaluated_continuity(continuity),
                **registry.generation_block(),
            },
        )

    changed = evidence_changed()
    if changed is not None:
        return changed

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
    continuity_passed = continuity_state == CONTINUITY_APPLIED
    try:
        continuity_refs = visible_continuity_refs(
            root, index.anchors(), continuity_refs, purpose=purpose
        )
    except Exception:  # noqa: BLE001 - a ref that cannot be decided is not disclosed
        log.warning("continuity visibility check failed; dropping the refs", exc_info=True)
        continuity_refs = frozenset()
    key = cache_key(
        freshness_key=freshness_key,
        index_generation=index.generation(),
        roles_hash=registry.roles_hash,
        turn=turn,
        max_chars=limit,
        retrieval_paths=retrieval_paths,
        continuity=continuity,
        anchor=anchor,
        continuity_refs=continuity_refs,
    )
    cache_identity = (str(root.absolute()), key, lexical_state, index.token())
    with _CACHE_LOCK:
        cached = _PACKET_CACHE.get(cache_identity)
        if cached is not None:
            _PACKET_CACHE.move_to_end(cache_identity)
    if cached is not None:
        return copy.deepcopy(cached)

    def _abstain_unavailable() -> dict[str, Any]:
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
            freshness_snapshot=freshness_snapshot,
            continuity_refs=continuity_refs,
            continuity_minted_ns=continuity_minted_ns(continuity) if continuity_passed else None,
            continuity_passed=continuity_passed,
            anchor=anchor,
            lexical_seconds=lexical_seconds,
        )
    except working_set.BudgetExhausted as exc:
        # A deliberate budget skip, not a bug: `log.info`, no traceback. The
        # broad handler below is for genuine compilation failures and would
        # otherwise log this expected, request-shaped outcome as a WARNING
        # with a full stack trace every time a client's clock simply ran out.
        log.info("activation skipped %s for the request budget", exc)
        packet = _abstain_unavailable()
        from . import request_budget

        active_budget = request_budget.current()
        block = active_budget.as_response_block() if active_budget is not None else None
        if block is not None:
            packet["request_budget"] = block
        return packet
    except Exception:  # noqa: BLE001 - the whole operation is additive and abstains
        log.warning("activation compilation failed; abstaining", exc_info=True)
        return _abstain_unavailable()
    # `compile_packet` reports whether a visible ref of a valid token qualified
    # anything; a token that qualified nothing contributed nothing, and
    # `applied` would claim it had.
    if continuity_state == CONTINUITY_APPLIED:
        continuity_state = packet["generation"].get("continuity") or CONTINUITY_STALE
    packet["generation"]["continuity"] = continuity_state
    packet["generation"]["lexical_evidence"] = lexical_state
    changed = evidence_changed()
    if changed is not None:
        return changed
    if index_stale:
        # Transient, so never cached: the scheduled build may leave the vault's
        # rows and the sidecar token exactly as they were, and a cached copy
        # would go on saying `index_stale` after the catalogue caught up.
        packet["generation"]["index_stale"] = True
        return packet
    if packet["generation"].get("semantic_evidence") in {
        "warming",
        "busy",
        "unavailable",
    } or lexical_state not in {"available", "not_requested", "agent_choice"}:
        return packet
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
