"""Maps and profiles: read-time views over the context catalogue.

A view is a ranked list of catalogue entries that one caller may see. It is
computed per request from the catalogue's raw rows and the registries as they
stand, and nothing it interprets is stored.

The caller's view comes first. Every row is filtered by what the caller may
see BEFORE it is ordered, counted, paged, hashed or compared with a pending
change, so no entry the caller cannot see moves a byte the caller receives.
`read_memory` serves a view through `read_view`; the activation packet will
serve the map through `compile_view` and `map_within`.

Map and profile refs share one namespace contract. `read_view` pages a view
with a signed continuation token that binds the view, the caller and the
generation.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import (
    activation_conventions,
    context_catalogue,
    context_roles,
    lifecycle_statuses,
    project_keys,
    source_taxonomy,
    vocabulary_resolution,
    working_set,
    working_set_index,
    working_set_resolve,
    working_set_state,
)
from .activation_result import INDEX_WARMING
from .context_catalogue import KIND_ANCHOR, KIND_COLLECTION, KIND_THREAD
from .governance import egress
from .governance import principal as principal_module
from .working_set_resolve import AnchorFacts, ResolvedAnchor, anchor_ref, anchor_visible
from .working_set_runtime import READY, UNAVAILABLE

MAP_REF = "exomem://map"
UNFILED_REF = "exomem://map/unfiled"
TOPIC_REF_PREFIX = "exomem://map/topic/"
PROFILE_REF_PREFIX = "exomem://profile/"
ANCHOR_PROFILE_PREFIX = PROFILE_REF_PREFIX + "anchor/"
TOPIC_PROFILE_PREFIX = PROFILE_REF_PREFIX + "topic/"

#: The map kinds, a closed set the refs above fix.
VIEW_MAP = "map"
VIEW_TOPIC = "topic"
VIEW_UNFILED = "unfiled"
# Profile kinds are the two ref shapes fixed by the read contract.
PROFILE_ANCHOR = "anchor_profile"
PROFILE_TOPIC = "topic_profile"

#: The row kind of a topic. The other row kinds are the catalogue's entry kinds.
KIND_TOPIC = "topic"
#: Where a topic row's description came from when the domain registry supplied it.
ORIGIN_REGISTRY = "registry"

#: Items one response carries. The spec fixes it.
VIEW_PAGE_SIZE = 40
#: How long a continuation token stays valid. A resource bound: a token is a place in a
#: view that moves, and 15 minutes matches the governed recall continuation.
CONTINUATION_TTL_SECONDS = 900
#: Longest continuation token accepted. A resource bound on untrusted input, set well above
#: the token this module mints.
CONTINUATION_MAX_CHARS = 1024
#: Longest topic key a ref may carry. A resource bound on untrusted input; registry keys are short slugs.
TOPIC_KEY_MAX_CHARS = 128
#: Bound encoded anchor refs before decoding untrusted input; long vault paths still fit.
ANCHOR_REF_MAX_CHARS = 8192
#: Bound topic lane fan-in so one topic cannot run lanes over the whole anchor catalogue.
PROFILE_MAX_ANCHORS = 12
#: Bound topic note reads while keeping cold declared members reachable by domain map.
PROFILE_MAX_NOTES = 80
#: Bound resident owner profiles, including their last published generations during refresh.
PROFILE_CACHE_SIZE = 32

#: Each row kind's share of a map budget, so one kind cannot crowd the others out of the
#: lines `map_within` returns. A tunable split of a character budget, keyed by the closed
#: set of kinds a map row has; it names no vault vocabulary.
MAP_KIND_SHARES: dict[str, float] = {
    KIND_TOPIC: 0.3,
    KIND_ANCHOR: 0.3,
    KIND_COLLECTION: 0.2,
    KIND_THREAD: 0.2,
}

_TOKEN_VERSION = 1
_NOT_FOUND = "NOT_FOUND: memory identifier is unavailable"


def _clock() -> float:
    """The time a token is minted and checked against. A seam: tests freeze it to compare bytes."""
    return time.time()


# --------------------------------------------------------------------------- #
# Refs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ViewTarget:
    """What a ref names: a view kind and its topic key or anchor ref as written."""

    kind: str
    key: str = ""

    @property
    def profile(self) -> bool:
        return self.kind in (PROFILE_ANCHOR, PROFILE_TOPIC)


def in_view_namespace(ref: str) -> bool:
    """Whether `ref` belongs to the views. A ref that does but names no view is NOT_FOUND."""
    return ref == MAP_REF or ref.startswith((MAP_REF + "/", PROFILE_REF_PREFIX))


def parse_view_ref(ref: str) -> ViewTarget | None:
    """The view a ref names, or None when it matches none of the supported ref shapes."""
    if ref == MAP_REF:
        return ViewTarget(VIEW_MAP)
    if ref == UNFILED_REF:
        return ViewTarget(VIEW_UNFILED)
    for prefix, kind in ((TOPIC_REF_PREFIX, VIEW_TOPIC), (TOPIC_PROFILE_PREFIX, PROFILE_TOPIC)):
        if not ref.startswith(prefix):
            continue
        key = ref[len(prefix):]
        if key and len(key) <= TOPIC_KEY_MAX_CHARS and "/" not in key:
            return ViewTarget(kind, key)
    if ref.startswith(ANCHOR_PROFILE_PREFIX):
        encoded = ref[len(ANCHOR_PROFILE_PREFIX):]
        if encoded and len(encoded) <= ANCHOR_REF_MAX_CHARS and "/" not in encoded:
            try:
                key = unquote(encoded, errors="strict")
            except UnicodeError:
                return None
            if key and "\0" not in key:
                return ViewTarget(PROFILE_ANCHOR, key)
    return None


def topic_ref(key: str) -> str:
    return TOPIC_REF_PREFIX + key


def anchor_profile_ref(ref: str) -> str:
    return ANCHOR_PROFILE_PREFIX + quote(ref, safe="")


def topic_profile_ref(key: str) -> str:
    return TOPIC_PROFILE_PREFIX + key


def not_found() -> ValueError:
    """The refusal for a ref that names no view this caller can see.

    It reads exactly as an identifier that resolves to nothing does, so a miss
    on a view says nothing a miss on a page would not.
    """
    return ValueError(_NOT_FOUND)


def invalid_continuation() -> ValueError:
    """The one INVALID_CONTINUATION refusal, shared with governed recall."""
    from .governance import projection_runtime

    return projection_runtime._invalid_continuation()  # noqa: SLF001 - one refusal text for every continuation


# --------------------------------------------------------------------------- #
# The caller's view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CallerView:
    """What one request may see, built once and used for every decision after it."""

    #: `keep(vault_relative_path)`, or None for the owner, who sees every entry.
    visible: Callable[[str], bool] | None
    purpose: str | None
    #: Digest of (audience id, authorization session id, declared purpose): who a token is for.
    binding: str

    @property
    def restricted(self) -> bool:
        return self.visible is not None

    def sees(self, path: str) -> bool:
        return self.visible is None or bool(self.visible(path))

    @classmethod
    def current(cls, vault_root: Path) -> CallerView:
        who = principal_module.effective_principal()
        # The purpose the release decision itself takes, so the filter and the binding agree.
        purpose = egress._declared_purpose(Path(vault_root), who, None)  # noqa: SLF001
        visible = egress.restricted_release_filter(Path(vault_root), purpose=purpose)
        binding = _digest(json.dumps([who.audience_id, who.authorization_session_id, purpose]))
        return cls(visible, purpose, binding)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #


class _Registries:
    """The two registries a view reads, loaded once per request."""

    def __init__(self, vault_root: Path) -> None:
        self.taxonomy = source_taxonomy.load_taxonomy(vault_root)
        self.projects = project_keys.load_project_registry(vault_root)
        self._digest: str | None = None

    def topic(self, raw: str) -> str | None:
        """The domain key `raw` resolves to (an alias, or a deprecated key's replacement), or None."""
        resolved = vocabulary_resolution.resolve_topics(self.taxonomy, (raw,)).resolved
        return resolved[0] if resolved else None

    @property
    def digest(self) -> str:
        """Identity of what the memberships and the topic rows read: the domains, their aliases, the project keys."""
        if self._digest is None:
            domains = [self.taxonomy.domains[key].as_dict() for key in sorted(self.taxonomy.domains)]
            aliases = sorted(self.taxonomy.domain_aliases.items())
            payload = json.dumps([domains, aliases, self.projects.content_hash], sort_keys=True)
            self._digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self._digest


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Row:
    """One line of a view, in the fields a caller is served."""

    entry_id: str
    kind: str
    anchor_kind: str
    title: str
    description: str
    description_origin: str
    ref: str
    path: str
    topics: tuple[str, ...]
    lifecycle_class: str | None
    activity: str

    def item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "description_origin": self.description_origin,
            "ref": self.ref,
            "topics": list(self.topics),
            "lifecycle_class": self.lifecycle_class,
            "activity": self.activity or None,
        }
        if self.anchor_kind:
            item["anchor_kind"] = self.anchor_kind
        if self.path:
            item["path"] = self.path
        return item

    def digest(self) -> str:
        """Identity of what is served for this row. It leaves out every stored field a caller never receives."""
        return _digest(json.dumps([self.entry_id, self.item()], sort_keys=True))


