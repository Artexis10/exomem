"""The map and the domain maps: read-time views over the context catalogue.

A view is a ranked list of catalogue entries that one caller may see. It is
computed per request from the catalogue's raw rows and the registries as they
stand, and nothing it interprets is stored.

The caller's view comes first. Every row is filtered by what the caller may
see BEFORE it is ordered, counted, paged, hashed or compared with a pending
change, so no entry the caller cannot see moves a byte the caller receives.
`read_memory` serves a view through `read_view`; activation serves a budgeted
map through `activation_packet` and `map_within`. A compile records what it
served in its own invocation (`served_items`), and each pass of the terminal
guard serves that record again (`pinned_items`), never the items it is handed.
So both passes serve the text that the generation was computed from.

A caller that may not read the domain registry gets nothing from it: no topic,
no topic key and no domain map. Every topic ref then reads as an unknown one.
A caller that may not read the project registry gets no project anchor and no
domain filed through a project key.

Map and profile refs share the view namespace. `read_view` pages a view
with a signed continuation token that binds the view, the caller and the
generation.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import (
    activation_conventions,
    context_catalogue,
    context_roles,
    find_types,
    lifecycle_statuses,
    project_keys,
    source_taxonomy,
    state_paths,
    vocabulary_resolution,
    working_set,
    working_set_index,
    working_set_resolve,
    working_set_state,
)
from .activation_result import AMBIGUOUS, INDEX_WARMING, UNRESOLVED, WITHHELD
from .context_catalogue import KIND_ANCHOR, KIND_COLLECTION, KIND_THREAD
from .governance import egress
from .governance import principal as principal_module
from .working_set_resolve import AnchorFacts, ResolvedAnchor, anchor_ref, anchor_visible
from .working_set_runtime import CACHE_SIZE, READY, UNAVAILABLE

MAP_REF = "exomem://map"
UNFILED_REF = "exomem://map/unfiled"
TOPIC_REF_PREFIX = "exomem://map/topic/"
PROFILE_REF_PREFIX = "exomem://profile/"
ANCHOR_PROFILE_PREFIX = PROFILE_REF_PREFIX + "anchor/"
TOPIC_PROFILE_PREFIX = PROFILE_REF_PREFIX + "topic/"
# Profile sections are the existing packet sections fixed by the read contract.
PROFILE_SECTIONS = ("current_state", "units", "pointers")
# The two reasons a profile marker may give: the working-set contract's truncation and disclosure notices.
MISSING_TRUNCATED = "lane_truncated"
MISSING_WITHHELD = "withheld"
# The two profile kinds are the closed ref shapes above.
PROFILE_ANCHOR = "anchor_profile"
PROFILE_TOPIC = "topic_profile"

#: The three kinds of view, a closed set the refs above fix.
VIEW_MAP = "map"
VIEW_TOPIC = "topic"
VIEW_UNFILED = "unfiled"

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
#: Bound encoded anchor refs before decoding untrusted input.
ANCHOR_REF_MAX_CHARS = 8192
#: Bound topic lane fan-in over the activation catalogue.
PROFILE_MAX_ANCHORS = 12
#: Bound declared note reads while domain maps keep colder members reachable.
PROFILE_MAX_NOTES = 80
#: Bound diagnostic generations retained to avoid logging each failed read.
PROFILE_FAILURE_GENERATIONS = 128

#: Each row kind's share of a map budget, so one kind cannot crowd the others out of the
#: lines `map_within` returns. A tunable split of a character budget, keyed by the closed
#: set of kinds a map row has; it names no vault vocabulary.
MAP_KIND_SHARES: dict[str, float] = {
    KIND_TOPIC: 0.3,
    KIND_ANCHOR: 0.3,
    KIND_COLLECTION: 0.2,
    KIND_THREAD: 0.2,
}

#: The share of an activation packet's budget that the turn slice holds back while the slice is on.
#: The map then always compiles into what is left, so it is the same on every turn. The slice is on
#: by default (design D13), and `EXOMEM_ACTIVATION_TURN_SLICE=off` is the rollback. A budget whose
#: share cannot hold one line holds back nothing (`_slice_reserve`).
TURN_SLICE_SHARE = 0.25

#: The key a turn slice's record is kept under. It names no view and is never served.
SLICE_REF = "exomem://turn-slice"
SLICE_DESCRIPTION = (
    "What this turn's words reached, with the evidence. Not checked for relevance; read a ref before relying on it."
)
#: The states of a turn slice. `ready` lists leads; `empty` is a turn that reached nothing the caller may
#: see; `skipped` is a request budget or a share with no room for a line; `unavailable` is a fault.
SLICE_EMPTY = "empty"
SLICE_SKIPPED = "skipped"
#: How a lead was reached, strongest first: the closed set of a slice item's `match`, in listing order.
MATCHES = ("resolved", "ambiguous", "carried", "partial", "named", "related")  # nosemgrep: ep-word-set -- The packet specification fixes this set.
#: The anchor statuses that make a lead, and the match each one is. The packet specification fixes both sets.
_MATCH_OF_STATUS = {  # nosemgrep: ep-word-set -- Fixed anchor statuses of the activation packet.
    "resolved": "resolved",
    "partial": "partial",
    working_set_resolve.RETRIEVAL_CARRIED_STATUS: "carried",
    working_set_resolve.RETRIEVAL_NAMED_STATUS: "named",
}

#: The current-state resolver's wire fields, with the statement first and its attribution after it.
STATE_FIELDS = ("statement", "as_of", "source", "path")  # nosemgrep: ep-word-set -- Fixed current-state wire fields.

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
    #: Whether the caller may read the domain registry. Without it no view reads the registry.
    domains_admitted: bool
    #: Whether the caller may read the project registry. Without it no view reads that registry.
    projects_admitted: bool

    @property
    def restricted(self) -> bool:
        return self.visible is not None

    def sees(self, path: str) -> bool:
        return self.visible is None or bool(self.visible(path))

    @classmethod
    def current(
        cls,
        vault_root: Path,
        *,
        purpose: str | None = None,
        principal: principal_module.RequestPrincipal | None = None,
    ) -> CallerView:
        """The view of `principal` (default: the bound one) under the `purpose` the request declared.

        The view's compile and the terminal guard both build it from the same two arguments, so
        they decide under one purpose, one filter and one registry admission.
        """
        root = Path(vault_root)
        who = principal if principal is not None else principal_module.effective_principal()
        # The purpose the release decision itself takes, so the filter and the binding agree.
        declared = egress._declared_purpose(root, who, purpose)  # noqa: SLF001
        visible = egress.restricted_release_filter(root, principal=principal, purpose=declared)
        binding = _digest(json.dumps([who.audience_id, who.authorization_session_id, declared]))
        # The project registry is a vault file like any other: a restricted caller reads it only at full release.
        projects_admitted = visible is None or egress.release_level_for_path_only(
            root, project_keys.registry_relpath(), principal=principal, purpose=declared
        ) >= egress.LEVEL_FULL
        return cls(visible, declared, binding, _domains_admitted(root, principal, purpose), projects_admitted)


def _domains_admitted(
    vault_root: Path, principal: principal_module.RequestPrincipal | None, purpose: str | None
) -> bool:
    """Whether the caller may read the domain registry: the admission that `schema_memory` asks for."""
    from .vocabulary.contract import selected_admission_refusal

    if principal is None and purpose is None:
        return selected_admission_refusal(vault_root, source_taxonomy.DOMAIN_SPEC) is None
    # The admission reads the bound principal, so it is bound with the purpose, as `read_view` binds it.
    who = principal if principal is not None else principal_module.effective_principal()
    with principal_module.request_scope(who.with_purpose(purpose)):
        return selected_admission_refusal(vault_root, source_taxonomy.DOMAIN_SPEC) is None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #


class _Registries:
    """The domain registry a view reads, loaded once per request."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self.taxonomy = source_taxonomy.load_taxonomy(vault_root)
        self._digest: str | None = None

    def topic(self, raw: str) -> str | None:
        """The domain key `raw` resolves to (an alias, or a deprecated key's replacement), or None."""
        resolved = vocabulary_resolution.resolve_topics(self.taxonomy, (raw,)).resolved
        return resolved[0] if resolved else None

    @property
    def digest(self) -> str:
        """Identity of what the memberships and the topic rows read: the domains, their aliases, the project keys.

        Only the owner's map cache keys on it, and only an owner's request reads it, because it reads the
        project registry whatever the caller may read. A generation never does: it covers the served rows alone.
        """
        if self._digest is None:
            domains = [self.taxonomy.domains[key].as_dict() for key in sorted(self.taxonomy.domains)]
            aliases = sorted(self.taxonomy.domain_aliases.items())
            projects = project_keys.load_project_registry(self.vault_root).content_hash
            payload = json.dumps([domains, aliases, projects], sort_keys=True)
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
        if self.kind == KIND_ANCHOR:
            item["profile"] = anchor_profile_ref(self.ref)
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

    @property
    def entry_id(self) -> str:
        return _digest(json.dumps(self.item(), sort_keys=True))

    def item(self) -> dict[str, Any]:
        item = copy.deepcopy(self.fields)
        for field in egress._WORKING_SET_PROSE_FIELDS:
            if isinstance(item.get(field), str):
                item[field] = egress.without_wikilinks(item[field])
        return item


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
    #: The map's current state of each anchor and collection, by entry id, over the caller's view.
    states: dict[str, dict[str, Any]] | None = None
    #: False when the map's states could not be read; its items then carry none.
    state_available: bool = True
    #: Profiles expose only truncation and notice markers; maps and warming omit this field.
    missing: tuple[dict[str, Any], ...] | None = None


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


