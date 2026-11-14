"""Request-local task leads and whole-source method freshness.

The catalogue owns discovery eligibility, recall owns ranking, and the reader
owns applicability. This projection adds no authored facts or durable state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from . import (
    context_roles,
    find_corpus,
    find_results,
    find_types,
    lifecycle_statuses,
    memory_refs,
    procedure_approval,
    provenance,
    request_budget,
    runtime_resources,
    semantic_index,
    semantic_units,
    vault,
    working_set,
    working_set_currency,
)
from .governance import egress, principal

log = logging.getLogger(__name__)

# Request resource bounds; these do not define any knowledge vocabulary.
MAX_CANDIDATES = 12
RESET_ADVICE = "Discard held methods."
READ_ADVICE = "Retrieval lead; read the source and its current state before using it."

# Use the serving serializer for admitted YAML scalars, including authored dates.
_PUBLIC_SOURCE = TypeAdapter(Any)


def empty(state: str = "empty", *, reset: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "state": state, "candidates": [], "methods": {"reset": reset, "items": []},
        "missing": [], "interpretation": {"state": "disabled"},
    }
    if reset:
        block["methods"]["advice"] = RESET_ADVICE
    return block


def _add_missing(block: dict[str, Any], reason: str) -> None:
    """Record a reason beside those already reported, so a later gap never hides an earlier one."""
    gap = {"role": "task_context", "reason": reason}
    if gap not in block["missing"]:
        block["missing"].append(gap)


def item_line(item: Mapping[str, Any], *, method: bool = False) -> str:
    if method:
        # The title is page text, so it is quoted and escaped: no title can read as part of the fixed instruction.
        title = json.dumps(str(item.get("attribution", "")), ensure_ascii=False)
        if item.get("approval") == procedure_approval.APPROVED:
            # The line is part of the method, so the allowance charges it with the method. It carries only the title.
            return f"Approved procedure: {title}. Read it, then follow it where it fits."
        return f"Method: {title}. Read the source for applicability and exclusions."
    line = f"{item.get('title', '')}: {item.get('reason', '')}"
    # The listed links are served beside the lead, so the allowance charges them with it.
    links = [f"{link['relation']} {link['title']} {link['target_ref']}" for link in item.get("links") or ()]
    return f"{line} Links: {'; '.join(links)}" if links else line


def packet_chars(block: Mapping[str, Any]) -> int:
    lines = [item_line(item) for item in block.get("candidates", ())]
    methods = block.get("methods") or {}
    lines.extend(item_line(item, method=True) for item in methods.get("items", ()))
    if methods.get("reset"):
        lines.insert(0, RESET_ADVICE)
    return len("\n".join(lines)) + (1 if lines else 0)


def _source(root: Path, source_ref: str, caller: Any):
    # Continuation references are read hints, so confinement and current admission precede every read.
    path = (root / source_ref).resolve()
    if (not path.is_relative_to(root.resolve())
            or path.relative_to(root.resolve()).as_posix() != source_ref
            or not caller.sees(source_ref)):
        return None
    if not egress.quick_page_visible(root, source_ref, purpose=caller.purpose):
        return None
    page = find_corpus.CACHE.get(path, root)
    if page is None or not caller.sees(source_ref) or not egress.quick_page_visible(root, source_ref, purpose=caller.purpose):
        return None
    return page


def _unit_candidates(page: Any, selected: Any) -> tuple[list[Any], dict[str, str]]:
    relation_registry = selected.definitions.snapshots["relations"].typed if selected.definitions is not None else None
    candidates, categories = [], {}
    for unit in selected.document.units:
        if not unit.unit_ref or any(
            unit.span.start_line <= error.line <= unit.span.end_line
            for error in selected.document.errors if error.line is not None
        ):
            continue
        categories[unit.unit_ref] = unit.category
        candidates.append(working_set.LaneItem(
            role="", level="unit", ref=unit.unit_ref, path=page.rel_path, title=page.title,
            text=unit.content, lifecycle=page.status or "", updated=unit.as_of or "", anchor=page.rel_path,
            provenance={
                **{key: getattr(unit, key) for key in semantic_units.GOVERNED_UNIT_METADATA_KEYS if getattr(unit, key)},
                "supersedes_targets": working_set_currency.relation_targets(
                    [relation.to_dict() for relation in unit.relations], registry=relation_registry,
                ),
            },
        ))
    return candidates, categories


def _method_refs(root: Path, page: Any, caller: Any, cache: dict, *, selected=None, admitted=None, check=None) -> tuple[str, ...]:
    """Current method identities after unit admission, shared by refresh and discovery."""
    key = page.rel_path, page.snapshot_hash
    if key in cache:
        return cache[key]
    basis = lifecycle_statuses.Basis(root)
    if not working_set._is_current_page(root, page.rel_path, status_basis=basis, page=page):
        cache[key] = ()
        return cache[key]
    (check or _deadline_check)()
    selected = selected or semantic_index.selected_parent_index_state(root, page.rel_path)
    if selected.parent_source_hash != page.snapshot_hash:
        raise ValueError("method source changed during selection")
    categories = context_roles.projection_categories(context_roles.load_roles(root), "method")
    if not any(unit.category in categories for unit in selected.document.units):
        cache[key] = ()
        return cache[key]
    candidates, category_by_ref = _unit_candidates(page, selected)
    if admitted is None:
        verdicts = egress.classify_units(root, [working_set._served_unit(item) for item in candidates], purpose=caller.purpose)
        admitted = tuple(item for item, verdict in zip(candidates, verdicts, strict=True) if verdict == egress.UNIT_KEPT)
    current = working_set_currency.annotate(admitted, status_basis=basis, visible=caller.visible)
    cache[key] = tuple(
        item.ref for item in current
        if category_by_ref[item.ref] in categories and not working_set_currency.state(item, status_basis=basis)[0]
    )
    return cache[key]


@dataclass(frozen=True, slots=True)
class SourceProjection:
    """One request's released source, with its raw guard kept outside the wire."""
    public: dict[str, Any]
    raw_hash: str
    version: str
    methods: tuple[str, ...] | None
    title: str | None
    carrier: str | None
    #: The page's stable memory ref, read from the raw frontmatter, for the approval verdict.
    ref: str | None = None