@dataclass(frozen=True, slots=True)
class ProfileLine:
    """One lane result in the paged read contract; its kind names the packet section."""

    fields: dict[str, Any]

    def item(self) -> dict[str, Any]:
        return copy.deepcopy(self.fields)


@dataclass(frozen=True, slots=True)
class View:
    """A compiled view. `state` is READY, or why there is none; then `generation` is None and `rows` empty."""

    ref: str
    state: str
    generation: str | None
    stale: bool
    rows: tuple[Row | ProfileLine, ...]
    #: How far the membership graph is built. Owner-only: a restricted caller is never told.
    graph: str | None = None


def _recency(activity: str) -> tuple[int, object]:
    return working_set_index._member_recency_key(activity)  # noqa: SLF001 - the activation index's own date order


def _newest(left: str, right: str) -> str:
    return right if _recency(right) > _recency(left) else left


def _ordered(rows: Iterable[Row]) -> list[Row]:
    """Newest activity first; ties by ref, then entry id. Two stable sorts: the second keeps the first's tie order."""
    out = sorted(rows, key=lambda row: (row.ref, row.entry_id))
    out.sort(key=lambda row: _recency(row.activity), reverse=True)
    return out


def _carryable(row: Row) -> bool:
    """Whether the map carries an entry: its lifecycle class is live or pending. An unclassified one is not."""
    return row.lifecycle_class is not None and lifecycle_statuses.Classification(row.lifecycle_class).carryable


