"""The map and the domain maps: read-time views over the context catalogue.

A view is a ranked list of catalogue entries that one caller may see. It is
computed per request from the catalogue's raw rows and the registries as they
stand, and nothing it interprets is stored.

The caller's view comes first. Every row is filtered by what the caller may
see BEFORE it is ordered, counted, paged, hashed or compared with a pending
change, so no entry the caller cannot see moves a byte the caller receives.
`read_memory` serves a view through `read_view`; the activation packet will
serve the map through `compile_view` and `map_within`. Every item a view serves
is pinned under its generation (`served_items`), and the terminal guard serves
the pinned item (`pinned_items`), so both of its passes serve the text that the
generation was computed from.

A caller that may not read the domain registry gets nothing from it: no topic,
no topic key and no domain map. Every topic ref then reads as an unknown one.

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
# Profile sections are the existing packet sections fixed by the read contract.
PROFILE_SECTIONS = ("current_state", "units", "pointers")
# These public reasons are the working-set contract's truncation and disclosure notices.
PROFILE_MISSING_REASONS = ("lane_truncated", "withheld")
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
        return cls(visible, declared, binding, _domains_admitted(root, principal, purpose))


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


def _world(catalogue: context_catalogue.ContextCatalogue, caller: CallerView) -> _World:
    entries = [entry for entry in catalogue.entries() if _admits(entry, caller.visible)]
    # The join runs over the visible entries only: a relation to an entry the caller cannot see adds nothing.
    # It resolves through the domain registry, so a caller who may not read the registry joins no topic.
    joined = catalogue.memberships(entries).topics if caller.domains_admitted else {}
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


def _prepare(vault_root: Path, path: str, caller: CallerView | None, purpose: str | None = None) -> _Prepared:
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
    state, catalogue, unlisted = context_catalogue.ensure_unlisted(root)
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


def _compile(prepared: _Prepared) -> View:
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
            states = _states(prepared.root, rows, caller)
        except (egress.ReaderViewUnavailable, lifecycle_statuses.ClassificationUnavailable):
            states, state_available = {}, False
    # The generation covers every field an item is served, its state included, so a continuation
    # minted before any of them changed is stale.
    registries = prepared.registries.digest if prepared.registries is not None else ""
    digest = hashlib.sha256(registries.encode("ascii"))
    digest.update(json.dumps(state_available).encode("ascii"))
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
    if any(entry.get("reason") == "lane_failed" for entry in missing):
        # A failed lane leaves knowledge unknown; warming costs a retry instead of silently serving omissions.
        return _abstention(prepared, INDEX_WARMING)
    # The packet builder owns currency, deduplication, role caps and whole-unit budgets.
    packet = working_set.build_packet(
        # A profile serves page candidates by ref; only semantic units carry authored prose and dates.
        items=tuple(replace(item, title=_unlinked(item.title), why=egress.without_wikilinks(item.why),
                            text="" if item.level == "page" else egress.without_wikilinks(item.text)) for item in items),
        anchors=(), roles=roles, current_state=state, ambiguity=(), missing=missing,
        max_chars=working_set.MAX_BUDGET_CHARS, generation={}, status="resolved",
        status_basis=basis, visible=visible,
    )
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
                missing=tuple(entry for entry in missing if entry.get("reason") == "lane_truncated"))


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
    """Current state for the anchors and collections among `rows`, by entry id, each over the caller's own view."""
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
    return {entry_of_ref[str(entry["anchor"])]: _served_state(entry) for entry in entries if str(entry.get("anchor")) in entry_of_ref}