def _unlinked(text: str) -> str:
    """Registry text as a view serves it: without its wikilinks, as the catalogue stores an entry's."""
    return " ".join(egress.without_wikilinks(text).split())


def _topic_row(key: str, registries: _Registries, activity: str) -> Row:
    definition = registries.taxonomy.domains.get(key)
    description = (
        working_set_state.bounded_statement(_unlinked(definition.description), working_set_index.LEDE_MAX_CHARS)
        if definition is not None
        else ""
    )
    label = _unlinked(definition.label) if definition is not None and definition.label else ""
    return Row(
        entry_id=f"{KIND_TOPIC}:{key}",
        kind=KIND_TOPIC,
        anchor_kind="",
        title=label or key,
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


def _world(
    catalogue: context_catalogue.ContextCatalogue, caller: CallerView,
    among: Sequence[context_catalogue.Entry] | None = None,
) -> _World:
    """The caller's world over `among`, entries chosen before admission (default: every entry)."""
    if among is None:
        among = catalogue.entries()
        # Every entry is decided here, and each read of the view decides them all again.
        egress.reserve_decision_memo(len(among))
    entries = [
        entry for entry in among
        if _admits(entry, caller.visible)
        # A project anchor is the project registry's entry: its title is the registry's folder.
        and (caller.projects_admitted or entry.anchor_kind != working_set_index.PROJECT_ANCHOR_KIND)
    ]
    # The join runs over the visible entries only: a relation to an entry the caller cannot see adds nothing.
    # It resolves through the domain registry, so a caller who may not read the registry joins no topic.
    joined = (
        catalogue.memberships(entries, projects=caller.projects_admitted).topics if caller.domains_admitted else {}
    )
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


def _view_rows(target: ViewTarget, key: str, world: _World, registries: _Registries | None) -> list[Row]:
    if target.kind == VIEW_MAP:
        topics = (
            []
            if registries is None
            else _ordered(_topic_row(topic, registries, activity) for topic, activity in world.topic_activity.items())
        )
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
    root: Path
    target: ViewTarget
    #: The ref the view is known by: a deprecated or aliased topic key reads as its replacement.
    ref: str
    key: str
    #: None when the caller may not read the domain registry: then no part of the view reads it.
    registries: _Registries | None
    caller: CallerView
    state: str
    catalogue: context_catalogue.ContextCatalogue | None
    unlisted: bool


def _prepare(
    vault_root: Path, path: str, caller: CallerView | None, purpose: str | None = None,
    freshness_stamp: str = "",
) -> _Prepared:
    """Resolve the ref and bring the catalogue forward. Reads no entry."""
    target = parse_view_ref(path)
    if target is None:
        raise not_found()
    root = Path(vault_root)
    caller = caller or CallerView.current(root, purpose=purpose)
    # Admission comes before any definition is read, so a registered topic and an unknown one read
    # the same to a caller who may not read the registry, whatever the catalogue's readiness.
    registries = _Registries(root) if caller.domains_admitted else None
    key = ""
    ref = path
    if target.kind in (VIEW_TOPIC, PROFILE_TOPIC):
        resolved = registries.topic(target.key) if registries is not None else None
        if resolved is None:
            raise not_found()
        key = resolved
        ref = topic_profile_ref(resolved) if target.profile else topic_ref(resolved)
    elif target.kind == PROFILE_ANCHOR:
        key, ref = target.key, anchor_profile_ref(target.key)
    # A caller not admitted to the project registry is served no project entries (`_world`).
    # The lag is decided with the page filter, which keeps a page that no longer exists:
    # `caller.visible` fails closed on it, and a deleted page would read as withheld.
    lag_visible = None if caller.visible is None else egress.page_filter_over(root, caller.visible)
    state, catalogue, unlisted = context_catalogue.ensure_unlisted(
        root, freshness_stamp=freshness_stamp, visible=lag_visible,
        registry_visible=caller.projects_admitted,
    )
    return _Prepared(root, target, ref, key, registries, caller, state, catalogue, unlisted)


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


def _compile(prepared: _Prepared, *, timings: find_types.FindTimings | None = None) -> View:
    if prepared.target.profile:
        return _compile_profile(prepared)
    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None  # `_usable` returned None
    world = _world(catalogue, caller)
    rows = _view_rows(prepared.target, prepared.key, world, prepared.registries)
    if not rows and prepared.target.kind == VIEW_TOPIC:
        # A domain with nothing this caller can see reads as one that does not exist. The map and
        # the unfiled map always exist, so an empty one is served, empty.
        raise not_found()
    states: dict[str, dict[str, Any]] | None = None
    state_available = True
    if prepared.target.kind == VIEW_MAP:
        try:
            with find_types.timing_span(timings, "working_set.map_state"):
                states = _states(prepared.root, rows, caller)
        except (egress.ReaderViewUnavailable, lifecycle_statuses.ClassificationUnavailable):
            states, state_available = {}, False
    # The generation covers every field an item is served, its state included, so a continuation
    # minted before any of them changed is stale. It reads nothing else: a registry edit that
    # changes no served row moves no byte this caller receives.
    digest = hashlib.sha256(json.dumps(state_available).encode("ascii"))
    for row in rows:
        digest.update(row.digest().encode("ascii"))
        if states is not None:
            digest.update(_digest(json.dumps(states.get(row.entry_id), sort_keys=True)).encode("ascii"))
    pending = catalogue.pending()
    stale = prepared.unlisted or any(caller.sees(path) for path in pending)
    graph = None if caller.restricted else catalogue.graph_state()
    return View(prepared.ref, READY, digest.hexdigest(), stale, tuple(rows), graph, states, state_available)


def compile_view(vault_root: Path, path: str, *, caller: CallerView | None = None) -> View:
    """The view `path` names, for `caller` (default: the bound principal).

    Raises NOT_FOUND for a ref that names no view this caller can see.
    """
    prepared = _prepare(vault_root, path, caller)
    return _usable(prepared) or _compile(prepared)


log = logging.getLogger(__name__)
_PROFILE_FAILURES: OrderedDict[tuple, set[str]] = OrderedDict()
_PROFILE_FAILURE_LOCK = threading.Lock()


def _log_profile_failure(root: Path, token: tuple, lane: str, error: Exception) -> None:
    """Report a lane fault once per catalogue generation, never through caller-visible state."""
    key = (str(root), token)
    with _PROFILE_FAILURE_LOCK:
        lanes = _PROFILE_FAILURES.setdefault(key, set())
        if lane in lanes:
            return
        lanes.add(lane)
        _PROFILE_FAILURES.move_to_end(key)
        while len(_PROFILE_FAILURES) > PROFILE_FAILURE_GENERATIONS:
            _PROFILE_FAILURES.popitem(last=False)
    log.warning("profile lane failed: generation=%s lane=%s exception=%s", token, lane, type(error).__name__)


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


def _names_anchor(row: Row, key: str) -> bool:
    return row.kind in (KIND_ANCHOR, KIND_COLLECTION) and key in (row.ref, row.path, row.entry_id)


def _profile_candidates(
    catalogue: context_catalogue.ContextCatalogue, prepared: _Prepared, caller: CallerView
) -> list[context_catalogue.Entry]:
    """The entries that can change what a profile serves, chosen before any of them is decided.

    A decision costs a page read, so a profile decides these and not the whole catalogue. The
    members come first: the entries that name the anchor, or that the join over every entry files
    under the topic. A visible member's topics are a subset of that join's, and a membership target
    that files a member under the topic is filed there too, so it is a member here. Then the other
    inputs of the caller's join: every revision of a member's thread, because the thread serves its
    newest visible revision, and every entry of a member's project keys, because an entry without
    activity of its own takes its project's newest.
    """
    entries = catalogue.entries()
    if prepared.target.kind == PROFILE_ANCHOR:
        chosen = [entry for entry in entries if _names_anchor(_row(entry, (), entry.activity), prepared.key)]
    else:
        joined = catalogue.memberships(entries, projects=caller.projects_admitted).topics
        chosen = [entry for entry in entries if prepared.key in joined.get(entry.entry_id, ())]
    ids = {entry.entry_id for entry in chosen}
    threads = {entry.thread_key for entry in chosen if entry.kind == KIND_THREAD}
    projects = {key for entry in chosen if not entry.activity for key in entry.projects}
    return [
        entry for entry in entries
        if entry.entry_id in ids
        or (entry.kind == KIND_THREAD and entry.thread_key in threads)
        or not projects.isdisjoint(entry.projects)
    ]


def _compile_profile(prepared: _Prepared) -> View:
    from . import find

    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None
    root = catalogue.vault_root
    token = catalogue.token()
    world = _world(catalogue, caller, _profile_candidates(catalogue, prepared, caller))
    if prepared.target.kind == PROFILE_ANCHOR:
        members = _ordered(row for row in world.rows if _names_anchor(row, prepared.key))
    else:
        members = _ordered(row for row in world.rows if prepared.key in row.topics)
    if not members:
        raise not_found()
    pending = catalogue.pending()
    stale = prepared.unlisted or any(caller.sees(path) for path in pending)
    basis = lifecycle_statuses.Basis(root)
    registry = context_roles.load_roles(root)
    conventions = activation_conventions.load_conventions(root)
    snapshot = find.FreshnessSnapshot(root)
    anchor_rows = [row for row in members if row.kind in (KIND_ANCHOR, KIND_COLLECTION)]
    neighbourhood = None
    if prepared.target.kind == PROFILE_TOPIC:
        anchor_rows = [row for row in anchor_rows if _carryable(row)][:PROFILE_MAX_ANCHORS]
        # Members were filtered and ordered before either resource bound applies.
        neighbourhood = frozenset(
            [row.path for row in members if row.kind == context_catalogue.KIND_NOTE][:PROFILE_MAX_NOTES]
        )
    visible = egress.reader_view(root, purpose=caller.purpose, unit_identity_only=True)
    anchors = _profile_anchors(root, anchor_rows, caller)
    analysis = working_set_resolve.analyze_turn("", vocabulary=conventions.conventions.referential)
    roles = context_roles.select_roles(registry, anchor_kinds=[row.kind for row in anchors], analysis=analysis)
    if prepared.target.kind == PROFILE_TOPIC and not anchors:
        # Declared notes have no anchor kind; the existing page selector uses their visible categories.
        by_page = working_set._unit_categories(root, tuple(neighbourhood or ()), visible)
        categories = None if by_page is None else frozenset(category for found in by_page.values() for category in found)
        roles = working_set._carry_roles(registry, analysis, categories)
    state = working_set_state.current_state_for(
        root, anchors=anchors, visible=visible, purpose=caller.purpose, status_basis=basis,
        state_fields=conventions.conventions.state_fields, date_fields=conventions.conventions.date_fields,
    )
    state = [dict(_served_state(entry), anchor=entry["anchor"]) for entry in state]
    items, missing = working_set.run_lanes(
        root, anchors=anchors, roles=roles, registry=registry, current_state=state,
        visible=visible, neighbourhood=neighbourhood, freshness_snapshot=snapshot,
        analysis=analysis, status_basis=basis,
        lane_failed=lambda lane, error: _log_profile_failure(root, token, lane, error),
    )
    # The packet builder owns currency, deduplication, role caps and whole-unit budgets.
    packet = working_set.build_packet(
        # A profile serves page candidates by ref; only semantic units carry authored prose and dates.
        items=tuple(replace(item, title=_unlinked(item.title), why=egress.without_wikilinks(item.why),
                            text="" if item.level == "page" else egress.without_wikilinks(item.text)) for item in items),
        anchors=(), roles=roles, current_state=state, ambiguity=(), missing=missing,
        max_chars=working_set.MAX_BUDGET_CHARS, generation={}, status="resolved",
        status_basis=basis, visible=visible,
    )
    missing = packet["missing"]
    if any(entry.get("reason") == "lane_failed" for entry in missing):
        # A failed lane leaves knowledge unknown; warming costs a retry instead of silently serving omissions.
        return _abstention(prepared, INDEX_WARMING)
    if packet.get("abstained"):
        return _abstention(prepared, UNAVAILABLE)
    pages = {item.ref for item in items if item.level == "page"}
    for pointer in packet["pointers"]:
        if pointer["ref"] in pages:
            pointer["reason"] = "requires_read"
    rows = tuple(
        ProfileLine({"kind": section, **item})
        for section in PROFILE_SECTIONS
        for item in packet[section]
    )
    generation = _digest(json.dumps([row.item() for row in rows], sort_keys=True))
    # A losing build cannot publish lines from an obsolete catalogue state.
    if catalogue.token() != token:
        return _abstention(prepared, INDEX_WARMING)
    return View(prepared.ref, READY, generation, stale, rows,
                missing=tuple(entry for entry in missing if entry.get("reason") == MISSING_TRUNCATED))


# --------------------------------------------------------------------------- #
# The activation map's budget
# --------------------------------------------------------------------------- #


def map_item_line(item: Mapping[str, Any]) -> str:
    """Render one map item, including the current state's own date and source when present."""
    text = f"{item['kind']} {item['title']}"
    if item.get("description"):
        text += f": {item['description']}"
    state = item.get("state")
    if isinstance(state, Mapping) and state.get("statement"):
        text += f"; {state['statement']}"
        details = [f"{key}: {state[key]}" for key in STATE_FIELDS[1:] if state.get(key)]
        if details:
            text += " (" + "; ".join(details) + ")"
    ref = item.get("profile") if item["kind"] == KIND_ANCHOR and not item.get("path") else item["ref"]
    return f"{text} [{ref or item['ref']}]"


def _map_item(view: View, row: Row) -> dict[str, Any]:
    item = row.item()
    if view.states is not None:
        item["state"] = view.states.get(row.entry_id)
    return item


def map_within(
    view: View, max_chars: int, *, item_for: Callable[[Row], Mapping[str, Any]] | None = None,
) -> tuple[tuple[int, ...], str]:
    """The indices of whole map lines that fit `max_chars`, and the closing domain-map pointer.

    Each row kind first gets its share of the budget (`MAP_KIND_SHARES`), in map order, stopping at
    its first line that does not fit; what is left then goes to the next lines in map order. A line is
    never cut. The last line is always present: the map carries no note and no retired entry, so it
    points at the domain maps that do. An empty pointer means even that line did not fit.
    `item_for` resolves state lazily, only for rows whose lines the budget can consider.
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
        return (), ""
    costs: dict[int, int] = {}

    def cost(index: int, remaining: int) -> int:
        if index not in costs:
            row = rows[index]
            # State can only add text, so a base line that cannot fit needs no state read.
            base = len(map_item_line(row.item())) + 1
            if base > remaining:
                return base
            item = item_for(row) if item_for is not None else _map_item(view, row)
            costs[index] = len(map_item_line(item)) + 1
        return costs[index]

    chosen: set[int] = set()
    used = 0
    for kind, share in MAP_KIND_SHARES.items():
        allowance, spent = int(budget * share), 0
        for index, row in enumerate(rows):
            if row.kind != kind:
                continue
            size = cost(index, allowance - spent)
            if spent + size > allowance:
                break
            chosen.add(index)
            spent += size
        used += spent
    for index in range(len(rows)):
        if index in chosen:
            continue
        size = cost(index, budget - used)
        if used + size > budget:
            break
        chosen.add(index)
        used += size
    return tuple(sorted(chosen)), tail(len(chosen))


@dataclass(frozen=True, slots=True)
class _ActivationMap:
    view: View
    selected: tuple[int, ...]
    overflow: str
    empty: bool


def _activation_map(prepared: _Prepared, max_chars: int, timings: find_types.FindTimings | None) -> _ActivationMap:
    """Budget before reading state; only candidate rows pay the current-state resolver's cost."""
    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None
    world = _world(catalogue, caller)
    rows = tuple(_view_rows(prepared.target, prepared.key, world, prepared.registries))
    states: dict[str, dict[str, Any]] = {}
    view = View(
        prepared.ref, READY, None, prepared.unlisted or any(caller.sees(path) for path in catalogue.pending()), rows,
        None if caller.restricted else catalogue.graph_state(), states,
    )
    available = True
    index = working_set_index.WorkingSetIndex(prepared.root)
    anchors = None

    def item_for(row: Row) -> Mapping[str, Any]:
        nonlocal available, anchors
        if available and row.kind in (KIND_ANCHOR, KIND_COLLECTION):
            try:
                if anchors is None:
                    anchors = {anchor.anchor_id: anchor for anchor in index.anchors()}
                anchor = anchors.get(row.entry_id)
                if anchor is not None:
                    # A single-row batch stops state work as soon as the budget is full.
                    states.update(_anchor_states(prepared.root, (anchor,), caller, index.token()))
            except (egress.ReaderViewUnavailable, lifecycle_statuses.ClassificationUnavailable):
                states.clear()
                available = False
        return _map_item(view, row)

    try:
        with find_types.timing_span(timings, "working_set.map_state"):
            selected, overflow = map_within(view, max_chars, item_for=item_for)
    finally:
        index.close()
    # Activation tokens describe the budgeted map received; read_memory retains its whole-view generation.
    items = [_map_item(view, rows[index]) for index in selected]
    generation = _digest(json.dumps([items, available, overflow, not world.rows, view.stale, view.graph], sort_keys=True))
    return _ActivationMap(replace(view, generation=generation, state_available=available), selected, overflow, not world.rows)


# The activation cache shares the existing packet cache's resource bound. Restricted builds never enter it.
_OWNER_MAPS: OrderedDict[tuple[Any, ...], _ActivationMap] = OrderedDict()
_OWNER_MAPS_LOCK = threading.Lock()


def map_packet_chars(block: Mapping[str, Any]) -> int:
    """The prose budget charged for a map, using the same rendering that selects its items."""
    lines = [str(block.get("description") or "")]
    lines.extend(map_item_line(item) for item in block.get("items", ()))
    if block.get("overflow"):
        lines.append(str(block["overflow"]))
    return len("\n".join(line for line in lines if line))


def activation_packet(
    vault_root: Path, max_chars: int, *, purpose: str | None = None,
    generation: Mapping[str, Any], timings: find_types.FindTimings | None = None,
    turn_leads: Callable[[CallerView], TurnLeads] | None = None,
) -> dict[str, Any]:
    """Serve the turn-independent map from the published catalogue for this caller.

    `turn_leads` turns the turn slice on: it holds back `TURN_SLICE_SHARE` of the budget for the slice
    (so the map is the same on every turn), and it is called once, after the map is compiled, with the
    map's caller view, for what the turn's words reached. A fault in it, or no room for a line, sets the
    slice's state and never changes the map.
    """
    if purpose is not None:
        with principal_module.request_scope(principal_module.effective_principal().with_purpose(purpose)):
            return _activation_packet(vault_root, max_chars, purpose, generation, timings, turn_leads)
    return _activation_packet(vault_root, max_chars, purpose, generation, timings, turn_leads)


def _activation_packet(
    root: Path, max_chars: int, purpose: str | None, generation: Mapping[str, Any],
    timings: find_types.FindTimings | None, turn_leads: Callable[[CallerView], TurnLeads] | None = None,
) -> dict[str, Any]:
    with find_types.timing_span(timings, "working_set.catalogue"):
        prepared = _prepare(root, MAP_REF, None, purpose, str(generation.get("freshness_key") or ""))
    catalogue = prepared.catalogue
    try:
        metadata = {
            **generation,
            **context_roles.load_roles(root).generation_block(),
            **activation_conventions.load_conventions(root).generation_block(),
        }
        view = _usable(prepared)
        description = "Index of what exists."
        reserve = 0 if turn_leads is None else _slice_reserve(max_chars)
        map_chars = max_chars - reserve - len(description) - 1
        if view is None:
            assert catalogue is not None
            # Only a caller who may use the owner cache builds its key: the key reads every registry.
            key = None if prepared.caller.restricted else (
                Path(root).absolute(), catalogue.token(), prepared.registries.digest if prepared.registries is not None else "",
                str(generation.get("freshness_key") or ""), prepared.caller.binding,
                metadata.get("index_generation"), metadata["roles_hash"], metadata["conventions_hash"],
                prepared.unlisted, tuple(catalogue.pending()), map_chars,
            )
            with _OWNER_MAPS_LOCK:
                compiled = None if key is None else _OWNER_MAPS.get(key)
                if compiled is not None:
                    _OWNER_MAPS.move_to_end(key)
            with find_types.timing_span(timings, "working_set.map", source="cache" if compiled else "computed"):
                if compiled is None:
                    compiled = _activation_map(prepared, map_chars, timings)
                    if key is not None and compiled.view.state_available and catalogue.token() == key[1]:
                        with _OWNER_MAPS_LOCK:
                            _OWNER_MAPS[key] = compiled
                            _OWNER_MAPS.move_to_end(key)
                            while len(_OWNER_MAPS) > CACHE_SIZE:
                                _OWNER_MAPS.popitem(last=False)
                else:
                    with find_types.timing_span(timings, "working_set.map_state", source="cache"):
                        pass
            view = compiled.view
        metadata["map_generation"] = view.generation
        packet = working_set.abstained_packet(reason=view.state, max_chars=max_chars, generation=metadata)
        if view.state != READY:
            return packet
        rows = [view.rows[index] for index in compiled.selected]
        block = _response(view, [], None)
        block["description"] = description
        if compiled.overflow:
            block["overflow"] = compiled.overflow
        if compiled.empty:
            block["reason"] = "empty"
        if not view.state_available:
            block["state_available"] = False
        block["items"] = served_items(
            root, view, rows, prepared.caller, block=block, limit=packet["budget"]["limit_chars"]
        )
        packet["map"] = block
        packet["budget"]["used_chars"] = map_packet_chars(block)
        if turn_leads is not None:
            slice_block = _turn_slice(
                prepared, turn_leads, limit=packet["budget"]["limit_chars"], reserve=reserve, timings=timings
            )
            packet["turn_slice"] = slice_block
            packet["budget"]["used_chars"] += slice_packet_chars(slice_block)
        packet["abstained"] = False
        packet.pop("abstention", None)
        return packet
    finally:
        if catalogue is not None:
            catalogue.close()


# --------------------------------------------------------------------------- #
# The turn slice
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Lead:
    """One page, anchor or collection that the turn's words reached, as the guarded compile named it."""

    ref: str
    match: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurnLeads:
    """What the turn reached: `READY` with its leads, `SLICE_EMPTY` with none, or why there is no answer.

    `evidence` holds the compile's own evidence states (`lexical_evidence`, `semantic_evidence`) as its
    packet reported them, so a slice that is empty because a lane was warming says so.
    """

    state: str
    leads: tuple[Lead, ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()


def _lead_ref(item: Mapping[str, Any]) -> str:
    return str(item.get("ref") or item.get("path") or "")


def _lead_evidence(item: Mapping[str, Any]) -> tuple[str, ...]:
    """The item's evidence kinds, only those of the closed vocabulary, in its reporting order."""
    named = {kind for kind in item.get("evidence") or () if isinstance(kind, str)}
    return tuple(kind for kind in working_set_resolve.EVIDENCE_KINDS if kind in named)


#: The abstentions that answer the turn itself, so their packet is read for leads like a served one.
_TURN_ANSWERS = frozenset({UNRESOLVED, AMBIGUOUS, WITHHELD})


def turn_leads(packet: Mapping[str, Any]) -> TurnLeads:
    """What the slice reads off one guarded anchors compile, served or abstained: the one place its outcome
    becomes a slice state.

    A served packet, and an abstention that answers the turn (nothing reached, a question between senses,
    everything withheld), give their leads: anchors, the senses of an ambiguous turn and related pages. A
    withheld packet so reads as a turn that reached nothing, and the slice says nothing about what was
    withheld. An abstention that the request budget caused (it carries `request_budget`) is `skipped`; any
    other one (a fault, a warming index, evidence that changed under the compile) is `unavailable`.

    `recent_context` is turn-independent and is never a lead. An anchor that is one sense of an ambiguous
    turn is listed as `ambiguous` whatever its own status. Strongest match first, the compile's order within one.
    """
    generation = packet.get("generation")
    evidence = tuple(
        (name, str(generation[name]))
        for name in ("lexical_evidence", "semantic_evidence")  # nosemgrep: ep-word-set -- The packet's two evidence-state fields.
        if isinstance(generation, Mapping) and generation.get(name)
    )
    abstention = packet.get("abstention")
    if packet.get("abstained") and (not isinstance(abstention, Mapping) or abstention.get("reason") not in _TURN_ANSWERS):
        return TurnLeads(SLICE_SKIPPED if "request_budget" in packet else UNAVAILABLE, (), evidence)
    senses = {
        _lead_ref(item) for item in packet.get("ambiguity") or () if isinstance(item, Mapping)
    }
    found: dict[str, Lead] = {}

    def add(item: Mapping[str, Any], match: str | None) -> None:
        ref = _lead_ref(item)
        if ref and match is not None and ref not in found:
            found[ref] = Lead(ref, match, _lead_evidence(item))

    for item in packet.get("anchors") or ():
        if isinstance(item, Mapping):
            add(item, "ambiguous" if _lead_ref(item) in senses else _MATCH_OF_STATUS.get(item.get("status")))
    for item in packet.get("ambiguity") or ():
        if isinstance(item, Mapping):
            add(item, "ambiguous")
    for item in packet.get("related") or ():
        if isinstance(item, Mapping):
            add(item, "related")
    leads = tuple(sorted(found.values(), key=lambda lead: MATCHES.index(lead.match)))
    return TurnLeads(READY if leads else SLICE_EMPTY, leads, evidence)


def slice_item_line(item: Mapping[str, Any]) -> str:
    """Render one slice item: how the turn reached it, its map line, and the evidence."""
    line = f"{item['match']}: {map_item_line(item)}"
    if item.get("evidence"):
        line += " (evidence: " + ", ".join(item["evidence"]) + ")"
    return line


def _slice_overflow(count: int) -> str:
    return f"Not shown: {count}."


def slice_packet_chars(block: Mapping[str, Any]) -> int:
    """The prose budget charged for a slice, rendered as `slice_item_line` selects it. A slice with no lines costs none."""
    if block.get("state") != READY or not block.get("description"):
        return 0
    lines = [str(block["description"])]
    lines.extend(slice_item_line(item) for item in block.get("items", ()))
    if block.get("overflow"):
        lines.append(_slice_overflow(int(block["overflow"])))
    # One newline joins the slice to the map text before it.
    return len("\n".join(lines)) + 1


def packet_chars(map_block: Mapping[str, Any], slice_block: Mapping[str, Any] | None) -> int:
    """What a maps packet is charged: its map, and its turn slice when it carries one."""
    return map_packet_chars(map_block) + (0 if slice_block is None else slice_packet_chars(slice_block))


def _slice_reserve(max_chars: int) -> int:
    """The characters that the slice holds back of `max_chars`: its share, or none where the share cannot hold
    the description and the shortest line that any slice can have. It reads the budget alone, so the map is
    still the same on every turn."""
    reserve = int(max_chars * TURN_SLICE_SHARE)
    # The shortest match and kind of their closed sets (an item is a map row's kind or a note), with a one-character
    # title and ref.
    kind = min((*MAP_KIND_SHARES, context_catalogue.KIND_NOTE), key=len)
    shortest = slice_item_line({"match": min(MATCHES, key=len), "kind": kind, "title": "?", "ref": "?"})
    return reserve if reserve - 1 - len(SLICE_DESCRIPTION) >= len(shortest) + 1 else 0


def _turn_slice(
    prepared: _Prepared, turn_leads: Callable[[CallerView], TurnLeads], *, limit: int, reserve: int,
    timings: find_types.FindTimings | None,
) -> dict[str, Any]:
    """The turn slice for this caller, in the `reserve` that the map left. It never raises and never touches the map."""
    state, chosen, overflow, evidence = UNAVAILABLE, [], 0, ()
    extras: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    try:
        # The map's view: its release filter has already decided every page the map admitted.
        # A share with no room for a line skips the slice without compiling it.
        leads = turn_leads(prepared.caller) if reserve else TurnLeads(SLICE_SKIPPED)
        state, evidence = leads.state, leads.evidence
        if state == READY:
            with find_types.timing_span(timings, "working_set.turn_slice"):
                rows, extras = _slice_rows(prepared, leads)
                if rows:
                    chosen, overflow = _slice_within(prepared, rows, extras, states, reserve - 1 - len(SLICE_DESCRIPTION))
                # No admitted lead is an empty turn; leads with no room for one whole line are a skipped slice.
                state = READY if chosen else (SLICE_SKIPPED if rows else SLICE_EMPTY)
    except Exception:  # noqa: BLE001 - a slice that cannot be built is a state, never a fault of the map
        log.warning("turn slice unavailable", exc_info=True)
        state, chosen, overflow, extras, states = UNAVAILABLE, [], 0, {}, {}
    shown = {row.entry_id: extras[row.entry_id] for row in chosen}
    block: dict[str, Any] = {"state": state}
    if state == READY:
        block["description"] = SLICE_DESCRIPTION
        if overflow:
            block["overflow"] = overflow
    # The compile's evidence states, so a slice that a warming lane left empty is not a silent `empty`.
    block.update(evidence)
    # The record's key: the content this slice serves, so the same content is the same generation.
    block["generation"] = _digest(json.dumps(
        [state, [[row.digest(), shown[row.entry_id], states.get(row.entry_id)] for row in chosen], overflow, evidence],
        sort_keys=True,
    ))
    view = View(SLICE_REF, READY, block["generation"], False, tuple(chosen), None, states)
    block["items"] = served_items(
        prepared.root, view, chosen, prepared.caller, block=block, limit=limit, extras=shown
    )
    return block


def _slice_rows(prepared: _Prepared, leads: TurnLeads) -> tuple[tuple[Row, ...], dict[str, dict[str, Any]]]:
    """The caller's rows for the leads, in lead order, with how each was reached.

    A lead names a catalogue entry by its id or its ref. A lead the caller may not see, or that names no
    entry, has no row, and its text is never read: display text comes from admitted entries alone.
    """
    catalogue, caller = prepared.catalogue, prepared.caller
    assert catalogue is not None  # `_usable` returned None
    entries = catalogue.entries_for(lead.ref for lead in leads.leads)
    world = _world(catalogue, caller, among=entries)
    by_name: dict[str, Row] = {}
    for row in world.rows:
        by_name.setdefault(row.entry_id, row)
        by_name.setdefault(row.ref, row)
    rows: list[Row] = []
    extras: dict[str, dict[str, Any]] = {}
    for lead in leads.leads:
        row = by_name.get(lead.ref)
        if row is not None and row.entry_id not in extras:
            rows.append(row)
            extras[row.entry_id] = {"match": lead.match, "evidence": list(lead.evidence)}
    return tuple(rows), extras


def _slice_within(
    prepared: _Prepared, rows: Sequence[Row], extras: Mapping[str, Mapping[str, Any]],
    states: dict[str, dict[str, Any]], available: int,
) -> tuple[list[Row], int]:
    """The leading rows whose whole lines fit `available` characters, and how many rows were left out.

    A line is never cut and a stronger lead is never passed over for a weaker one. State is read only for a
    row whose line without it fits. What is left out is a count, whose own line is kept room for.
    """
    index = working_set_index.WorkingSetIndex(prepared.root)
    anchors: dict[str, Any] | None = None
    readable = True

    def read_state(row: Row) -> None:
        nonlocal anchors, readable
        if not readable or row.kind not in (KIND_ANCHOR, KIND_COLLECTION):
            return
        try:
            if anchors is None:
                anchors = {anchor.anchor_id: anchor for anchor in index.anchors()}
            anchor = anchors.get(row.entry_id)
            if anchor is not None:
                states.update(_anchor_states(prepared.root, (anchor,), prepared.caller, index.token()))
        except (egress.ReaderViewUnavailable, lifecycle_statuses.ClassificationUnavailable):
            states.clear()
            readable = False

    chosen: list[Row] = []
    used = 0
    footer = len(_slice_overflow(len(rows))) + 1
    try:
        for position, row in enumerate(rows):
            # Room for the count is kept back until the last row, which needs none when every row is shown.
            remaining = available - used - (0 if position == len(rows) - 1 else footer)
            item = {**row.item(), **extras[row.entry_id]}
            # State can only add text, so a line that does not fit without it needs no state read.
            if len(slice_item_line(item)) + 1 > remaining:
                break
            read_state(row)
            line = len(slice_item_line({**item, "state": states.get(row.entry_id)})) + 1
            if line > remaining:
                states.pop(row.entry_id, None)
                break
            chosen.append(row)
            used += line
    finally:
        index.close()
    return chosen, len(rows) - len(chosen)


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
    """Current state for the anchors and collections among `rows`, by entry id, each over the caller's own view."""
    wanted = {row.entry_id for row in rows if row.kind in (KIND_ANCHOR, KIND_COLLECTION)}
    if not wanted:
        return {}
    index = working_set_index.WorkingSetIndex(vault_root)
    try:
        anchors = [anchor for anchor in index.anchors() if anchor.anchor_id in wanted]
        return _anchor_states(vault_root, anchors, caller, index.token())
    finally:
        index.close()


def _anchor_states(vault_root: Path, anchors: Sequence[Any], caller: CallerView, token: tuple[int, int, int]) -> dict[str, dict[str, Any]]:
    entries = working_set_state.current_state_for(
        vault_root, anchors=anchors, purpose=caller.purpose, visible=caller.visible,
        index_generation=token[1], index_token=token,
    )
    entry_of_ref = {anchor_ref(anchor): anchor.anchor_id for anchor in anchors}
    return {entry_of_ref[str(entry["anchor"])]: _served_state(entry) for entry in entries if str(entry.get("anchor")) in entry_of_ref}


def _served_state(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A current state as a view serves it, its statement without wikilinks.

    So whether a page that the statement links to exists, or is withheld, changes no byte of it.
    The guard checks the page the state was read from, never the pages its statement mentions.
    """
    state = {key: entry[key] for key in STATE_FIELDS if entry.get(key) is not None}
    if isinstance(state.get("statement"), str):
        state["statement"] = _unlinked(state["statement"])
    return state


def read_view(
    vault_root: Path, path: str, *, continuation: str | None = None, purpose: str | None = None
) -> dict[str, Any]:
    """Serve one page of a view, for the bound principal under the `purpose` the request declared.

    The response carries the ref, the generation, a stale flag, at most `VIEW_PAGE_SIZE` items and a
    continuation for the next page. A view that cannot be served yet answers with its readiness state
    and no items. Raises NOT_FOUND, INVALID_CONTINUATION or CONTINUATION_STALE.
    """
    # The view decides every entry it reads. Like `find`, it resolves placement and reads the connector
    # ceiling once for all of them, and the terminal guard reads both again, live.
    with state_paths.resolution_scope():
        if purpose is None:
            return _read_view(Path(vault_root), path, continuation, None)
        # The readers under the view (a record's current state) take the purpose from the bound principal.
        with principal_module.request_scope(principal_module.effective_principal().with_purpose(purpose)):
            return _read_view(Path(vault_root), path, continuation, purpose)


def _read_view(root: Path, path: str, continuation: str | None, purpose: str | None) -> dict[str, Any]:
    prepared = _prepare(root, path, None, purpose)
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
    extra: dict[str, Any] = {}
    if view.states is not None and not view.state_available:
        extra["state_available"] = False
    return _response(view, served_items(root, view, page, caller, continuation=token), token, extra)


# --------------------------------------------------------------------------- #
# What the terminal guard serves
# --------------------------------------------------------------------------- #


#: The fields of an item that are display text: the catalogue's own, served without links.
DISPLAY_FIELDS = ("title", "description", "description_origin")  # nosemgrep: ep-word-set -- `Row`'s own field names.


@dataclass(frozen=True, slots=True)
class _Served:
    """What one compile served for one response: its rows, with their entry ids, and their states.

    `missing` is a profile's own markers. `block` and `limits` are set for the activation map only:
    the block's own fields and the packet character limits that the block was budgeted for.
    """

    rows: tuple[Row | ProfileLine, ...]
    states: dict[str, Any] | None
    missing: tuple[dict[str, Any], ...] = ()
    block: dict[str, Any] | None = None
    limits: frozenset[int] = frozenset()
    #: Fields a turn slice adds to its items, by entry id: how the turn reached each one.
    extras: dict[str, dict[str, Any]] | None = None


def _served_key(
    vault_root: Path, ref: str, caller: CallerView, generation: str, continuation: str | None
) -> tuple[object, ...]:
    # The admissions are part of the key: a guard that decides under other admissions finds no record.
    return (
        str(Path(vault_root)), ref, caller.binding, caller.domains_admitted, caller.projects_admitted,
        generation, continuation,
    )


def _items(served: _Served) -> list[dict[str, Any]]:
    items = [row.item() for row in served.rows]
    if served.states is not None:
        for row, item in zip(served.rows, items, strict=True):
            item["state"] = copy.deepcopy(served.states.get(row.entry_id))
    if served.extras is not None:
        for row, item in zip(served.rows, items, strict=True):
            item.update(copy.deepcopy(served.extras.get(row.entry_id) or {}))
    return items


def served_items(
    vault_root: Path, view: View, rows: Sequence[Row | ProfileLine], caller: CallerView, *,
    continuation: str | None = None, block: Mapping[str, Any] | None = None, limit: int | None = None,
    extras: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The items `rows` are served as. The invocation that compiled them keeps them for the terminal guard.

    The record is kept under the view's ref, its generation, the caller and the page's position (the
    continuation the response carries) in the invocation's own store (`principal.invocation_served`),
    so nothing another invocation does can change or evict it, and it ends with the invocation. Outside
    any invocation nothing is kept, and the guard then serves no item. An activation map's record also
    names each packet character `limit` that its block was budgeted for.
    """
    served = _Served(
        tuple(rows),
        None if view.states is None else {row.entry_id: copy.deepcopy(view.states.get(row.entry_id)) for row in rows},
        copy.deepcopy(view.missing or ()),
        # The block keeps its `items` key, empty, so a restored block keeps its key order.
        None if block is None else copy.deepcopy({**block, "items": []}),
        frozenset() if limit is None else frozenset((limit,)),
        None if extras is None else copy.deepcopy({key: dict(value) for key, value in extras.items()}),
    )
    store = principal_module.invocation_served()
    if store is not None and view.generation is not None:
        key = _served_key(vault_root, view.ref, caller, view.generation, continuation)
        held = store.get(key, served)
        # A map's generation covers its block, so one block budgeted again for another packet adds that
        # packet's limit. One invocation that served two different pages under one key cannot say which
        # one a response is, so the key then holds no record and the guard serves no item.
        if held is not None and replace(held, limits=served.limits) == served:
            store[key] = replace(served, limits=held.limits | served.limits)
        else:
            store[key] = None
    return _items(served)


def _record(
    vault_root: Path, ref: object, generation: object, continuation: object, caller: CallerView
) -> _Served | None:
    if not isinstance(ref, str) or not isinstance(generation, str) or not isinstance(continuation, str | None):
        return None
    store = principal_module.invocation_served()
    return None if store is None else store.get(_served_key(vault_root, ref, caller, generation, continuation))


def pinned_items(vault_root: Path, response: Mapping[str, Any], caller: CallerView) -> list[dict[str, Any]]:
    """The items this invocation's compile served for `response`, read again from its record.

    The terminal guard serves these in place of the items it is given, and reads none of those. A
    response is named by its ref, its generation and its continuation (the page's position), for
    this caller. So both guard passes serve the text the generation was computed from, and a release
    strip in the first pass cannot change what the second pass serves. A response that this invocation
    never compiled for this caller, under the admissions that stand now, gets no item.
    """
    served = _record(vault_root, response.get("ref"), response.get("generation"), response.get("continuation"), caller)
    return [] if served is None else _items(served)


def pinned_missing(vault_root: Path, response: Mapping[str, Any], caller: CallerView) -> list[dict[str, Any]]:
    """The profile markers this invocation's compile served for `response`, as `pinned_items` reads its items."""
    served = _record(vault_root, response.get("ref"), response.get("generation"), response.get("continuation"), caller)
    return [] if served is None else copy.deepcopy(list(served.missing))


def profile_markers(vault_root: Path, missing: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The markers a profile serves: each role from a closed set, never text that a page or a caller wrote.

    A withheld marker names a profile section, and a truncation marker names a lane of the vault's
    role registry. Any other marker is dropped, and a kept one carries only its role and reason.
    """
    roles = {
        MISSING_TRUNCATED: frozenset(context_roles.load_roles(vault_root).roles),
        MISSING_WITHHELD: frozenset(PROFILE_SECTIONS),
    }
    return [
        {"role": entry["role"], "reason": entry["reason"]}
        for entry in missing
        if isinstance(entry.get("reason"), str) and isinstance(entry.get("role"), str)
        and entry["role"] in roles.get(entry["reason"], ())
    ]


def pinned_map_block(vault_root: Path, packet: Mapping[str, Any], caller: CallerView) -> dict[str, Any] | None:
    """The activation map that this invocation compiled for `packet`, or None when it compiled none.

    The block is chosen by the packet's own `map_generation` and must be the one compiled for the
    packet's character limit; the block's own `generation` must agree with it. So an untrusted
    block field, or a block budgeted for a larger packet, never reaches the wire.
    """
    block = packet.get("map")
    generation = packet.get("generation")
    budget = packet.get("budget")
    if not isinstance(block, Mapping) or not isinstance(generation, Mapping) or not isinstance(budget, Mapping):
        return None
    map_generation = generation.get("map_generation")
    if block.get("generation") != map_generation:
        return None
    limit = budget.get("limit_chars")
    served = _record(vault_root, MAP_REF, map_generation, None, caller)
    # `bool` is an `int` equal to 0 or 1, so only an exact `int` names a limit.
    if served is None or served.block is None or type(limit) is not int or limit not in served.limits:
        return None
    return {**copy.deepcopy(served.block), "items": _items(served)}


def unavailable_map_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """The `unavailable` abstention that a map packet is served as when its block has no record."""
    generation = {**dict(packet.get("generation") or {}), "map_generation": None}
    # `abstained_packet` clamps the limit it is given into the declared range.
    limit = (packet.get("budget") or {}).get("limit_chars")
    return working_set.abstained_packet(reason=UNAVAILABLE, max_chars=limit, generation=generation)


def pinned_slice_block(vault_root: Path, packet: Mapping[str, Any], caller: CallerView) -> dict[str, Any] | None:
    """The turn slice that this invocation compiled for `packet`, or None when it compiled none.

    The block is chosen by its own `generation`, and must be the one compiled for the packet's character
    limit, as `pinned_map_block` does for the map. So an untrusted block field, or a slice budgeted for a
    larger packet, never reaches the wire.
    """
    block = packet.get("turn_slice")
    budget = packet.get("budget")
    if not isinstance(block, Mapping) or not isinstance(budget, Mapping):
        return None
    limit = budget.get("limit_chars")
    served = _record(vault_root, SLICE_REF, block.get("generation"), None, caller)
    if served is None or served.block is None or type(limit) is not int or limit not in served.limits:
        return None
    return {**copy.deepcopy(served.block), "items": _items(served)}


def unavailable_turn_slice() -> dict[str, Any]:
    """The slice that a packet is served with when its block has no record: no item, and the map untouched."""
    return {"state": UNAVAILABLE, "items": []}


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
    if view.missing is not None:
        out["missing"] = list(view.missing)
    if view.graph is not None:
        out["membership_graph"] = view.graph
    out.update(extra or {})
    return out