def _admits(entry: context_catalogue.Entry, sees: Callable[[str], bool] | None) -> bool:
    """Whether the caller may see an entry. Anchors, collections and pages share one rule."""
    if sees is None:
        return True
    facts = AnchorFacts(
        anchor_id=entry.entry_id,
        path=entry.path,
        ref=entry.ref,
        title=entry.title,
        kind=entry.anchor_kind,
        lifecycle=entry.status,
        aliases=(),
        terms=(),
        categories=(),
        neighbourhood=frozenset(),
    )
    return anchor_visible(facts, sees)


def _row(entry: context_catalogue.Entry, topics: tuple[str, ...], activity: str) -> Row:
    return Row(
        entry_id=entry.entry_id,
        kind=entry.kind,
        anchor_kind=entry.anchor_kind,
        title=entry.title,
        description=entry.description,
        description_origin=entry.description_origin,
        ref=entry.ref or entry.path or entry.entry_id,
        path=entry.path,
        topics=topics,
        lifecycle_class=entry.lifecycle_class,
        activity=activity,
    )


def _topic_row(key: str, registries: _Registries, activity: str) -> Row:
    definition = registries.taxonomy.domains.get(key)
    description = (
        working_set_state.bounded_statement(definition.description, working_set_index.LEDE_MAX_CHARS)
        if definition is not None
        else ""
    )
    return Row(
        entry_id=f"{KIND_TOPIC}:{key}",
        kind=KIND_TOPIC,
        anchor_kind="",
        title=(definition.label if definition is not None and definition.label else key),
        description=description,
        description_origin=ORIGIN_REGISTRY if description else context_catalogue.ORIGIN_NONE,
        ref=topic_ref(key),
        path="",
        topics=(),
        lifecycle_class=None,
        activity=activity,
    )


