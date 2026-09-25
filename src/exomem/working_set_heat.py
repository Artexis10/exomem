"""The hot profile as a decaying event projection (close-memory-loop 7.3, 7.4).

What "the user was just working on" means, kept as EVENTS rather than
re-derived from each file's current mtime. Re-deriving it let a maintenance
batch erase the history it overwrote: the batch's timestamp replaced the
user's, and nothing could tell a user's multi-page commit from a maintenance
pass. Here every event says where it came from, so a batch simply writes none.

An event is a path, a time, a channel and an origin, plus the opaque
attribution its caller supplied (ruling S5-1). It never carries text. The
channels are a closed vocabulary:

* deliberate — `work` (a governed write outside any batch scope, or a lone
  external edit), `pick` (an agent's admitted `anchor=` choice), `episode` (a
  page an episode recap names in `about`);
* selection — `read` (a released read), `cite` (a page a governed write
  cited);
* contact only — `captured` (a captured session) and `episode_page` (the recap
  page itself). These order the recent-context block and never a referent.

Decay is displacement plus a session window, never a score: the ring keeps
the newest `RING_MAX` events, and a referent is ranked only from the latest
working session (`SESSION_GAP_NS`). There are no weights and no frequency
term, so a page read a thousand times ranks as one read at its latest time,
and exact ties stay ties for the agent to choose between.

Ruling S5-1 scopes the ranking to the caller's own thread first. A caller that
supplies a session key is answered from that session's own deliberate acts and
last served thread; a fresh session from its workspace's; everything else, and
every caller that supplies no key, from the vault-wide ranking, which is the
design's projection unchanged.

This half of the module is pure: functions over events, no sidecar, no vault.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

#: The newest events the ring keeps. Several busy sessions; the aggregate over
#: it is a few milliseconds and is cached per sidecar token.
RING_MAX = 4096
#: Recent governed commits kept for echo matching: only recent ones can echo.
ATTRIBUTED_MAX = 4096
#: Pages a cold seed turns into events. It ranks the newest; more buys nothing.
SEED_MAX = 256
#: A larger external delta is a sync or a batch by definition, and is folded as
#: one burst: string work only.
MAX_FOLD_PATHS = 2048
#: Sessions whose last served thread is remembered, least recently seen dropped.
SESSIONS_MAX = 512
#: A gap this long between deliberate or selection events ends a working
#: session. Overnight splits one, a lunch break does not.
SESSION_GAP_NS = 6 * 3600 * 1_000_000_000
#: How wide a leading tie may be before it is cut. Mirrors `working_set.
#: HOT_PROFILE_K`, which is the one callers pass.
DEFAULT_LIMIT = 5

#: A write burst: a chain of this many pages or more whose edits follow one
#: another with no gap longer than `BURST_GAP_NS`. It is how an EXTERNAL batch
#: (a sync, an import) is told from a user's edit, now that governed writes
#: say for themselves whether they are bulk. A chain rather than a fixed
#: window, because a batch on a loaded machine stalled 2.9 s between two
#: pages and a one-second window cut its tail off into "work".
BURST_PAGES = 3
BURST_GAP_NS = 5_000_000_000

DELIBERATE: tuple[str, ...] = ("work", "pick", "episode")
SELECTION: tuple[str, ...] = ("read", "cite")
CONTACT_ONLY: tuple[str, ...] = ("captured", "episode_page")
CHANNELS: tuple[str, ...] = DELIBERATE + SELECTION + CONTACT_ONLY

#: The recent-context reason each contact channel supplies (`working_set.
#: RECENT_CONTEXT_REASONS`, closed and unchanged). `episode` is absent on
#: purpose: an `about` page is a deliberate act for the referent, and the
#: contact it stands for is the recap page's own `episode_page` row.
REASON_OF: dict[str, str] = {
    "work": "edited",
    "episode_page": "episode",
    "pick": "activated",
    "read": "activated",
    "cite": "activated",
    "captured": "captured",
}
#: Equal-time tie-break between contact channels, in the reasons' own order.
_CONTACT_ORDER: tuple[str, ...] = ("work", "episode_page", "pick", "read", "cite", "captured")

STATES: tuple[str, ...] = ("current", "partial", "seeded", "behind", "empty")

#: The tiers of ruling S5-1, highest first.
TIER_SESSION = 1
TIER_WORKSPACE = 2
TIER_VAULT = 3


class HeatEvent(NamedTuple):
    """One event. `session` and `workspace` are derived keys, never raw ids."""

    ts_ns: int
    path: str
    channel: str
    origin: str = ""
    client: str = ""
    session: str = ""
    workspace: str = ""


class SessionMark(NamedTuple):
    """A session's last served thread: the pages of the last token minted for
    it, and the workspace it was last seen in. Keys are derived, never raw."""

    session: str
    workspace: str = ""
    client: str = ""
    paths: tuple[str, ...] = ()
    minted_ns: int = 0
    seen_ns: int = 0


class Attribution(NamedTuple):
    """The caller's derived keys. Empty fields mean "not supplied"."""

    session: str = ""
    workspace: str = ""
    client: str = ""


@dataclass(frozen=True, slots=True)
class HeatRow:
    """The latest time each channel touched one path."""

    path: str
    work_ns: int = 0
    pick_ns: int = 0
    episode_ns: int = 0
    read_ns: int = 0
    cite_ns: int = 0
    captured_ns: int = 0
    episode_page_ns: int = 0
    #: A served thread's time, in the session and workspace tiers only.
    mark_ns: int = 0

    @property
    def deliberate_ns(self) -> int:
        return max(self.work_ns, self.pick_ns, self.episode_ns, self.mark_ns)

    @property
    def selection_ns(self) -> int:
        return max(self.read_ns, self.cite_ns)

    @property
    def contact_ns(self) -> int:
        # No `episode_ns`: see `REASON_OF`.
        return max(
            self.work_ns,
            self.pick_ns,
            self.read_ns,
            self.cite_ns,
            self.captured_ns,
            self.episode_page_ns,
            self.mark_ns,
        )

    @property
    def contact_reason(self) -> str:
        """The reason of the channel that supplied `contact_ns`."""
        latest = self.contact_ns
        if not latest:
            return ""
        if self.mark_ns == latest:
            return "activated"
        for channel in _CONTACT_ORDER:
            if getattr(self, f"{channel}_ns") == latest:
                return REASON_OF[channel]
        return ""


class Lead(NamedTuple):
    """One tier's ranked candidates, groups of equal key, best first.

    `token` means the tier is a continuity thread taken whole: it never falls
    through to its next group or to a lower tier, exactly as a leading token
    never did before the projection existed.
    """

    tier: int
    token: bool
    groups: tuple[tuple[str, ...], ...]


