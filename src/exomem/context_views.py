"""The map and the domain maps: read-time views over the context catalogue.

A view is a ranked list of catalogue entries that one caller may see. It is
computed per request from the catalogue's raw rows and the registries as they
stand, and nothing it interprets is stored.

The caller's view comes first. Every row is filtered by what the caller may
see BEFORE it is ordered, counted, paged, hashed or compared with a pending
change, so no entry the caller cannot see moves a byte the caller receives.
`read_memory` serves a view through `read_view`; the activation packet will
serve the map through `compile_view` and `map_within`.

The refs, `exomem://map`, `exomem://map/topic/<domain key>` and
`exomem://map/unfiled`, are the whole view namespace. `read_view` pages a view
with a signed continuation token that binds the view, the caller and the
generation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    activation_conventions,
    context_catalogue,
    context_roles,
    find_types,
    lifecycle_statuses,
    project_keys,
    source_taxonomy,
    vocabulary_resolution,
    working_set,
    working_set_index,
    working_set_state,
)
from .activation_result import INDEX_WARMING
from .context_catalogue import KIND_ANCHOR, KIND_COLLECTION, KIND_THREAD
from .governance import egress
from .governance import principal as principal_module
from .working_set_resolve import AnchorFacts, anchor_ref, anchor_visible
from .working_set_runtime import CACHE_SIZE, READY, UNAVAILABLE

MAP_REF = "exomem://map"
UNFILED_REF = "exomem://map/unfiled"
TOPIC_REF_PREFIX = "exomem://map/topic/"

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
    """What a ref names: a view kind, and for a topic its domain key as written."""

    kind: str
    key: str = ""


def in_view_namespace(ref: str) -> bool:
    """Whether `ref` belongs to the views. A ref that does but names no view is NOT_FOUND."""
    return ref == MAP_REF or ref.startswith(MAP_REF + "/")


def parse_view_ref(ref: str) -> ViewTarget | None:
    """The view a ref names, or None when it is not exactly one of the three."""
    if ref == MAP_REF:
        return ViewTarget(VIEW_MAP)
    if ref == UNFILED_REF:
        return ViewTarget(VIEW_UNFILED)
    if ref.startswith(TOPIC_REF_PREFIX):
        key = ref[len(TOPIC_REF_PREFIX):]
        if key and len(key) <= TOPIC_KEY_MAX_CHARS and "/" not in key:
            return ViewTarget(VIEW_TOPIC, key)
    return None


def topic_ref(key: str) -> str:
    return TOPIC_REF_PREFIX + key


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
    def current(
        cls,
        vault_root: Path,
        *,
        purpose: str | None = None,
        principal: principal_module.RequestPrincipal | None = None,
    ) -> CallerView:
        """The view of `principal` (default: the bound one) under the `purpose` the request declared.

        The view's compile and the terminal guard both build it from the same two arguments, so
        they decide under one purpose and one filter.
        """
        who = principal if principal is not None else principal_module.effective_principal()
        # The purpose the release decision itself takes, so the filter and the binding agree.
        declared = egress._declared_purpose(Path(vault_root), who, purpose)  # noqa: SLF001
        visible = egress.restricted_release_filter(Path(vault_root), principal=principal, purpose=declared)
        binding = _digest(json.dumps([who.audience_id, who.authorization_session_id, declared]))
        return cls(visible, declared, binding)


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
class View:
    """A compiled view. `state` is READY, or why there is none; then `generation` is None and `rows` empty."""

    ref: str
    state: str
    generation: str | None
    stale: bool
    rows: tuple[Row, ...]
    #: How far the membership graph is built. Owner-only: a restricted caller is never told.
    graph: str | None = None
    #: The map's current state of each anchor and collection, by entry id, over the caller's view.
    states: dict[str, dict[str, Any]] | None = None
    #: False when the map's states could not be read; its items then carry none.
    state_available: bool = True


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
    root: Path
    target: ViewTarget
    #: The ref the view is known by: a deprecated or aliased topic key reads as its replacement.
    ref: str
    key: str
    registries: _Registries
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
    registries = _Registries(root)
    key = ""
    ref = path
    if target.kind == VIEW_TOPIC:
        resolved = registries.topic(target.key)
        if resolved is None:
            raise not_found()
        key, ref = resolved, topic_ref(resolved)
    state, catalogue, unlisted = (
        context_catalogue.ensure_unlisted(root, freshness_stamp=freshness_stamp)
        if freshness_stamp else context_catalogue.ensure_unlisted(root)
    )
    caller = caller or CallerView.current(root, purpose=purpose)
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
    # minted before any of them changed is stale.
    digest = hashlib.sha256(prepared.registries.digest.encode("ascii"))
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
        # nosemgrep: ep-word-set -- The current-state resolver fixes these wire fields.
        details = [f"{key}: {state[key]}" for key in ("as_of", "source", "path") if state.get(key)]
        if details:
            text += " (" + "; ".join(details) + ")"
    return f"{text} [{item['ref']}]"


def _map_item(view: View, row: Row) -> dict[str, Any]:
    item = row.item()
    if view.states is not None:
        item["state"] = view.states.get(row.entry_id)
    return item


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
    lines = [map_item_line(_map_item(view, row)) for row in rows]
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


# The activation cache shares the existing packet cache's resource bound. Restricted builds never enter it.
_OWNER_MAPS: OrderedDict[tuple[Any, ...], View] = OrderedDict()
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
) -> dict[str, Any]:
    """Serve the turn-independent map from the published catalogue for this caller."""
    if purpose is not None:
        with principal_module.request_scope(principal_module.effective_principal().with_purpose(purpose)):
            return _activation_packet(vault_root, max_chars, purpose, generation, timings)
    return _activation_packet(vault_root, max_chars, purpose, generation, timings)


def _activation_packet(
    root: Path, max_chars: int, purpose: str | None, generation: Mapping[str, Any],
    timings: find_types.FindTimings | None,
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
        if view is None:
            assert catalogue is not None
            key = (
                Path(root).absolute(), catalogue.token(), prepared.registries.digest,
                str(generation.get("freshness_key") or ""), prepared.caller.binding,
                metadata.get("index_generation"), metadata["roles_hash"], metadata["conventions_hash"],
                prepared.unlisted, tuple(catalogue.pending()),
            )
            with _OWNER_MAPS_LOCK:
                view = None if prepared.caller.restricted else _OWNER_MAPS.get(key)
                if view is not None:
                    _OWNER_MAPS.move_to_end(key)
            with find_types.timing_span(timings, "working_set.map", source="cache" if view else "computed"):
                if view is None:
                    view = _compile(prepared, timings=timings)
                    if not prepared.caller.restricted and view.state_available and catalogue.token() == key[1]:
                        with _OWNER_MAPS_LOCK:
                            _OWNER_MAPS[key] = view
                            _OWNER_MAPS.move_to_end(key)
                            while len(_OWNER_MAPS) > CACHE_SIZE:
                                _OWNER_MAPS.popitem(last=False)
                else:
                    with find_types.timing_span(timings, "working_set.map_state", source="cache"):
                        pass
        metadata["map_generation"] = view.generation
        packet = working_set.abstained_packet(reason=view.state, max_chars=max_chars, generation=metadata)
        if view.state != READY:
            return packet
        description = "Index of what exists."
        lines = map_within(view, max_chars - len(description) - 1)
        selected = frozenset(lines[:-1])
        items = [_map_item(view, row) for row in view.rows if map_item_line(_map_item(view, row)) in selected]
        block = _response(view, items, None)
        block["description"] = description
        if lines:
            block["overflow"] = lines[-1]
        if not view.rows:
            block["reason"] = "empty"
        if not view.state_available:
            block["state_available"] = False
        packet["map"] = block
        packet["budget"]["used_chars"] = map_packet_chars(block)
        packet["abstained"] = False
        packet.pop("abstention", None)
        return packet
    finally:
        if catalogue is not None:
            catalogue.close()


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
    return {
        entry_of_ref[str(entry["anchor"])]: {
            key: entry[key] for key in ("statement", "as_of", "source", "path") if entry.get(key) is not None
        }
        for entry in entries
        if str(entry.get("anchor")) in entry_of_ref
    }


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
    items = [row.item() for row in page]
    extra: dict[str, Any] = {}
    if view.states is not None:
        if not view.state_available:
            extra["state_available"] = False
        for row, item in zip(page, items, strict=True):
            item["state"] = view.states.get(row.entry_id)
    return _response(view, items, token, extra)


# --------------------------------------------------------------------------- #
# The display text an item is served
# --------------------------------------------------------------------------- #


def _identity(item: Mapping[str, Any]) -> tuple[object, ...]:
    """The fields that name an entry on the wire. A value that is not a string matches no entry."""
    values = (item.get("kind"), item.get("anchor_kind") or "", item.get("ref"), item.get("path") or "")
    return tuple(value if isinstance(value, str) else None for value in values)


#: The fields of an item that are display text. The terminal guard serves an entry's own (`entry_display`).
DISPLAY_FIELDS = ("title", "description", "description_origin")  # nosemgrep: ep-word-set -- `Row`'s own field names.


def _display(row: Row) -> dict[str, str]:
    return {field: getattr(row, field) for field in DISPLAY_FIELDS}


def entry_display(
    vault_root: Path, items: Sequence[Mapping[str, Any]], caller: CallerView
) -> list[dict[str, str] | None]:
    """The title and description each item's own entry is served with, or None when `caller` can see no such entry.

    The terminal guard serves these in place of an item's own, so a description no entry has never
    reaches the wire. An item is matched by the fields that name an entry: its kind, anchor kind, ref
    and path. Only entries the caller may see are candidates, so a withheld entry never decides what
    an item gets. Of visible entries alike in all four, the one with the item's own text is kept
    when there is one, else the lowest entry id. A topic gets its domain's registry rendering.
    """
    root = Path(vault_root)
    candidates: dict[tuple[object, ...], list[Row]] | None = None
    registries: _Registries | None = None
    out: list[dict[str, str] | None] = []
    for item in items:
        ref = item.get("ref")
        if item.get("kind") == KIND_TOPIC:
            target = parse_view_ref(ref) if isinstance(ref, str) else None
            if target is None or target.kind != VIEW_TOPIC:
                out.append(None)
                continue
            registries = registries or _Registries(root)
            out.append(_display(_topic_row(target.key, registries, "")))
            continue
        if candidates is None:
            candidates = {}
            catalogue = context_catalogue.ContextCatalogue(root)
            try:
                entries = catalogue.entries()
            finally:
                catalogue.close()
            for entry in entries:
                if _admits(entry, caller.visible):
                    row = _row(entry, (), "")
                    candidates.setdefault(_identity(row.item()), []).append(row)
        alike = candidates.get(_identity(item), [])
        if not alike:
            out.append(None)
            continue
        own = [row for row in alike if (row.title, row.description) == (item.get("title"), item.get("description"))]
        out.append(_display(min(own or alike, key=lambda row: row.entry_id)))
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
    if view.graph is not None:
        out["membership_graph"] = view.graph
    out.update(extra or {})
    return out