@dataclass(frozen=True, slots=True)
class _World:
    """What one caller can see of the catalogue: its rows, each with the topics it joins."""

    #: Visible rows, one per page, collection or anchor; a thread shows only its newest visible revision.
    rows: tuple[Row, ...]
    #: Newest activity among the rows of each topic the caller can see. A topic with no visible row is absent.
    topic_activity: dict[str, str]


def _world(catalogue: context_catalogue.ContextCatalogue, caller: CallerView) -> _World:
    entries = [entry for entry in catalogue.entries() if _admits(entry, caller.visible)]
    # The join runs over the visible entries only: a relation to an entry the caller cannot see adds nothing.
    joined = catalogue.memberships(entries).topics
    # An entry with no activity of its own (a project anchor) takes its project's newest visible page.
    project_activity: dict[str, str] = {}
    for entry in entries:
        for key in entry.projects:
            project_activity[key] = _newest(project_activity.get(key, ""), entry.activity)
    heads: dict[str, context_catalogue.Entry] = {}
    served = []
    for entry in entries:
        if entry.kind != KIND_THREAD:
            served.append(entry)
            continue
        head = heads.get(entry.thread_key)
        if head is None or (entry.thread_order, entry.entry_id) > (head.thread_order, head.entry_id):
            heads[entry.thread_key] = entry
    served.extend(heads.values())
    rows: list[Row] = []
    topic_activity: dict[str, str] = {}
    for entry in served:
        activity = entry.activity
        if not activity:
            for key in entry.projects:
                activity = _newest(activity, project_activity.get(key, ""))
        topics = joined.get(entry.entry_id, ())
        for key in topics:
            topic_activity[key] = _newest(topic_activity.get(key, ""), activity)
        rows.append(_row(entry, topics, activity))
    return _World(tuple(rows), topic_activity)


def _view_rows(target: ViewTarget, key: str, world: _World, registries: _Registries) -> list[Row]:
    if target.kind == VIEW_MAP:
        topics = _ordered(_topic_row(topic, registries, activity) for topic, activity in world.topic_activity.items())
        # The map lists the kinds that have a share of its budget; an ordinary note is reached through a domain map.
        return topics + _ordered(row for row in world.rows if row.kind in MAP_KIND_SHARES and _carryable(row))
    if target.kind == VIEW_UNFILED:
        return _ordered(row for row in world.rows if not row.topics)
    return _ordered(row for row in world.rows if key in row.topics)


# --------------------------------------------------------------------------- #
# Compiling a view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Prepared:
    target: ViewTarget
    #: The ref the view is known by: a deprecated or aliased topic key reads as its replacement.
    ref: str
    key: str
    registries: _Registries
    caller: CallerView
    state: str
    catalogue: context_catalogue.ContextCatalogue | None
    unlisted: bool


def _prepare(vault_root: Path, path: str, caller: CallerView | None) -> _Prepared:
    """Resolve the ref and bring the catalogue forward. Reads no entry."""
    target = parse_view_ref(path)
    if target is None:
        raise not_found()
    root = Path(vault_root)
    registries = _Registries(root)
    key = ""
    ref = path
    if target.kind in (VIEW_TOPIC, PROFILE_TOPIC):
        resolved = registries.topic(target.key)
        if resolved is None:
            raise not_found()
        key = resolved
        ref = topic_profile_ref(resolved) if target.profile else topic_ref(resolved)
    elif target.kind == PROFILE_ANCHOR:
        key, ref = target.key, anchor_profile_ref(target.key)
    state, catalogue, unlisted = context_catalogue.ensure_unlisted(root)
    return _Prepared(target, ref, key, registries, caller or CallerView.current(root), state, catalogue, unlisted)