class Members(NamedTuple):
    """The referent's members, and which tier supplied them."""

    tier: int = TIER_VAULT
    token: bool = False
    paths: tuple[str, ...] = ()


class Contact(NamedTuple):
    """One recent-context candidate, in the order the block should offer it."""

    path: str
    ts_ns: int
    reason: str
    tier: int


@dataclass(frozen=True, slots=True)
class HeatProfile:
    """What one sidecar state says, aggregated once and cached on its token."""

    events: tuple[HeatEvent, ...]
    sessions: Mapping[str, SessionMark]
    state: str
    session_start_ns: int
    last_deliberate_ns: int
    window_rows: Mapping[str, HeatRow]
    all_rows: Mapping[str, HeatRow]
    digest: str
    salt: str = ""
    token: tuple = ()
    tombstones: frozenset[str] = field(default_factory=frozenset)
    #: Text facts the sidecar keeps beside the ring (`observed_through_ns`,
    #: `seeded_at_ns`), for the fold and the state report.
    meta: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def aggregate(
    events: Iterable[HeatEvent],
    *,
    since_ns: int = 0,
    marks: Iterable[tuple[str, int]] = (),
) -> dict[str, HeatRow]:
    """`{path: HeatRow}`, each channel at its LATEST time. No counts: that is
    what closes the popularity loop by construction. `marks` are `(path,
    minted_ns)` pairs of served threads, for the session and workspace tiers."""
    latest: dict[str, dict[str, int]] = {}
    for event in events:
        if event.ts_ns < since_ns or event.channel not in CHANNELS:
            continue
        slot = latest.setdefault(event.path, {})
        name = f"{event.channel}_ns"
        if event.ts_ns > slot.get(name, 0):
            slot[name] = int(event.ts_ns)
    for path, minted in marks:
        slot = latest.setdefault(path, {})
        if minted > slot.get("mark_ns", 0):
            slot["mark_ns"] = int(minted)
    return {path: HeatRow(path, **fields) for path, fields in latest.items()}


def session_start(events: Iterable[HeatEvent]) -> int:
    """Where the latest working session begins: walking deliberate and
    selection events newest first, the first event after a gap of
    `SESSION_GAP_NS` or more. Relative to the events, never to the clock, so a
    profile does not change merely because time passed, and "continue" after
    three weeks away still resumes the last session. `0` with no events."""
    times = sorted(
        (int(event.ts_ns) for event in events if event.channel in DELIBERATE + SELECTION),
        reverse=True,
    )
    if not times:
        return 0
    start = times[0]
    for stamp in times[1:]:
        if start - stamp >= SESSION_GAP_NS:
            break
        start = stamp
    return start


def build_profile(
    events: Iterable[HeatEvent],
    *,
    sessions: Iterable[SessionMark] | Mapping[str, SessionMark] = (),
    state: str = "current",
    salt: str = "",
    token: tuple = (),
    tombstones: frozenset[str] = frozenset(),
    meta: Mapping[str, str] | None = None,
) -> HeatProfile:
    """Aggregate `events` (oldest first) into the profile every reader shares."""
    kept = tuple(event for event in events if event.path not in tombstones)
    marks = sessions.values() if isinstance(sessions, Mapping) else sessions
    by_session = {mark.session: mark for mark in marks if mark.session}
    start = session_start(kept)
    window = aggregate(kept, since_ns=start) if start else {}
    everything = aggregate(kept)
    last_deliberate = max(
        (event.ts_ns for event in kept if event.channel in DELIBERATE), default=0
    )
    profile = HeatProfile(
        events=kept,
        sessions=by_session,
        state=state if state in STATES else "current",
        session_start_ns=start,
        last_deliberate_ns=int(last_deliberate),
        window_rows=window,
        all_rows=everything,
        digest="",
        salt=salt,
        token=token,
        tombstones=frozenset(tombstones),
        meta=dict(meta or {}),
    )
    return _with_digest(profile)


# --------------------------------------------------------------------------- #
# The referent: tiers, groups and the eligibility walk
# --------------------------------------------------------------------------- #


def _key(row: HeatRow) -> tuple[int, int]:
    return (row.deliberate_ns, row.selection_ns)


def _groups(
    rows: Mapping[str, HeatRow], admissible: Callable[[str], bool] | None
) -> tuple[tuple[str, ...], ...]:
    """Rows keyed `(deliberate, selection)`, grouped by equal key, best first.
    A row keyed `(0, 0)` drops out: an untouched page is not a referent."""
    keyed: dict[tuple[int, int], list[str]] = {}
    for path, row in rows.items():
        key = _key(row)
        if key == (0, 0):
            continue
        if admissible is not None and not admissible(path):
            continue
        keyed.setdefault(key, []).append(path)
    return tuple(tuple(sorted(keyed[key])) for key in sorted(keyed, reverse=True))


def _token_leads(
    events: Iterable[HeatEvent],
    paths: frozenset[str],
    minted_ns: int | None,
    admissible: Callable[[str], bool] | None,
) -> bool:
    """Is the thread still the latest thing that happened? True unless a
    deliberate act on a path outside it came later than the mint (R-M,
    generalised from edits to deliberate acts: a read does not move a
    conversation on). A token that does not say when it was minted leads."""
    if minted_ns is None:
        return True
    for event in events:
        if (
            event.channel in DELIBERATE
            and event.ts_ns > minted_ns
            and event.path not in paths
            and (admissible is None or admissible(event.path))
        ):
            return False
    return True


def _session_events(profile: HeatProfile, session: str) -> list[HeatEvent]:
    return [event for event in profile.events if session and event.session == session]


def _workspace_scope(
    profile: HeatProfile, attribution: Attribution
) -> tuple[list[HeatEvent], list[SessionMark]]:
    """The workspace's events and served threads from OTHER sessions."""
    workspace, own = attribution.workspace, attribution.session
    if not workspace:
        return [], []
    sessions = {
        key
        for key, mark in profile.sessions.items()
        if mark.workspace == workspace and key != own
    }
    events = [
        event
        for event in profile.events
        if not (own and event.session == own)
        and (event.workspace == workspace or (event.session and event.session in sessions))
    ]
    marks = [profile.sessions[key] for key in sorted(sessions) if profile.sessions[key].paths]
    return events, marks


def _holds_deliberate(events: Iterable[HeatEvent]) -> bool:
    return any(event.channel in DELIBERATE for event in events)