class TaskDeadline(TimeoutError):
    """The request deadline arrived while task sources were prepared, as distinct from any other timeout."""


def _deadline_check() -> None:
    if working_set.budget_exhausted("working_set.task_context"):
        raise TaskDeadline("task source preparation is unavailable")


def _serve_without_deadline() -> None:
    """The final guard re-reads only leads already served, so a deadline that has passed must not drop them."""


def full_source_access(root: Path, caller: Any, *, provider=None) -> bool:
    """The raw fast path requires unrestricted content, an empty policy and no bound writer.

    An enrolled collection store keeps the fast path: each page it owns is still decided on its own by the
    walk filter and by `quick_page_visible`, so a store outage withholds the pages the store owns. A failure
    to prove ownership still makes the walk filter raise, and the task context reports unavailable.
    """
    policy, _active = egress.gate_state(root)
    who = principal.effective_principal().with_purpose(caller.purpose)
    return (provider is None and egress.policy_empty_without_writer(root, policy)
            and egress.unrestricted_content_access(root, who))


def project_source(root: Path, source_ref: str, caller: Any, *, cache=None, expected_hash=None, provider=None, check=None):
    """Release one immutable source before its text, metadata or version can influence a task."""
    check = check or _deadline_check
    check()
    if cache is not None and source_ref in cache and expected_hash is None and provider is None:
        return cache[source_ref]
    path = (root / source_ref).resolve()
    if (not path.is_relative_to(root.resolve()) or path.relative_to(root.resolve()).as_posix() != source_ref
            or not caller.sees(source_ref) or not egress.quick_page_visible(root, source_ref, purpose=caller.purpose)):
        return None
    check()
    method_cache = {}
    if full_source_access(root, caller, provider=provider):
        if expected_hash is not None:
            _raw, guard = vault.read_bounded_guarded_bytes(
                root, source_ref, limit=runtime_resources.SEMANTIC_SOURCE_MAX_BYTES, expected_hash=expected_hash,
            )
            guard.recheck(root)
        page = _source(root, source_ref, caller)
        if page is None:
            return None
        try:
            methods = _method_refs(root, page, caller, method_cache, check=check)
        except Exception:  # noqa: BLE001 - failed method currency leaves the source read route usable
            methods = None
        result = SourceProjection(
            {"path": source_ref, "frontmatter": dict(page.frontmatter), "body": page.body},
            page.snapshot_hash, page.snapshot_hash, methods, egress.without_wikilinks(page.title), page.page_type,
            procedure_approval.page_ref(page.frontmatter),
        )
    else:
        with runtime_resources.semantic_preparation(root, path, check=check, expected_hash=expected_hash) as prepared:
            raw = prepared.source.encode("utf-8")
            page = find_corpus.parse_page(path, prepared.mtime, root, content=raw)
            if page is None:
                raise ValueError("task source is unavailable")
            who = principal.effective_principal().with_purpose(caller.purpose)
            views = [(who, caller)]
            if provider is not None:
                from .context_views import CallerView

                views.append((provider.with_purpose(caller.purpose), CallerView.current(root, principal=provider, purpose=caller.purpose)))
            policy, _active = egress.gate_state(root)
            # Extra unit omissions have no exact canonical L5 excerpt mapping; keep that source on ordinary pull surfaces.
            if any(egress._release_level_for_markdown_snapshot(
                root, source_ref, raw, principal=actor, purpose=caller.purpose, policy=policy,
            ) != egress.LEVEL_FULL for actor, _view in views):
                if cache is not None and provider is None:
                    cache[source_ref] = None
                return None
            selected = semantic_index.selected_parent_index_state(root, source_ref, source=prepared.source)
            if selected.parent_source_hash != page.snapshot_hash:
                raise ValueError("task source changed during selection")
            units, _categories = _unit_candidates(page, selected)
            kept = [True] * len(units)
            for actor, view in views:
                reader = egress.reader_view(root, principal=actor, purpose=caller.purpose, keep=view.visible)
                verdicts = (reader.verdicts([working_set._served_unit(item) for item in units]) if reader is not None
                            else egress.classify_units(root, [working_set._served_unit(item) for item in units], principal=actor, purpose=caller.purpose))
                kept = [previous and verdict == egress.UNIT_KEPT for previous, verdict in zip(kept, verdicts, strict=True)]
            prose_units = find_results.prose_units(root, page, selected.document.units)
            admitted_refs = {unit.unit_ref for unit in prose_units}
            admitted = tuple(item for item, keep in zip(units, kept, strict=True) if keep and item.ref in admitted_refs)
            admitted_refs = {item.ref for item in admitted}
            body = provenance.remove_carriers(page.body, provenance._merged(
                (unit.span.start_offset, unit.span.end_offset)
                for unit in selected.document.units if unit.unit_ref not in admitted_refs
            ))
            # These are the canonical page-read fields; raw hash and advisory file time remain private guard evidence.
            released = {"path": source_ref, "frontmatter": dict(page.frontmatter), "body": body}
            reference = memory_refs.ref_from_markdown(prepared.source)
            if reference is not None:
                released["ref"] = reference
            for actor, _view in views:
                with principal.request_scope(actor):
                    released = egress.annotate_page(root, released, principal=actor, purpose=caller.purpose)
                    if released is None or released.get("release_level", egress.LEVEL_FULL) != egress.LEVEL_FULL:
                        return None
                    released = egress.postfilter("read_memory", released, root, request_kwargs={"purpose": caller.purpose})
            if not isinstance(released, dict) or not isinstance(released.get("body"), str) or released.get("path") != source_ref:
                raise ValueError("released task source is unavailable")
            for field in ("content_hash", "content", "mtime"):  # Exact-byte read fields do not define this derived source version.
                released.pop(field, None)
            released = _PUBLIC_SOURCE.dump_python(released, mode="json", warnings="error")
            metadata = released.get("frontmatter")
            title = (vault.resolve_display_title(metadata, released["body"], path) if isinstance(metadata, Mapping) else None)
            carrier = metadata.get("type") if isinstance(metadata, Mapping) else None
            try:
                methods = _method_refs(root, page, caller, method_cache, selected=selected, admitted=admitted, check=check)
            except Exception:  # noqa: BLE001 - failed currency cannot remove admitted source prose
                methods = None
            prepared.guard.recheck(root)
            check()
            version = hashlib.sha256(json.dumps(released, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            result = SourceProjection(released, page.snapshot_hash, version, methods,
                                      egress.without_wikilinks(title) if title else None, carrier if isinstance(carrier, str) else None,
                                      reference)
    if cache is not None and provider is None:
        cache[source_ref] = result
    return result


def approvals(root: Path, projections: Mapping[str, SourceProjection]) -> tuple[dict[str, str], bool]:
    """The approval verdict of each released source for this caller, and whether every source could be checked.

    The projection exists only for a page the caller reads at FULL release, so the release leg is met here. A
    source that cannot be checked reads `unapproved`, because approval only ever adds an endorsement, and the
    flag lets the packet say so. The page's holders come from the reference index that every read already
    consults for a page's ref: one indexed read, only when a ledger entry makes holders matter, and never a
    walk of the vault. That index follows each write, so a page approved a moment ago reads `approved` here.
    The owner check runs only when a ledger holds an entry, and a check that fails leaves every source unchecked.
    """
    values = dict.fromkeys(projections, procedure_approval.UNAPPROVED)
    refs = {path: source.ref for path, source in projections.items() if source.ref}
    if not refs:
        return values, True
    ledgers = procedure_approval.ledgers(root, refs.values())
    owner = False
    if any(ledgers[ref].held for ref in refs.values()):
        try:
            owner = procedure_approval.owner_view(root, principal.effective_principal())
        except Exception:  # noqa: BLE001 - a failed owner check leaves the sources unapproved and keeps the packet
            log.warning("approval owner check unavailable", exc_info=True)
            return values, False
    named: dict[str, str | None] | None = None
    # A ledger that cannot be read leaves its source unchecked, even though its verdict reads unapproved.
    complete = not any(ledgers[ref].unavailable for ref in refs.values())

    def holders(path: str, ref: str) -> tuple[str, ...]:
        # The index names a ref for a path only when that page is the single valid owner of its id.
        nonlocal named
        if named is None:
            named = memory_refs.ReferenceIndex(root).refs_for_paths(list(refs), recall_reader=True)
        return (path,) if named.get(path) == ref else ()

    for path, ref in refs.items():
        source = projections[path]

        def read(path: str = path, source: SourceProjection = source) -> bytes:
            raw, _guard = vault.read_bounded_guarded_bytes(
                root, path, limit=runtime_resources.SEMANTIC_SOURCE_MAX_BYTES, expected_hash=source.raw_hash,
            )
            return raw

        try:
            values[path] = procedure_approval.served_verdict(
                root, path, ledgers[ref], owner=owner, full_release=True,
                read=read, holders=lambda path=path, ref=ref: holders(path, ref),
            ).value
        except Exception:  # noqa: BLE001 - an unchecked source stays unapproved and the packet reports a failed lane
            log.warning("approval verdict unavailable for %s", path, exc_info=True)
            complete = False
    return values, complete


def approved_title(source: SourceProjection) -> str:
    """The title an approved method shows: the approved page's own title or first heading, else its ref.

    It never falls back to the file name. A move keeps an approval when the bytes are unchanged, so a name
    given by a rename must not read as part of what the owner approved.
    """
    public = source.public
    title = vault.authored_title(public.get("frontmatter"), str(public.get("body") or ""))
    return egress.without_wikilinks(title) if title else str(source.ref or "")


def _revalidate(root: Path, held: Sequence[Mapping[str, str]], caller: Any, sources: dict) -> bool:
    checked: list[tuple[Mapping[str, str], SourceProjection]] = []
    for item in held:
        try:
            source = project_source(root, item["source_ref"], caller, cache=sources)
            if (source is None or source.version != item["source_version"]
                    or source.methods is None or item["ref"] not in source.methods):
                return True
            checked.append((item, source))
        except Exception:  # noqa: BLE001 - absent, withheld and uncheckable invalidate identically
            return True
    if not checked:
        return False
    # Approve and revoke change no page bytes, so the version check alone cannot see them. An entry that carries
    # no approval reads as unapproved, and an unchecked source reads the same, so it resets only a held approval.
    try:
        current, _complete = approvals(root, {item["source_ref"]: source for item, source in checked})
    except Exception:  # noqa: BLE001 - uncheckable invalidates like an absent source
        return True
    return any(
        item.get("approval", procedure_approval.UNAPPROVED) != current[item["source_ref"]] for item, _source in checked
    )


def _remember_sources(root: Path, caller: Any, sources: Mapping[str, SourceProjection | None]) -> None:
    store = principal.invocation_served()
    if store is not None:
        store[("task_context_sources", str(root), caller.binding)] = {
            path: source.raw_hash for path, source in sources.items() if source is not None
        }


def compile_context(
    root: Path, *, caller: Any, catalogue: Any, turn: str, conversation: Any,
    snapshot: Any, held: Sequence[Mapping[str, str]], max_chars: int,
    timings: find_types.FindTimings | None = None,
    index_token: tuple[int, int, int] | None = None,
    prepare_slice: Callable[[], tuple[frozenset[str], int]] | None = None,
) -> dict[str, Any]:
    """Revalidate before selection, then allocate complete source-bound read routes."""
    from . import working_set_runtime

    if not working_set_runtime.activation_released_corpus_tasks() and not full_source_access(root, caller):
        # Without the raw fast path, discovery and method revalidation would prepare released sources on this
        # request. It reports the state that a released corpus over its preparation allowance already reports,
        # and held methods reset as they do on every unavailable branch.
        return empty("unavailable", reset=bool(held))
    sources: dict[str, SourceProjection | None] = {}
    with find_types.timing_span(timings, "working_set.methods"):
        reset = _revalidate(root, held, caller, sources)
    block = empty(reset=reset)
    if prepare_slice is not None:
        # The slice compiles first and must leave discovery its reserve; a slow slice would otherwise spend the
        # whole budget and leave the call with the map alone.
        with request_budget.leaving(request_budget.task_context_reserve()):
            covered, protected = prepare_slice()
    else:
        covered, protected = frozenset(), 0
    # Reserve the possible continuation-capacity reset before admitting complete projections.
    allowance = max_chars - protected - (len(RESET_ADVICE) + 1 if held and not reset else 0)
    if not turn.strip() and not conversation.focus:
        return block
    if working_set.budget_exhausted("working_set.task_context"):
        # The map is already served and held methods were just revalidated: the block says the deadline cut
        # discovery short, and keeps the revalidated methods instead of resetting them for a budget reason.
        _add_missing(block, "budget")
        block["state"] = "partial"
        return block
    try:
        try:
            with find_types.timing_span(timings, "working_set.task_context"):
                discovered, missing = working_set.task_candidates(
                    root, catalogue=catalogue, caller=caller, turn=turn,
                    focus=conversation.focus, snapshot=snapshot, limit=MAX_CANDIDATES,
                    index_token=index_token, projections=sources, timings=timings,
                )
        except TaskDeadline:
            # The released corpus ran out of time before it ranked anything; a prefix would bias the ranking, so
            # no lead is served, and the revalidated methods stand as at the boundary above.
            _add_missing(block, "budget")
            block["state"] = "partial"
            return block
        block["missing"] = missing
        candidates = []
        identities: dict[tuple[str, str], dict[str, Any]] = {}
        methods_by_source: dict[str, list[dict[str, Any]]] = {}
        for candidate in discovered:
            try:
                with find_types.timing_span(timings, "working_set.task_context_projection"):
                    source = project_source(root, candidate["source_ref"], caller, cache=sources)
            except TaskDeadline:
                # The leads already projected are complete; the rest stay unread and the block says so.
                _add_missing(block, "budget")
                break
            if source is None:
                _add_missing(block, "lane_failed")
                continue
            item = {
                **candidate, "source_version": source.version,
                "reason": READ_ADVICE, "read_ref": candidate["source_ref"],
            }
            if caller.restricted:
                item["ref"] = source.public.get("ref") or source.public["path"]
            if source.title:
                item["title"] = source.title
            if source.carrier:
                item["carrier"] = source.carrier
            identity = item["ref"], item["source_version"]
            previous = identities.get(identity)
            if previous is not None:
                previous["origins"] = list(dict.fromkeys((*previous["origins"], *item["origins"])))
                continue
            identities[identity] = item
            candidates.append(item)
            if source.methods is None:
                _add_missing(block, "lane_failed")
            for reference in source.methods or ():
                method = {
                    "ref": reference,
                    "source_ref": candidate["source_ref"], "source_version": source.version,
                    "applicability": "read_required", "read_ref": candidate["source_ref"],
                }
                if source.title:
                    method["attribution"] = source.title
                methods_by_source.setdefault(candidate["source_ref"], []).append(method)
        from . import context_fit

        with find_types.timing_span(timings, "working_set.context_fit"):
            candidates, block["interpretation"] = context_fit.interpret(
                root, caller=caller, candidates=candidates, turn=turn, conversation=conversation, sources=sources,
            )
        with find_types.timing_span(timings, "working_set.approvals"):
            kept = {item["source_ref"] for item in candidates}
            verdicts, checked = approvals(
                root, {path: sources[path] for path in methods_by_source if path in kept},
            )
        if not checked:
            _add_missing(block, "lane_failed")
        for path, methods in methods_by_source.items():
            value = verdicts.get(path, procedure_approval.UNAPPROVED)
            # An unapproved method carries no field, so an ordinary method gains nothing; the absence reads unapproved.
            if value == procedure_approval.UNAPPROVED:
                continue
            for method in methods:
                method["approval"] = value
                if value == procedure_approval.APPROVED:
                    method["attribution"] = approved_title(sources[path])
                    # The read handle is the stable ref, never the path: a rename must not put words into the line.
                    method["read_ref"] = sources[path].ref
        linked: list[tuple[dict[str, Any], list[dict[str, str]], bool]] = []
        for item in candidates:
            # Links spend only what every lead and method leaves, so a link never displaces one.
            links = item.pop("links", [])
            # Guarded turn leads already carry this route; keep only the additional method projection.
            if item["ref"] not in covered and item["source_ref"] not in covered:
                block["candidates"].append(item)
                if packet_chars(block) > allowance:
                    block["candidates"].pop()
                    _add_missing(block, "budget")
                else:
                    linked.append((item, links, False))
            elif links:
                # The turn slice carries no links, so a named lead returns here only to list them.
                linked.append((item, links, True))
            for method in methods_by_source.get(item["source_ref"], ()):
                block["methods"]["items"].append(method)
                if packet_chars(block) > allowance:
                    block["methods"]["items"].pop()
                    _add_missing(block, "budget")
        for item, links, named in linked:
            if named:
                block["candidates"].append(item)
                if packet_chars(block) > allowance:
                    block["candidates"].pop()
                    _add_missing(block, "budget")
                    continue
            item["links"] = []
            for link in links:
                item["links"].append(link)
                if packet_chars(block) > allowance:
                    item["links"].pop()
                    break
            if len(links) > len(item["links"]):
                item["links_omitted"] = item.get("links_omitted", 0) + len(links) - len(item["links"])
            if not item["links"]:
                del item["links"]
                if named:
                    # A named lead without one listed link would only repeat the turn slice's route.
                    block["candidates"].remove(item)
                    _add_missing(block, "budget")
        has_content = bool(block["candidates"] or block["methods"]["items"])
        block["state"] = "partial" if block["missing"] else ("ready" if has_content else "empty")
        _remember_sources(root, caller, sources)
        return block
    except Exception:  # noqa: BLE001 - optional discovery cannot discard a usable map
        log.warning("task context unavailable", exc_info=True)
        return empty("unavailable", reset=bool(held))


def pin(root: Path, caller: Any, block: Mapping[str, Any], *, limit: int) -> dict[str, Any]:
    """Keep the actual compiled block in this invocation, under this caller."""
    frozen = copy.deepcopy(dict(block))
    frozen.pop("generation", None)
    frozen["generation"] = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()[:32]
    store = principal.invocation_served()
    if store is not None:
        from . import context_views

        key = (*context_views._served_key(root, "task_context", caller, frozen["generation"], None), limit)
        compiled = store.get(("task_context_sources", str(root), caller.binding), {})
        paths = {item["source_ref"] for item in (*frozen["candidates"], *frozen["methods"]["items"])}
        # The invocation record carries raw checks separately from the released block.
        store[key] = {"block": copy.deepcopy(frozen), "sources": {path: compiled.get(path) for path in paths}}
    return frozen


def guarded(root: Path, caller: Any, block: Mapping[str, Any], *, limit: int) -> dict[str, Any]:
    """Restore only compiled fields and recheck each operative source version."""
    store = principal.invocation_served()
    from . import context_views

    key = (*context_views._served_key(root, "task_context", caller, block.get("generation"), None), limit)
    held = None if store is None else store.get(key)
    if held is None:
        return empty("unavailable", reset=True)
    result = copy.deepcopy(held["block"])
    sources = {}
    # These two sections are fixed by the task-context wire schema.
    for section, method in ((result["candidates"], False), (result["methods"]["items"], True)):
        for item in section:
            try:
                path = item["source_ref"]
                expected = held["sources"].get(path)
                if expected is None:
                    return empty("partial", reset=True)
                if path not in sources:
                    sources[path] = project_source(
                        root, path, caller, expected_hash=expected, check=_serve_without_deadline,
                    )
                source = sources[path]
                if (source is None or source.version != item["source_version"]
                        or (method and (source.methods is None or item["ref"] not in source.methods))):
                    return empty("partial", reset=True)
            except Exception:  # noqa: BLE001 - source races and admission failures release no projection
                return empty("partial", reset=True)
    return result


def continuity_token(packet: dict[str, Any], *, held: Sequence[Mapping[str, str]], **payload: Any) -> str:
    """Carry every retained dependency, or reset the held set before bounded replacement."""
    from . import context_views, working_set_runtime

    block = packet["task_context"]
    methods = block["methods"]

    def versions(items):
        # These are fixed version-hint fields of the activation protocol. A method item carries `approval` only
        # when it is approved or stale, so the hint carries it only then.
        return [
            {**{key: item[key] for key in ("ref", "source_ref", "source_version")},
             **({"approval": item["approval"]} if "approval" in item else {})}
            for item in items
        ]

    retained = {} if methods["reset"] else {item["ref"]: dict(item) for item in held}
    retained.update({item["ref"]: item for item in versions(methods["items"])})

    def encode(items):
        return working_set_runtime.encode_continuity(**payload, method_versions=items)

    token = encode(list(retained.values()))
    if (len(retained) > working_set_runtime.CONTINUITY_MAX_REFS
            or len(token) > working_set_runtime.CONTINUITY_MAX_CHARS):
        methods.update(reset=True, advice=RESET_ADVICE)
        while True:
            current = versions(methods["items"])
            token = encode(current)
            if len(current) <= working_set_runtime.CONTINUITY_MAX_REFS and len(token) <= working_set_runtime.CONTINUITY_MAX_CHARS:
                break
            methods["items"].pop()
        block["state"] = "partial"
        _add_missing(block, "lane_truncated")
    packet["budget"]["used_chars"] = context_views.packet_chars(
        packet["map"], packet.get("turn_slice"), block, packet.get("stance"), packet.get("next_action"),
    )
    return token