def _abstention(prepared: _Prepared, state: str) -> View:
    return View(prepared.ref, state, None, False, ())


def _usable(prepared: _Prepared) -> View | None:
    """The abstention that stands in for the view when it cannot be served yet, else None."""
    if prepared.state != READY or prepared.catalogue is None:
        return _abstention(prepared, prepared.state)
    if prepared.caller.restricted and prepared.unlisted:
        # The catalogue is behind by work it cannot list, so it cannot say whether that work
        # touches a page this caller may see. It answers warming, never a guess at stale.
        return _abstention(prepared, INDEX_WARMING)
    return None


def _compile(prepared: _Prepared) -> View:
    if prepared.target.profile:
        return _compile_profile(prepared)
    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None  # `_usable` returned None
    world = _world(catalogue, caller)
    rows = _view_rows(prepared.target, prepared.key, world, prepared.registries)
    if not rows:
        raise not_found()
    digest = hashlib.sha256(prepared.registries.digest.encode("ascii"))
    for row in rows:
        digest.update(row.digest().encode("ascii"))
    pending = catalogue.pending()
    stale = prepared.unlisted or any(caller.sees(path) for path in pending)
    graph = None if caller.restricted else catalogue.graph_state()
    return View(prepared.ref, READY, digest.hexdigest(), stale, tuple(rows), graph)


def compile_view(vault_root: Path, path: str, *, caller: CallerView | None = None) -> View:
    """The view `path` names, for `caller` (default: the bound principal).

    Raises NOT_FOUND for a ref that names no view this caller can see.
    """
    prepared = _prepare(vault_root, path, caller)
    return _usable(prepared) or _compile(prepared)


# --------------------------------------------------------------------------- #
# Profiles through the existing role lanes
# --------------------------------------------------------------------------- #


_PROFILE_CACHE: OrderedDict[tuple, View] = OrderedDict()
_PROFILE_LOCK = threading.RLock()


def _cached_profile(prefix: tuple, basis: lifecycle_statuses.Basis) -> View | None:
    """Find an owner build whose lifecycle witnesses still hold. The caller holds the lock."""
    for key in reversed(_PROFILE_CACHE):
        if key[:len(prefix)] == prefix and basis.matches(key[-1]):
            _PROFILE_CACHE.move_to_end(key)
            return copy.deepcopy(_PROFILE_CACHE[key])
    return None


def _profile_anchors(
    root: Path, rows: Sequence[Row], caller: CallerView
) -> tuple[ResolvedAnchor, ...]:
    """Use the activation index's facts, with an explicit ref read resolving each chosen anchor."""
    index = working_set_index.WorkingSetIndex(root)
    try:
        indexed = {row.anchor_id: row for row in index.anchors()}
        return tuple(
            ResolvedAnchor(
                anchor_id=row.anchor_id, path=row.path, ref=row.ref, title=row.title,
                kind=row.kind, lifecycle=row.lifecycle, status="resolved", evidence=(),
                categories=row.categories,
                neighbourhood=frozenset(path for path in row.neighbourhood if caller.sees(path)),
                anchor_neighbourhood=frozenset(path for path in row.anchor_neighbourhood if caller.sees(path)),
                entity_type=row.entity_type,
            )
            for chosen in rows
            if (row := indexed.get(chosen.entry_id)) is not None
        )
    finally:
        index.close()