def leading(
    profile: HeatProfile,
    *,
    attribution: Attribution | None = None,
    token_paths: frozenset[str] = frozenset(),
    token_minted_ns: int | None = None,
    token_passed: bool = False,
    admissible: Callable[[str], bool] | None = None,
    marks: Mapping[str, SessionMark] | None = None,
) -> tuple[Lead, ...]:
    """The referent's candidate tiers, highest first (ruling S5-1).

    1. **Own session**, when the caller supplied a session key: its passed
       token (or, failing one, the session's last served thread), and the
       deliberate acts that carry its key. The thread leads, taken whole,
       until the session's own deliberate act elsewhere is newer than it.
    2. **Same workspace**: other sessions' deliberate acts and last served
       threads carrying the caller's workspace key, ranked by time.
    3. **Vault-wide**: the design's projection — rows with an event in the
       latest working session keyed `(deliberate, selection)` — and, for a
       caller with no session key, the passed token under today's rule.

    A higher tier is listed only when it holds a deliberate act or a thread:
    selection alone never lifts a tier over a deliberate act below it. Nothing
    here reads a page; `members` walks the result with the caller's own
    eligibility check. `marks` overrides `profile.sessions` with the caller's
    visibility-filtered copy.
    """
    who = attribution or Attribution()
    sessions = profile.sessions if marks is None else marks
    leads: list[Lead] = []

    if who.session:
        own = _session_events(profile, who.session)
        thread: tuple[frozenset[str], int | None] | None = None
        if token_passed:
            thread = (frozenset(token_paths), token_minted_ns)
        elif (stored := sessions.get(who.session)) is not None and stored.paths:
            thread = (frozenset(stored.paths), stored.minted_ns or None)
        if thread is not None and _token_leads(own, thread[0], thread[1], admissible):
            leads.append(Lead(TIER_SESSION, True, (tuple(sorted(thread[0])),)))
            return tuple(leads)
        if _holds_deliberate(own) or thread is not None:
            marked = ((path, thread[1] or 0) for path in thread[0]) if thread else ()
            leads.append(
                Lead(TIER_SESSION, False, _groups(aggregate(own, marks=marked), admissible))
            )

    if who.workspace:
        view = replace(profile, sessions=sessions)
        events, threads = _workspace_scope(view, who)
        if _holds_deliberate(events) or threads:
            marked = [(path, mark.minted_ns) for mark in threads for path in mark.paths]
            leads.append(
                Lead(TIER_WORKSPACE, False, _groups(aggregate(events, marks=marked), admissible))
            )

    if token_passed and not who.session:
        paths = frozenset(token_paths)
        if _token_leads(profile.events, paths, token_minted_ns, admissible):
            leads.append(Lead(TIER_VAULT, True, (tuple(sorted(paths)),)))
            return tuple(leads)
    leads.append(Lead(TIER_VAULT, False, _groups(profile.window_rows, admissible)))
    return tuple(leads)


def members(
    leads: Sequence[Lead],
    *,
    limit: int = DEFAULT_LIMIT,
    eligible: Callable[[str], bool] | None = None,
) -> Members:
    """Walk `leads` to the referent: the first group with an eligible member,
    its eligible members sorted by path and cut at `limit`.

    At most `limit` eligibility checks in all, which is the bound on page
    reads the hot profile has always had. An ineligible top row is skipped
    for the next group; a tier with nothing eligible falls to the next tier;
    a thread (`Lead.token`) never falls through, so a conversation whose pages
    all went abstains rather than being answered from somewhere else.
    """
    reads = 0
    for lead in leads:
        for group in lead.groups:
            chosen: list[str] = []
            for path in group:
                if len(chosen) >= limit:
                    break
                if eligible is not None:
                    if reads >= limit:
                        return Members(lead.tier, lead.token, tuple(chosen))
                    reads += 1
                    if not eligible(path):
                        continue
                chosen.append(path)
            if chosen:
                return Members(lead.tier, lead.token, tuple(chosen))
        if lead.token:
            return Members(lead.tier, True, ())
    return Members()


# --------------------------------------------------------------------------- #
# Recent context
# --------------------------------------------------------------------------- #


def _contacts(rows: Mapping[str, HeatRow], tier: int) -> list[Contact]:
    out = [
        Contact(path, row.contact_ns, row.contact_reason, tier)
        for path, row in rows.items()
        if row.contact_ns
    ]
    out.sort(key=lambda item: (-item.ts_ns, _reason_rank(item.reason), item.path))
    return out


def _reason_rank(reason: str) -> int:
    order = ("edited", "episode", "activated", "captured", "planning")
    return order.index(reason) if reason in order else len(order)


def recent(
    profile: HeatProfile,
    *,
    attribution: Attribution | None = None,
    marks: Mapping[str, SessionMark] | None = None,
    limit: int = 256,
) -> tuple[Contact, ...]:
    """Contacts newest first, the caller's leading tier first (ruling S5-1),
    then the remaining tiers in order, each path once.

    The leading tier is the first of own session and workspace that holds a
    deliberate act or a served thread, else the vault. A served thread's
    pages are `activated` contacts at their mint time; every other contact
    keeps the channel that supplied it (`REASON_OF`). The vault tier is every
    event in the ring however old: the block keeps U1's eight latest contacts
    with their dates, and the agent reads the dates.
    """
    who = attribution or Attribution()
    sessions = profile.sessions if marks is None else marks
    tiers: dict[int, list[Contact]] = {}
    holds: list[int] = []
    if who.session:
        own = _session_events(profile, who.session)
        stored = sessions.get(who.session)
        marked = [(path, stored.minted_ns) for path in stored.paths] if stored else []
        tiers[TIER_SESSION] = _contacts(aggregate(own, marks=marked), TIER_SESSION)
        if _holds_deliberate(own) or marked:
            holds.append(TIER_SESSION)
    if who.workspace:
        view = replace(profile, sessions=sessions)
        events, threads = _workspace_scope(view, who)
        marked = [(path, mark.minted_ns) for mark in threads for path in mark.paths]
        tiers[TIER_WORKSPACE] = _contacts(aggregate(events, marks=marked), TIER_WORKSPACE)
        if _holds_deliberate(events) or marked:
            holds.append(TIER_WORKSPACE)
    tiers[TIER_VAULT] = _contacts(profile.all_rows, TIER_VAULT)
    lead = holds[0] if holds else TIER_VAULT
    order = [lead, *(tier for tier in (TIER_SESSION, TIER_WORKSPACE, TIER_VAULT) if tier != lead)]
    out: list[Contact] = []
    seen: set[str] = set()
    for tier in order:
        for contact in tiers.get(tier, ()):
            if contact.path in seen:
                continue
            seen.add(contact.path)
            out.append(contact)
            if len(out) >= limit:
                return tuple(out)
    return tuple(out)


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #

#: How much of the output the digest covers: every row a referent or the
#: recent-context block could be drawn from before a page is read.
_DIGEST_REFERENT_ROWS = 16
_DIGEST_CONTACTS = 24


