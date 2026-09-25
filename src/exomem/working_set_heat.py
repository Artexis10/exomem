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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

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
        view = HeatProfile(
            events=profile.events,
            sessions=sessions,
            state=profile.state,
            session_start_ns=profile.session_start_ns,
            last_deliberate_ns=profile.last_deliberate_ns,
            window_rows=profile.window_rows,
            all_rows=profile.all_rows,
            digest=profile.digest,
        )
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
        view = HeatProfile(
            events=profile.events,
            sessions=sessions,
            state=profile.state,
            session_start_ns=profile.session_start_ns,
            last_deliberate_ns=profile.last_deliberate_ns,
            window_rows=profile.window_rows,
            all_rows=profile.all_rows,
            digest=profile.digest,
        )
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
    return HeatProfile(
        events=profile.events,
        sessions=profile.sessions,
        state=profile.state,
        session_start_ns=profile.session_start_ns,
        last_deliberate_ns=profile.last_deliberate_ns,
        window_rows=profile.window_rows,
        all_rows=profile.all_rows,
        digest=digest,
        salt=profile.salt,
        token=profile.token,
        tombstones=profile.tombstones,
    )


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