def _compile_profile(prepared: _Prepared) -> View:
    from . import find

    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None
    root = catalogue.vault_root
    token = catalogue.token()
    world = _world(catalogue, caller)
    if prepared.target.kind == PROFILE_ANCHOR:
        members = _ordered(
            row for row in world.rows
            if row.kind in (KIND_ANCHOR, KIND_COLLECTION) and prepared.key in (row.ref, row.path, row.entry_id)
        )
    else:
        members = _ordered(row for row in world.rows if prepared.key in row.topics)
    if not members:
        raise not_found()
    pending = catalogue.pending()
    stale = prepared.unlisted or any(caller.sees(path) for path in pending)
    basis = lifecycle_statuses.Basis(root)
    # Restricted callers never inspect or fill the owner's cache, even for a stale build.
    if stale and not caller.restricted:
        with _PROFILE_LOCK:
            previous = _cached_profile((str(root.absolute()), prepared.ref), basis)
        if previous is not None:
            return replace(previous, stale=True)

    registry = context_roles.load_roles(root)
    conventions = activation_conventions.load_conventions(root)
    snapshot = find.FreshnessSnapshot(root)
    prefix = (
        str(root.absolute()), prepared.ref, token, snapshot.projection_key("kb"),
        registry.roles_hash, conventions.conventions_hash,
    )
    if not stale and not caller.restricted:
        with _PROFILE_LOCK:
            previous = _cached_profile(prefix, basis)
        if previous is not None:
            return previous

    anchor_rows = [row for row in members if row.kind in (KIND_ANCHOR, KIND_COLLECTION)]
    neighbourhood = None
    if prepared.target.kind == PROFILE_TOPIC:
        anchor_rows = [row for row in anchor_rows if _carryable(row)][:PROFILE_MAX_ANCHORS]
        # Members were filtered and ordered before either resource bound applies.
        neighbourhood = frozenset(
            [row.path for row in members if row.kind == context_catalogue.KIND_NOTE][:PROFILE_MAX_NOTES]
        )
    anchors = _profile_anchors(root, anchor_rows, caller)
    analysis = working_set_resolve.analyze_turn("", vocabulary=conventions.conventions.referential)
    roles = context_roles.select_roles(registry, anchor_kinds=[row.kind for row in anchors], analysis=analysis)
    if prepared.target.kind == PROFILE_TOPIC and not anchors:
        # Declared notes have no anchor kind; the existing page selector uses their visible categories.
        by_page = working_set._unit_categories(root, tuple(neighbourhood or ()), caller.visible)
        categories = None if by_page is None else frozenset(category for found in by_page.values() for category in found)
        roles = working_set._carry_roles(registry, analysis, categories)
    state = working_set_state.current_state_for(
        root, anchors=anchors, visible=caller.visible, purpose=caller.purpose, status_basis=basis,
        state_fields=conventions.conventions.state_fields, date_fields=conventions.conventions.date_fields,
    )
    items, missing = working_set.run_lanes(
        root, anchors=anchors, roles=roles, registry=registry, current_state=state,
        visible=caller.visible, neighbourhood=neighbourhood, freshness_snapshot=snapshot,
        analysis=analysis, status_basis=basis,
    )
    if any(entry.get("reason") == "lane_failed" for entry in missing):
        # A failed lane leaves knowledge unknown; warming costs a retry instead of silently serving omissions.
        return _abstention(prepared, INDEX_WARMING)
    # The packet builder owns currency, deduplication, role caps and whole-unit budgets.
    packet = working_set.build_packet(
        # A profile serves page candidates by ref; only semantic units carry authored prose and dates.
        items=tuple(replace(item, text="") if item.level == "page" else item for item in items),
        anchors=(), roles=roles, current_state=state, ambiguity=(), missing=missing,
        max_chars=working_set.MAX_BUDGET_CHARS, generation={}, status="resolved",
        status_basis=basis, visible=caller.visible,
    )
    if packet.get("abstained"):
        return _abstention(prepared, UNAVAILABLE)
    pages = {item.ref for item in items if item.level == "page"}
    for pointer in packet["pointers"]:
        if pointer["ref"] in pages:
            pointer["reason"] = "requires_read"
    rows = tuple(
        ProfileLine({"kind": section, **item})
        for section in ("current_state", "units", "pointers")
        for item in packet[section]
    )
    generation = _digest(json.dumps([prepared.ref, prepared.registries.digest, [row.item() for row in rows]], sort_keys=True))
    view = View(prepared.ref, READY, generation, stale, rows)
    with _PROFILE_LOCK:
        current = catalogue.token()
        if current != token:
            # A concurrent write owns publication now; the losing build never reaches a caller or the cache.
            previous = None if caller.restricted else _cached_profile(
                (str(root.absolute()), prepared.ref, current), basis
            )
            if previous is not None:
                return previous
            previous = None if caller.restricted else _cached_profile((str(root.absolute()), prepared.ref), basis)
            return replace(previous, stale=True) if previous is not None else _abstention(prepared, INDEX_WARMING)
        if not stale and not caller.restricted:
            _PROFILE_CACHE[(*prefix, basis.dependency)] = copy.deepcopy(view)
            _PROFILE_CACHE.move_to_end((*prefix, basis.dependency))
            while len(_PROFILE_CACHE) > PROFILE_CACHE_SIZE:
                _PROFILE_CACHE.popitem(last=False)
    return view