def _with_digest(profile: HeatProfile) -> HeatProfile:
    referent = sorted(
        ((row.path, _key(row)) for row in profile.window_rows.values() if _key(row) != (0, 0)),
        key=lambda item: (-item[1][0], -item[1][1], item[0]),
    )[:_DIGEST_REFERENT_ROWS]
    contacts = _contacts(profile.all_rows, TIER_VAULT)[:_DIGEST_CONTACTS]
    material = repr(
        (
            profile.session_start_ns,
            profile.last_deliberate_ns,
            referent,
            [(item.path, item.ts_ns, item.reason) for item in contacts],
            profile.state,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return replace(profile, digest=digest)


def view_digest(
    profile: HeatProfile,
    attribution: Attribution | None = None,
    *,
    marks: Mapping[str, SessionMark] | None = None,
) -> str:
    """The digest of what THIS caller's packet is compiled from: the vault
    digest alone for a caller with no keys, else that plus its own session
    and workspace tiers. Another session's activity never moves it unless it
    reaches this caller's tiers, so parallel sessions cost each other no
    cache hits."""
    who = attribution or Attribution()
    if not who.session and not who.workspace:
        return profile.digest
    scoped = [
        (lead.tier, lead.token, lead.groups[:4])
        for lead in leading(profile, attribution=who, marks=marks)
        if lead.tier != TIER_VAULT
    ]
    contacts = [
        (item.path, item.ts_ns, item.reason, item.tier)
        for item in recent(profile, attribution=who, marks=marks, limit=_DIGEST_CONTACTS)
        if item.tier != TIER_VAULT
    ]
    material = repr((profile.digest, scoped, contacts))
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Classifying writes and external changes
# --------------------------------------------------------------------------- #

#: The channel a governed or external change to a page of each recent-context
#: reason records. An episode recap written by `episode_memory` is recorded by
#: `note_episode`, with its caller's attribution, not by the commit seam.
_COMMIT_CHANNEL = {"edited": "work", "captured": "captured"}
_EXTERNAL_CHANNEL = {"edited": "work", "captured": "captured", "episode": "episode_page"}


def classify_commit(path: str, *, traced: bool, in_batch: bool, reason: str) -> str | None:
    """The channel a governed commit of `path` records, or `None`.

    Nothing without a mutation trace (no command wrote it on anyone's
    behalf), nothing inside a batch scope (a maintenance pass is nobody's
    work), and nothing for a page that is not working context (`reason`, the
    recent-context block's own rule).
    """
    if not traced or in_batch:
        return None
    return _COMMIT_CHANNEL.get(reason)


def counts_toward_burst(path: str) -> bool:
    """Whether an edit to `path` can make a write burst.

    Not a navigation page, which every confirmed write rewrites, and not an
    episode recap: a revision writes two recap pages, the new one and the
    supersede mark on the old, which is one structured record rather than a
    batch edit.
    """
    from . import find_corpus, source_taxonomy
    from .kbdir import kb_prefix

    if path.rsplit("/", 1)[-1].casefold() in find_corpus.NAVIGATION_BASENAMES:
        return False
    inner = path[len(kb_prefix()) :] if path.startswith(kb_prefix()) else path
    return not inner.startswith(f"Sources/{source_taxonomy.EPISODE_PATH_LABEL}/")


def burst_paths(edited: Mapping[str, int]) -> frozenset[str]:
    """The pages whose edit fell in a write burst: a maximal chain of at least
    `BURST_PAGES` edits, each within `BURST_GAP_NS` of the next, navigation
    pages and episode recaps not counted. One pass over the times, sorted."""
    times = sorted(
        (int(mtime), path)
        for path, mtime in edited.items()
        if int(mtime) > 0 and counts_toward_burst(path)
    )
    burst: set[str] = set()
    chain: list[str] = []
    previous: int | None = None
    for mtime, path in times:
        if previous is not None and mtime - previous > BURST_GAP_NS:
            if len(chain) >= BURST_PAGES:
                burst.update(chain)
            chain = []
        chain.append(path)
        previous = mtime
    if len(chain) >= BURST_PAGES:
        burst.update(chain)
    return frozenset(burst)


def default_reason_for(paths: Iterable[str]) -> Callable[[str], str]:
    """The recent-context block's own rule for what is working context, with
    the collection directories derived from `paths` (string work only)."""
    from . import working_set

    collections = working_set._recent_collection_dirs(paths)
    return lambda rel: working_set._recent_reason_for(rel, collections=collections)


class FoldResult(NamedTuple):
    events: tuple[HeatEvent, ...] = ()
    deferred: frozenset[str] = frozenset()
    tombstones: frozenset[str] = frozenset()


def fold_external_events(
    changed: Mapping[str, Sequence[int]],
    *,
    deleted: Iterable[str] = (),
    attributed: Mapping[str, Sequence[int]] | None = None,
    in_flight: frozenset[str] = frozenset(),
    now_ns: int,
    reason_for: Callable[[str], str] | None = None,
) -> FoldResult:
    """Classify one fold's external changes. `changed` maps a vault-relative
    path to its freshness signature `(mtime_ns, ctime_ns, size)`.

    * A path still being committed is DEFERRED to the next fold, never
      classified, so an echo cannot be mistaken for an edit mid-commit.
    * A path whose signature equals the one our own governed commit recorded
      is that commit's echo, and is skipped before bursts are counted, so a
      governed batch can neither absorb nor pose as an external edit.
    * The rest are external edits at `min(mtime, now)`: a future-dated mtime
      from a skewed device cannot lead forever.
    * The burst rule runs over this fold's external edits only. A burst's
      members get no event and erase nothing; a captured session is exempt,
      being the record of a conversation rather than a batch edit. A delta
      larger than `MAX_FOLD_PATHS` is one burst by definition.
    * Deleted paths become tombstones.
    """
    reason_of = reason_for or default_reason_for((*changed, *deleted))
    attributed = attributed or {}
    deferred: set[str] = set()
    edits: dict[str, int] = {}
    for path in sorted(changed):
        signature = tuple(int(part) for part in changed[path])
        if path in in_flight:
            deferred.add(path)
            continue
        previous = attributed.get(path)
        if previous is not None and tuple(int(part) for part in previous) == signature:
            continue
        edits[path] = min(int(signature[0]), int(now_ns)) if signature else int(now_ns)
    tombstones = frozenset(str(path) for path in deleted)
    if len(edits) > MAX_FOLD_PATHS:
        return FoldResult((), frozenset(deferred), tombstones)
    burst = burst_paths(edits)
    events: list[HeatEvent] = []
    for path, stamp in sorted(edits.items(), key=lambda item: (item[1], item[0])):
        channel = _EXTERNAL_CHANNEL.get(reason_of(path))
        if channel is None:
            continue
        if path in burst and channel != "captured":
            continue
        events.append(HeatEvent(stamp, path, channel, origin="external"))
    return FoldResult(tuple(events), frozenset(deferred), tombstones)


def seed_events(
    mtimes: Mapping[str, int],
    *,
    reason_for: Callable[[str], str] | None = None,
    limit: int = SEED_MAX,
) -> tuple[HeatEvent, ...]:
    """The cold seed: today's rules exactly, once per sidecar.

    A seed has no history to consult, so it is the one place R-N2's cutoff
    survives: a burst's members carry no edit, and neither does any edit at
    or before the latest burst's newest one, which may have taken the user's
    own page with it. A captured session and an episode recap are exempt, as
    the recent-context block always exempted them. The newest `limit` become
    `origin="seed"` events, oldest first.
    """
    reason_of = reason_for or default_reason_for(mtimes)
    burst = burst_paths(mtimes)
    after_burst = max((int(mtimes[path]) for path in burst), default=0)
    chosen: list[HeatEvent] = []
    for path, mtime in mtimes.items():
        stamp = int(mtime)
        if stamp <= 0:
            continue
        channel = _EXTERNAL_CHANNEL.get(reason_of(path))
        if channel is None:
            continue
        if channel == "work" and (path in burst or stamp <= after_burst):
            continue
        chosen.append(HeatEvent(stamp, path, channel, origin="seed"))
    chosen.sort(key=lambda event: (-event.ts_ns, event.path))
    return tuple(sorted(chosen[: max(0, limit)], key=lambda event: (event.ts_ns, event.path)))


# =========================================================================== #
# The sidecar
# =========================================================================== #
#
# Machine-local, never in the vault, disposable: the same placement as the
# activation index, but its own file. Heat changes on every read, and the
# anchor sidecar's generation feeds the packet cache key, the continuity
# identity and the anchor row cache, so coupling the two would invalidate
# anchor rows on every read.

SIDECAR_NAME = ".working-set-heat.sqlite"
#: Bumped whenever the tables change shape; a mismatch wipes and reseeds.
SCHEMA_VERSION = 1
#: A seam never waits on heat longer than this. The shared sidecar default is
#: five seconds, which a read must never pay for a hint.
BUSY_TIMEOUT_MS = 50
#: The counter a dropped event increments. Dropping fails nothing.
DROPPED_METRIC = "exomem_heat_events_dropped_total"
#: What derived keys are cut to.
KEY_HEX = 24
#: The longest caller-supplied key value accepted; longer is ignored, never refused.
KEY_MAX_CHARS = 256

_LOCK = threading.Lock()
#: `{sidecar path: (token, profile)}`: the aggregate is derived once per token.
_PROFILES: dict[str, tuple[tuple, HeatProfile]] = {}
#: Sidecar parents already known to exist, so a warm seam pays no `stat`.
_PARENTS: set[str] = set()


def disabled() -> bool:
    """The activation kill switch turns the whole projection off. There is no
    switch of its own, and deliberately not the query-log gate, which would
    switch the read tier off on every lean install."""
    from . import working_set_index

    return working_set_index.disabled()


def sidecar_path(vault_root: Path) -> Path:
    from .state_paths import vault_state_dir

    return vault_state_dir(Path(vault_root)) / SIDECAR_NAME


def _drop(reason: str) -> None:
    try:
        from . import metrics

        metrics.inc_counter(DROPPED_METRIC, {"reason": reason})
    except Exception:  # noqa: BLE001 - a counter never fails a seam
        pass


def _connect(vault_root: Path) -> sqlite3.Connection | None:
    """One short-lived connection with the seam's busy timeout, or `None`
    under the kill switch or when the file cannot be opened."""
    if disabled():
        return None
    try:
        path = sidecar_path(vault_root)
        parent = str(path.parent)
        if parent not in _PARENTS:
            from . import sidecar_store

            sidecar_store.ensure_sidecar_parent(path)
            _PARENTS.add(parent)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    except (OSError, sqlite3.Error, ValueError):
        log.debug("heat sidecar unavailable", exc_info=True)
        return None
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        _ensure_schema(conn)
    except sqlite3.Error:
        log.debug("heat sidecar schema could not be prepared", exc_info=True)
        conn.close()
        return None
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "heat_meta" in existing:
        row = conn.execute("SELECT value FROM heat_meta WHERE key = 'schema_version'").fetchone()
        if row is not None and str(row[0]) == str(SCHEMA_VERSION):
            return
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:  # pragma: no cover - WAL unavailable on unusual filesystems
        pass
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS heat_events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_ns INTEGER NOT NULL, "
            "path TEXT NOT NULL, channel TEXT NOT NULL, origin TEXT NOT NULL, "
            "client TEXT NOT NULL, session TEXT NOT NULL, workspace TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS attributed ("
            "path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, "
            "size INTEGER NOT NULL, ts_ns INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS heat_sessions ("
            "session TEXT PRIMARY KEY, workspace TEXT NOT NULL, client TEXT NOT NULL, "
            "paths TEXT NOT NULL, minted_ns INTEGER NOT NULL, seen_ns INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS heat_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)")
        row = conn.execute("SELECT value FROM heat_meta WHERE key = 'schema_version'").fetchone()
        if row is not None and str(row[0]) != str(SCHEMA_VERSION):
            # A different shape: the derived rows go, the token keeps counting,
            # so a consumer that cached the old rows can never believe it current.
            for table in ("heat_events", "attributed", "heat_sessions"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
            conn.execute("DELETE FROM heat_meta")
        conn.execute(
            "INSERT OR REPLACE INTO heat_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO heat_meta (key, value) VALUES ('salt', ?)",
            (secrets.token_hex(16),),
        )
        if conn.execute("SELECT 1 FROM meta WHERE key = 'instance'").fetchone() is None:
            from . import sidecar_store

            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('instance', ?)",
                (secrets.randbelow(sidecar_store.INSTANCE_MAX) + 1,),
            )
        _bump(conn)


def _bump(conn: sqlite3.Connection) -> None:
    cur = conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
    if cur.rowcount == 0:
        conn.execute("INSERT INTO meta (key, value) VALUES ('generation', 1)")


def _token(conn: sqlite3.Connection) -> tuple:
    rows = dict(
        conn.execute("SELECT key, value FROM meta WHERE key IN ('generation', 'instance')")
    )
    return (int(rows.get("instance") or 0), int(rows.get("generation") or 0))


def _write(vault_root: Path, work: Callable[[sqlite3.Connection], None], *, what: str) -> bool:
    """Run `work` in one write transaction that bumps the token. Never raises:
    a busy or broken sidecar drops the write, counts it, and fails nothing."""
    conn = _connect(vault_root)
    if conn is None:
        if not disabled():
            _drop(what)
        return False
    try:
        with conn:
            work(conn)
            _bump(conn)
        return True
    except sqlite3.Error:
        log.debug("heat %s dropped", what, exc_info=True)
        _drop(what)
        return False
    finally:
        conn.close()


def append(vault_root: Path, events: Iterable[HeatEvent]) -> bool:
    """Append events to the ring, dropping the oldest past `RING_MAX`."""
    rows = [
        (
            int(event.ts_ns),
            str(event.path),
            str(event.channel),
            str(event.origin),
            str(event.client),
            str(event.session),
            str(event.workspace),
        )
        for event in events
        if event.channel in CHANNELS and event.path
    ]
    if not rows:
        return True

    def work(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO heat_events (ts_ns, path, channel, origin, client, session, workspace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "DELETE FROM heat_events WHERE seq <= (SELECT MAX(seq) FROM heat_events) - ?",
            (RING_MAX,),
        )

    return _write(vault_root, work, what="events")


def record_attributed(
    vault_root: Path, signatures: Mapping[str, Sequence[int]], *, ts_ns: int
) -> bool:
    """Remember the post-write signature of our own commits, so the watcher's
    echo of them is never mistaken for an external edit."""
    rows = [
        (str(path), int(sig[0]), int(sig[1]), int(sig[2]), int(ts_ns))
        for path, sig in signatures.items()
        if len(sig) >= 3
    ]
    if not rows:
        return True

    def work(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO attributed (path, mtime_ns, ctime_ns, size, ts_ns) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "DELETE FROM attributed WHERE path IN (SELECT path FROM attributed "
            "ORDER BY ts_ns DESC, path LIMIT -1 OFFSET ?)",
            (ATTRIBUTED_MAX,),
        )

    return _write(vault_root, work, what="attributed")


def attributed_signatures(
    vault_root: Path, paths: Iterable[str]
) -> dict[str, tuple[int, int, int]]:
    """`{path: signature}` of our own recorded commits among `paths`."""
    wanted = sorted({str(path) for path in paths})
    if not wanted:
        return {}
    conn = _connect(vault_root)
    if conn is None:
        return {}
    out: dict[str, tuple[int, int, int]] = {}
    try:
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            marks = ",".join("?" * len(chunk))
            for path, mtime, ctime, size in conn.execute(
                f"SELECT path, mtime_ns, ctime_ns, size FROM attributed WHERE path IN ({marks})",  # noqa: S608
                chunk,
            ):
                out[str(path)] = (int(mtime), int(ctime), int(size))
    except sqlite3.Error:
        log.debug("heat attributed lookup failed", exc_info=True)
        return out
    finally:
        conn.close()
    return out


def note_session(vault_root: Path, mark: SessionMark) -> bool:
    """Remember a session's workspace and, when a token was minted, its last
    served thread. Bounded to `SESSIONS_MAX`, least recently seen dropped. A
    mark with no paths keeps the thread already stored: an abstention does not
    end a conversation's thread."""
    if not mark.session:
        return True
    paths = json.dumps(sorted({str(path) for path in mark.paths}))

    def work(conn: sqlite3.Connection) -> None:
        if mark.paths:
            conn.execute(
                "INSERT OR REPLACE INTO heat_sessions "
                "(session, workspace, client, paths, minted_ns, seen_ns) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    mark.session,
                    mark.workspace,
                    mark.client,
                    paths,
                    int(mark.minted_ns),
                    int(mark.seen_ns),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO heat_sessions (session, workspace, client, paths, minted_ns, seen_ns) "
                "VALUES (?, ?, ?, '[]', 0, ?) ON CONFLICT(session) DO UPDATE SET "
                "workspace = excluded.workspace, client = excluded.client, "
                "seen_ns = excluded.seen_ns",
                (mark.session, mark.workspace, mark.client, int(mark.seen_ns)),
            )
        conn.execute(
            "DELETE FROM heat_sessions WHERE session IN (SELECT session FROM heat_sessions "
            "ORDER BY seen_ns DESC, session LIMIT -1 OFFSET ?)",
            (SESSIONS_MAX,),
        )

    return _write(vault_root, work, what="session")


def purge(vault_root: Path, paths: Iterable[str]) -> bool:
    """Drop every event of deleted pages (their tombstones hid them already)."""
    gone = sorted({str(path) for path in paths})
    if not gone:
        return True

    def work(conn: sqlite3.Connection) -> None:
        for start in range(0, len(gone), 400):
            chunk = gone[start : start + 400]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM heat_events WHERE path IN ({marks})", chunk)  # noqa: S608
            conn.execute(f"DELETE FROM attributed WHERE path IN ({marks})", chunk)  # noqa: S608

    return _write(vault_root, work, what="purge")


def set_meta(vault_root: Path, values: Mapping[str, object]) -> bool:
    """Write text-valued sidecar facts (`observed_through_ns`, `seeded_at_ns`)."""
    items = [(str(key), str(value)) for key, value in values.items()]

    def work(conn: sqlite3.Connection) -> None:
        conn.executemany("INSERT OR REPLACE INTO heat_meta (key, value) VALUES (?, ?)", items)

    return _write(vault_root, work, what="meta")


class _Loaded(NamedTuple):
    token: tuple
    events: tuple[HeatEvent, ...]
    sessions: tuple[SessionMark, ...]
    meta: dict[str, str]


def _read(conn: sqlite3.Connection) -> _Loaded:
    token = _token(conn)
    events = tuple(
        HeatEvent(
            int(ts), str(path), str(channel), str(origin), str(client), str(session), str(space)
        )
        for ts, path, channel, origin, client, session, space in conn.execute(
            "SELECT ts_ns, path, channel, origin, client, session, workspace "
            "FROM heat_events ORDER BY seq"
        )
    )
    sessions: list[SessionMark] = []
    for session, space, client, paths, minted, seen in conn.execute(
        "SELECT session, workspace, client, paths, minted_ns, seen_ns FROM heat_sessions"
    ):
        try:
            listed = tuple(str(item) for item in json.loads(paths) if isinstance(item, str))
        except (TypeError, ValueError):
            listed = ()
        sessions.append(
            SessionMark(str(session), str(space), str(client), listed, int(minted), int(seen))
        )
    meta = {
        str(key): str(value) for key, value in conn.execute("SELECT key, value FROM heat_meta")
    }
    return _Loaded(token, events, tuple(sessions), meta)


def load(vault_root: Path) -> HeatProfile:
    """The persisted projection, aggregated once per sidecar token.

    One token read when nothing moved; the ring and the sessions when it did.
    Another process's writes move the token, so they are seen on the next
    read. Never raises: an unreadable sidecar is an empty profile. The state
    is `empty` or `partial` here; the fold decides the reported one
    (`with_state`).
    """
    empty = build_profile((), state="empty")
    conn = _connect(vault_root)
    if conn is None:
        return empty
    try:
        key = str(sidecar_path(vault_root))
        token = _token(conn)
        with _LOCK:
            cached = _PROFILES.get(key)
        if cached is not None and cached[0] == token:
            return cached[1]
        loaded = _read(conn)
    except sqlite3.Error:
        log.debug("heat sidecar unreadable", exc_info=True)
        return empty
    finally:
        conn.close()
    profile = build_profile(
        loaded.events,
        sessions=loaded.sessions,
        state="empty" if not loaded.events else "partial",
        salt=loaded.meta.get("salt", ""),
        token=loaded.token,
        meta=loaded.meta,
    )
    with _LOCK:
        _PROFILES[key] = (loaded.token, profile)
    return profile


def with_state(profile: HeatProfile, state: str) -> HeatProfile:
    """`profile` reporting `state`, its digest recomputed (the state is in it)."""
    if profile.state == state:
        return profile
    return _with_digest(replace(profile, state=state if state in STATES else profile.state))


# --------------------------------------------------------------------------- #
# Attribution: derived keys, never raw values (ruling S5-1)
# --------------------------------------------------------------------------- #


def derive_key(salt: str, kind: str, audience: str, value: object) -> str:
    """A caller's opaque key as the sidecar stores it, or `""` when absent or
    unusable. Salted per sidecar and scoped to the audience, so the stored
    value is never the caller's own string and a key another principal passes
    matches nothing of the owner's."""
    if not isinstance(value, str) or not value or len(value) > KEY_MAX_CHARS or not salt:
        return ""
    material = f"exomem-heat-{kind}-v1\0{salt}\0{audience}\0{value}"
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:KEY_HEX]


def _audience() -> str:
    try:
        from .governance.principal import effective_principal

        return str(effective_principal().audience_id or "")
    except Exception:  # noqa: BLE001 - an unknown audience shares nobody's keys
        return "\0unresolved"


def client_label(value: object) -> str:
    """A declared or observed client label as recorded: the label, or `""`."""
    from . import query_log

    declared = query_log.declared_client(value)
    return declared if declared not in (None, "invalid") else ""


def attribution_for(
    vault_root: Path,
    *,
    client: object = None,
    session: object = None,
    workspace: object = None,
    salt: str | None = None,
) -> Attribution:
    """The caller's derived keys, for ranking and for recording. Reads the
    sidecar's salt unless the caller holds a profile that carries it."""
    if salt is None:
        salt = load(vault_root).salt
    audience = _audience()
    return Attribution(
        session=derive_key(salt, "session", audience, session),
        workspace=derive_key(salt, "workspace", audience, workspace),
        client=client_label(client),
    )


# --------------------------------------------------------------------------- #
# The governed-commit seam (`vault.batch_atomic_write`)
# --------------------------------------------------------------------------- #

#: `{vault root: {path: depth}}`: pages a commit is flipping right now. The
#: fold defers them rather than classify a half-finished commit's echo.
_IN_FLIGHT: dict[str, dict[str, int]] = {}
#: `{vault root: {path: signature}}`: this process's own recent commits, known
#: synchronously, before the sidecar row lands. Bounded like `attributed`.
_OURS: dict[str, dict[str, tuple[int, int, int]]] = {}


class Commit(NamedTuple):
    root: Path
    paths: tuple[str, ...]


class ObservedCommit(NamedTuple):
    root: Path
    events: tuple[HeatEvent, ...]
    signatures: Mapping[str, tuple[int, int, int]]
    ts_ns: int


def _root_key(vault_root: Path) -> str:
    import os

    return os.path.abspath(str(vault_root))


def _kb_relative(vault_root: Path, target: object) -> str | None:
    """The vault-relative POSIX path of a knowledge-base Markdown page, or
    `None`. String work only: no resolve, no stat."""
    import os

    from .kbdir import kb_prefix

    root = _root_key(vault_root)
    text = os.path.abspath(os.fspath(target))  # type: ignore[arg-type]
    if not text.startswith(root + os.sep) or not text.lower().endswith(".md"):
        return None
    rel = text[len(root) + 1 :].replace(os.sep, "/")
    return rel if rel.startswith(kb_prefix()) else None


def begin_commit(vault_root: Path | None, targets: Iterable[object]) -> Commit | None:
    """Register a batch's knowledge-base pages as in flight, before any flip."""
    if vault_root is None or disabled():
        return None
    try:
        paths = tuple(
            dict.fromkeys(
                rel for target in targets if (rel := _kb_relative(vault_root, target)) is not None
            )
        )
    except Exception:  # noqa: BLE001 - the seam never fails a commit
        log.debug("heat commit registration failed", exc_info=True)
        return None
    if not paths:
        return None
    key = _root_key(vault_root)
    with _LOCK:
        flying = _IN_FLIGHT.setdefault(key, {})
        for rel in paths:
            flying[rel] = flying.get(rel, 0) + 1
    return Commit(Path(vault_root), paths)


def _land(commit: Commit) -> None:
    key = _root_key(commit.root)
    with _LOCK:
        flying = _IN_FLIGHT.get(key)
        if flying is None:
            return
        for rel in commit.paths:
            left = flying.get(rel, 0) - 1
            if left > 0:
                flying[rel] = left
            else:
                flying.pop(rel, None)
        if not flying:
            _IN_FLIGHT.pop(key, None)


def abandon_commit(commit: Commit | None) -> None:
    """A commit that failed records nothing and stops being in flight."""
    if commit is not None:
        _land(commit)


def in_flight(vault_root: Path) -> frozenset[str]:
    with _LOCK:
        return frozenset(_IN_FLIGHT.get(_root_key(vault_root), {}))


def _observed_client() -> str:
    try:
        from . import command_surface

        name = command_surface.mcp_caller_identity().get("client_name")
    except Exception:  # noqa: BLE001 - attribution is never worth a failure
        return ""
    return client_label(str(name).strip().casefold()) if name else ""


def _collection_dirs_on_disk(vault_root: Path, paths: Iterable[str]) -> frozenset[str]:
    """Which parents and grandparents of `paths` hold a `_collection.md`: at
    most two `stat`s per written page, on the write path, never the read path."""
    dirs: set[str] = set()
    for rel in paths:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for candidate in (parent, parent.rsplit("/", 1)[0] if "/" in parent else ""):
            if candidate and candidate not in dirs:
                if (Path(vault_root) / candidate / "_collection.md").is_file():
                    dirs.add(candidate)
    return frozenset(dirs)


def observe_commit(commit: Commit | None) -> ObservedCommit | None:
    """After a successful commit, synchronously: the mutation trace, the
    request's batch scope, each page's post-write signature, and the events a
    traced, unbatched commit earns. Stops the pages being in flight. Never
    raises."""
    if commit is None:
        return None
    try:
        import time

        from . import due_state, freshness, working_set, writer_lease

        trace = writer_lease.active_mutation_trace()
        batch = due_state.in_batch_scope()
        now = time.time_ns()
        signatures: dict[str, tuple[int, int, int]] = {}
        for rel in commit.paths:
            try:
                signatures[rel] = tuple(freshness.stat_signature(commit.root / rel))  # type: ignore[assignment]
            except OSError:
                continue
        events: list[HeatEvent] = []
        if trace is not None and not batch:
            collections = _collection_dirs_on_disk(commit.root, signatures)
            client = _observed_client()
            for rel in signatures:
                channel = classify_commit(
                    rel,
                    traced=True,
                    in_batch=False,
                    reason=working_set._recent_reason_for(rel, collections=collections),
                )
                if channel is not None:
                    events.append(HeatEvent(now, rel, channel, origin=trace[1], client=client))
        key = _root_key(commit.root)
        with _LOCK:
            ours = _OURS.setdefault(key, {})
            for rel, signature in signatures.items():
                ours.pop(rel, None)
                ours[rel] = signature
            while len(ours) > ATTRIBUTED_MAX:
                ours.pop(next(iter(ours)))
        return ObservedCommit(commit.root, tuple(events), signatures, now)
    except Exception:  # noqa: BLE001 - the seam never fails a commit
        log.debug("heat commit observation failed", exc_info=True)
        return None
    finally:
        _land(commit)


def persist_commit(observed: ObservedCommit | None) -> None:
    """Persist an observed commit after its terminal is durable, or inline
    when there is no mutation to defer to. Called with the commit lock
    released. Never raises."""
    if observed is None or (not observed.events and not observed.signatures):
        return

    def work() -> list[object]:
        record_attributed(observed.root, observed.signatures, ts_ns=observed.ts_ns)
        append(observed.root, observed.events)
        return []

    try:
        from . import writer_lease

        if writer_lease.defer_until_terminal_persisted(work):
            return
        work()
    except Exception:  # noqa: BLE001 - heat never fails a commit
        log.debug("heat commit persistence failed", exc_info=True)


def recent_attributed(vault_root: Path, paths: Iterable[str]) -> dict[str, tuple[int, int, int]]:
    """Our own commits' signatures among `paths`: this process's, known at
    once, then the sidecar's, which another process's commits reach."""
    wanted = list(dict.fromkeys(str(path) for path in paths))
    with _LOCK:
        ours = dict(_OURS.get(_root_key(vault_root), {}))
    out = {path: ours[path] for path in wanted if path in ours}
    missing = [path for path in wanted if path not in out]
    if missing:
        out.update(
            {
                path: signature
                for path, signature in attributed_signatures(vault_root, missing).items()
                if path not in out
            }
        )
    return out


# --------------------------------------------------------------------------- #
# The selection seams: reads, citations, admitted picks
# --------------------------------------------------------------------------- #

#: Working-context reasons a selection event may name. An episode recap is
#: offered once per conversation from its own `episode_page` rows, so a read
#: of a retired revision must not bring it back beside the newest.
_SELECTABLE = frozenset({"edited", "captured"})


def _owner_request() -> bool:
    try:
        from .governance.principal import OWNER_AUDIENCE, effective_principal

        return str(effective_principal().audience_id or "") == OWNER_AUDIENCE
    except Exception:  # noqa: BLE001 - an unknown principal is not the owner
        return False


def _released(vault_root: Path, rel: str) -> bool:
    """Whether the current caller may see `rel`. A selection is recorded only
    for a page released to its caller: otherwise trying to read a withheld
    page would heat it, and the next "continue" would say it exists."""
    if _owner_request():
        return True
    try:
        from .governance import egress

        return bool(egress.quick_page_visible(vault_root, rel))
    except Exception:  # noqa: BLE001 - undecidable is not released
        return False


def note_selection(
    vault_root: Path,
    paths: Iterable[str],
    channel: str,
    *,
    attribution: Attribution | None = None,
) -> bool:
    """Record a `read`, `cite` or `pick` of each working-context page the
    caller was released. One sqlite insert, a 50 ms busy timeout at most,
    and it never raises. Serving a packet is never a selection: only these
    three seams call this."""
    if channel not in (*SELECTION, "pick") or disabled():
        return False
    try:
        import time

        from . import working_set

        wanted = [
            rel
            for rel in dict.fromkeys(str(path) for path in paths)
            if rel and _kb_relative(vault_root, Path(vault_root) / rel) == rel
        ]
        if not wanted:
            return False
        collections = _collection_dirs_on_disk(vault_root, wanted)
        who = attribution or Attribution(client=_observed_client())
        now = time.time_ns()
        events = [
            HeatEvent(
                now,
                rel,
                channel,
                origin=channel,
                client=who.client,
                session=who.session,
                workspace=who.workspace,
            )
            for rel in wanted
            if working_set._recent_reason_for(rel, collections=collections) in _SELECTABLE
            and _released(vault_root, rel)
        ]
    except Exception:  # noqa: BLE001 - heat never fails the read it describes
        log.debug("heat selection could not be classified", exc_info=True)
        return False
    return append(vault_root, events) if events else False


def cited_paths(vault_root: Path, sources: Iterable[object]) -> list[str]:
    """The vault-relative pages a governed write's `sources` name, spelled
    the way the heat ring keys pages. Brackets, an alias and a missing
    knowledge-base prefix or `.md` are tolerated, as the writers tolerate
    them; a stable reference is resolved read-only; a value that names no
    file is dropped (one `stat` per source, on the write path)."""
    from . import memory_refs
    from .kbdir import kb_prefix

    out: list[str] = []
    for raw in sources or ():
        text = str(raw or "").strip()
        if text.startswith("[[") and text.endswith("]]"):
            text = text[2:-2].strip()
        text = text.split("|", 1)[0].strip()
        if not text:
            continue
        if text.lower().startswith(memory_refs.REF_PREFIX):
            try:
                text = memory_refs.resolve_identifier_read_only(vault_root, text)
            except Exception:  # noqa: BLE001 - an unresolvable ref names nothing
                continue
        if not text.startswith(kb_prefix()):
            text = kb_prefix() + text.lstrip("/")
        if not text.lower().endswith(".md"):
            text += ".md"
        try:
            if (Path(vault_root) / text).is_file() and text not in out:
                out.append(text)
        except OSError:
            continue
    return out


def note_citations(vault_root: Path, sources: Iterable[object]) -> bool:
    """`note_selection(..., "cite")` over a governed write's cited sources."""
    try:
        paths = cited_paths(vault_root, sources)
    except Exception:  # noqa: BLE001 - heat never fails the write it describes
        log.debug("heat citation could not be resolved", exc_info=True)
        return False
    return note_selection(vault_root, paths, "cite") if paths else False


def reset_for_tests() -> None:
    """Forget every cached profile and in-process fold state."""
    with _LOCK:
        _PROFILES.clear()
        _PARENTS.clear()
        _IN_FLIGHT.clear()
        _OURS.clear()