def _served_state(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A current state as a view serves it, its statement without wikilinks.

    So whether a page that the statement links to exists, or is withheld, changes no byte of it.
    The guard checks the page the state was read from, never the pages its statement mentions.
    """
    state = {key: entry[key] for key in ("statement", "as_of", "source", "path") if entry.get(key) is not None}
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
    return _response(view, served_items(root, view, page, caller), token, extra)


# --------------------------------------------------------------------------- #
# What the terminal guard serves
# --------------------------------------------------------------------------- #


def _identity(item: Mapping[str, Any]) -> tuple[object, ...]:
    """The fields that name an entry on the wire. A value that is not a string matches no entry."""
    values = (item.get("kind"), item.get("anchor_kind") or "", item.get("ref"), item.get("path") or "")
    return tuple(value if isinstance(value, str) else None for value in values)


#: The fields of an item that are display text: the catalogue's own, served without links.
DISPLAY_FIELDS = ("title", "description", "description_origin")  # nosemgrep: ep-word-set -- `Row`'s own field names.

#: Most items pinned at once, over every pinned page. A memory bound: the terminal guard reads a pin
#: in the request that made it, moments later, so only an in-flight request needs its pin kept.
PINNED_ITEMS_MAX = 4096


class _Pins:
    """The items lately served, by (vault, view ref, caller binding, generation), least recent first."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        #: For each key, the items served under it, by `_identity`, then by entry id.
        self._pages: OrderedDict[tuple[str, ...], dict[tuple[object, ...], dict[str, dict[str, Any]]]] = OrderedDict()
        self._count = 0

    def add(self, key: tuple[str, ...], served: Iterable[tuple[str, dict[str, Any]]]) -> None:
        with self.lock:
            page = self._pages.pop(key, {})
            for entry_id, item in served:
                alike = page.setdefault(_identity(item), {})
                self._count += entry_id not in alike
                alike[entry_id] = copy.deepcopy(item)
            self._pages[key] = page
            while self._count > PINNED_ITEMS_MAX and len(self._pages) > 1:
                _, dropped = self._pages.popitem(last=False)
                self._count -= sum(len(alike) for alike in dropped.values())

    def page(self, key: tuple[str, ...]) -> dict[tuple[object, ...], dict[str, dict[str, Any]]]:
        """The items pinned under `key`. The caller holds `lock` while it reads them."""
        page = self._pages.get(key)
        if page is None:
            return {}
        self._pages.move_to_end(key)
        return page


_PINS = _Pins()


def _pin_key(vault_root: Path, ref: str, caller: CallerView, generation: str) -> tuple[str, ...]:
    return (str(Path(vault_root)), ref, caller.binding, generation)


def served_items(vault_root: Path, view: View, rows: Sequence[Row | ProfileLine], caller: CallerView) -> list[dict[str, Any]]:
    """The items `rows` are served as, each pinned under the view's generation for the terminal guard."""
    items = [row.item() for row in rows]
    if view.states is not None:
        for row, item in zip(rows, items, strict=True):
            item["state"] = view.states.get(row.entry_id)
    if view.generation is not None:
        _PINS.add(_pin_key(vault_root, view.ref, caller, view.generation), zip([row.entry_id for row in rows], items, strict=True))
    return items


def pinned_items(
    vault_root: Path, ref: object, generation: object, items: Sequence[Mapping[str, Any]], caller: CallerView
) -> list[dict[str, Any]]:
    """Each item as the view `ref` served it to `caller` under `generation`. An item it never served is dropped.

    The terminal guard serves these in place of the items it is given. So both of its passes serve the
    text the generation was computed from, and a field that no compile served this caller never reaches
    the wire. An item is matched by the fields that name an entry: its kind, anchor kind, ref and path.
    Of served items alike in all four, the one equal to the given item is kept, else the lowest entry
    id's. Registry admission is decided again, as it stands now: without it, no topic and no topic key.
    """
    if not isinstance(ref, str) or not isinstance(generation, str):
        return []
    out: list[dict[str, Any]] = []
    with _PINS.lock:
        page = _PINS.page(_pin_key(vault_root, ref, caller, generation))
        for item in items:
            alike = page.get(_identity(item))
            if not alike:
                continue
            own = [served for served in alike.values() if served == item]
            out.append(copy.deepcopy(own[0] if own else alike[min(alike)]))
    if not caller.domains_admitted:
        out = [{**item, "topics": []} if "topics" in item else item for item in out if item.get("kind") != KIND_TOPIC]
    return out


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