# --------------------------------------------------------------------------- #
# The activation map's budget
# --------------------------------------------------------------------------- #


def _line(row: Row) -> str:
    text = f"{row.kind} {row.title}"
    if row.description:
        text += f": {row.description}"
    return f"{text} [{row.ref}]"


def map_within(view: View, max_chars: int) -> tuple[str, ...]:
    """The map as whole lines that fit `max_chars`, ending with a line that names the domain maps for the rest.

    Each row kind first gets its share of the budget (`MAP_KIND_SHARES`), in map order, stopping at
    its first line that does not fit; what is left then goes to the next lines in map order. A line is
    never cut. The last line is always present: the map carries no note and no retired entry, so it
    points at the domain maps that do. Returns nothing when even that line does not fit.
    """
    rows = list(view.rows)

    def tail(shown: int) -> str:
        return (
            f"Not shown: {len(rows) - shown}. Domain maps: {TOPIC_REF_PREFIX}<domain key> for one domain, "
            f"{UNFILED_REF} for entries with no domain."
        )

    # A line costs its newline too, and the tail is sized for the largest count it can name.
    reserve = len(tail(0)) + 1
    budget = max_chars + 1 - reserve
    if budget < 0:
        return ()
    lines = [_line(row) for row in rows]
    chosen: set[int] = set()
    used = 0
    for kind, share in MAP_KIND_SHARES.items():
        allowance, spent = int(budget * share), 0
        for index, row in enumerate(rows):
            if row.kind != kind:
                continue
            cost = len(lines[index]) + 1
            if spent + cost > allowance:
                break
            chosen.add(index)
            spent += cost
        used += spent
    for index in range(len(rows)):
        if index in chosen:
            continue
        cost = len(lines[index]) + 1
        if used + cost > budget:
            break
        chosen.add(index)
        used += cost
    shown = [lines[index] for index in sorted(chosen)]
    return (*shown, tail(len(shown)))


# --------------------------------------------------------------------------- #
# The continuation token
# --------------------------------------------------------------------------- #


# A token is lower-case hex. The egress scrubber reads a long mixed-case run as a credential
# and would blank the token on its way out; a single-case run it leaves alone.
def _sign(secret: bytes, body: str) -> str:
    return hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()


def _mint(secret: bytes, ref: str, caller: CallerView, generation: str, offset: int) -> str:
    payload = {
        "v": _TOKEN_VERSION,
        "view": _digest(ref),
        "caller": caller.binding,
        "generation": generation,
        "offset": offset,
        "minted": int(_clock()),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8").hex()
    return f"{body}.{_sign(secret, body)}"


def _open(secret: bytes, token: str, ref: str, caller: CallerView) -> tuple[str, int]:
    """The generation and offset a token was minted for this view and caller, else INVALID_CONTINUATION."""
    try:
        if len(token) > CONTINUATION_MAX_CHARS:
            raise ValueError("token too long")
        body, dot, mac = token.partition(".")
        if not dot or not hmac.compare_digest(mac.encode("utf-8", "replace"), _sign(secret, body).encode("ascii")):
            raise ValueError("bad signature")
        payload = json.loads(bytes.fromhex(body))
        generation, offset, minted = payload["generation"], payload["offset"], payload["minted"]
        if (
            payload["v"] != _TOKEN_VERSION
            or not isinstance(generation, str)
            or type(offset) is not int
            or type(minted) is not int
            or offset < 0
            or payload["caller"] != caller.binding
            or payload["view"] != _digest(ref)
        ):
            raise ValueError("token is not for this view and caller")
        if not 0 <= _clock() - minted <= CONTINUATION_TTL_SECONDS:
            raise ValueError("token expired")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise invalid_continuation() from error
    return generation, offset


# --------------------------------------------------------------------------- #
# Serving a page
# --------------------------------------------------------------------------- #


def _states(vault_root: Path, rows: Sequence[Row], caller: CallerView) -> dict[str, dict[str, Any]]:
    """Current state for the anchors and collections among `rows`, by entry id.

    Only the rows served are asked, and each is asked over the caller's own view.
    """
    wanted = {row.entry_id for row in rows if row.kind in (KIND_ANCHOR, KIND_COLLECTION)}
    if not wanted:
        return {}
    index = working_set_index.WorkingSetIndex(vault_root)
    try:
        anchors = [anchor for anchor in index.anchors() if anchor.anchor_id in wanted]
        entries = working_set_state.current_state_for(
            vault_root,
            anchors=anchors,
            purpose=caller.purpose,
            visible=caller.visible,
            index_generation=index.token()[1],
            index_token=index.token(),
        )
    finally:
        index.close()
    entry_of_ref = {anchor_ref(anchor): anchor.anchor_id for anchor in anchors}
    return {
        entry_of_ref[str(entry["anchor"])]: {
            key: entry[key] for key in ("statement", "as_of", "source", "path") if entry.get(key) is not None
        }
        for entry in entries
        if str(entry.get("anchor")) in entry_of_ref
    }


def read_view(vault_root: Path, path: str, *, continuation: str | None = None) -> dict[str, Any]:
    """Serve one page of a view, for the bound principal.

    The response carries the ref, the generation, a stale flag, at most `VIEW_PAGE_SIZE` items and a
    continuation for the next page. A view that cannot be served yet answers with its readiness state
    and no items. Raises NOT_FOUND, INVALID_CONTINUATION or CONTINUATION_STALE.
    """
    root = Path(vault_root)
    prepared = _prepare(root, path, None)
    abstained = _usable(prepared)
    if abstained is not None:
        return _response(abstained, [], None)
    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None
    offset, minted_for = 0, None
    if continuation is not None:
        secret = catalogue.secret()
        if not secret:
            return _response(_abstention(prepared, UNAVAILABLE), [], None)
        minted_for, offset = _open(secret, continuation, prepared.ref, caller)
    view = _compile(prepared)
    if minted_for is not None and minted_for != view.generation:
        raise ValueError("CONTINUATION_STALE: the view changed since this continuation was issued")
    page = view.rows[offset : offset + VIEW_PAGE_SIZE]
    following = offset + len(page)
    token = None
    if following < len(view.rows):
        secret = catalogue.secret()
        if not secret:
            return _response(_abstention(prepared, UNAVAILABLE), [], None)
        assert view.generation is not None
        token = _mint(secret, prepared.ref, caller, view.generation, following)
    items = [row.item() for row in page]
    extra: dict[str, Any] = {}
    if prepared.target.kind == VIEW_MAP:
        try:
            states = _states(root, page, caller)
        except (egress.ReaderViewUnavailable, lifecycle_statuses.ClassificationUnavailable):
            states = {}
            extra["state_available"] = False
        for row, item in zip(page, items, strict=True):
            item["state"] = states.get(row.entry_id)
    return _response(view, items, token, extra)


def _response(
    view: View, items: list[dict[str, Any]], continuation: str | None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ref": view.ref,
        "state": view.state,
        "generation": view.generation,
        "stale": view.stale,
        "items": items,
        "continuation": continuation,
    }
    if view.graph is not None:
        out["membership_graph"] = view.graph
    out.update(extra or {})
    return out
