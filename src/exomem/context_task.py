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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from . import (
    activation_conventions,
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
    vault_skills,
    working_set,
    working_set_currency,
    working_set_index,
)
from .governance import egress, principal

log = logging.getLogger(__name__)

# Request resource bounds; these do not define any knowledge vocabulary.
MAX_CANDIDATES = 12
#: At most this many collection manifests are offered to the method-fit judgment, so one turn reads a bounded number.
MAX_COLLECTION_CARRIERS = 48
#: How much of a collection manifest's body the judgment reads as its description: enough for what one item is.
COLLECTION_DESCRIPTION_CHARS = 600
RESET_ADVICE = "Discard the skills, collections and approaches you hold."
READ_ADVICE = "Retrieval lead; read the source and its current state before using it."
#: The `kind` of a method item that is a whole vault skill rather than one unit of a page.
SKILL_KIND = vault_skills.KIND_SKILL
#: The `kind` of a method item that is a whole collection manifest: the collection's authored contract.
COLLECTION_KIND = vault_skills.KIND_COLLECTION
#: The `selection` of a method that the method-fit judgment chose for this turn, rather than retrieval.
METHOD_FIT = "method_fit"

# Use the serving serializer for admitted YAML scalars, including authored dates.
_PUBLIC_SOURCE = TypeAdapter(Any)


def empty(state: str = "empty", *, reset: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "state": state, "candidates": [], "methods": {"reset": reset, "items": []},
        "missing": [], "interpretation": {"state": "disabled"}, "method_fit": {"state": "disabled"},
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
    if method and item.get("kind") in (SKILL_KIND, COLLECTION_KIND):
        # The name and description are page text, so they are quoted and escaped like a method's title.
        name, description = (json.dumps(str(item.get(key, "")), ensure_ascii=False) for key in ("name", "description"))
        label = "Skill" if item.get("kind") == SKILL_KIND else "Collection"
        contract = item.get("contract")
        if not contract or not contract.get("text"):
            return f"{label} {name}: {description}. Load it with read_page before acting."
        # The contract is page text too: quoted and escaped, so no line of it reads as part of the instruction.
        text = json.dumps(str(contract["text"]), ensure_ascii=False)
        if contract.get("complete"):
            return f"{label} {name}: {description}. Its whole text follows; follow it where it fits: {text}"
        return (
            f"{label} {name}: {description}. Its opening follows; load the rest with read_page before acting: {text}"
        )
    if method:
        # The title is page text, so it is quoted and escaped: no title can read as part of the fixed instruction.
        title = json.dumps(str(item.get("attribution", "")), ensure_ascii=False)
        if item.get("approval") == procedure_approval.APPROVED:
            # The line is part of the method, so the allowance charges it with the method. It carries only the title.
            return f"Approved procedure: {title}. Read it, then follow it where it fits."
        return f"Method: {title}. Read the source for applicability and exclusions."
    line = f"{item.get('title', '')}: {item.get('reason', '')}"
    # The listed links are served beside the lead, so the allowance charges them with it.
    # The title is page text, so it is quoted and escaped exactly as the hook renderer quotes it.
    links = [
        f"{json.dumps(str(link.get('title', '')), ensure_ascii=False)} "
        f"[{link.get('target_ref', '')}]"
        + (f": {link['why']}" if link.get("why") else "")
        for link in item.get("links") or ()
    ]
    return f"{line} Links: {'; '.join(links)}" if links else line


#: Served once at the end of a block that carries at least one link.
LINKS_ADVICE = "Read a link when its reason bears on the answer."


def packet_chars(block: Mapping[str, Any]) -> int:
    lines = [item_line(item) for item in block.get("candidates", ())]
    methods = block.get("methods") or {}
    lines.extend(item_line(item, method=True) for item in methods.get("items", ()))
    if methods.get("reset"):
        lines.insert(0, RESET_ADVICE)
    if any(item.get("links") for item in block.get("candidates", ())):
        lines.append(LINKS_ADVICE)
    return len("\n".join(lines)) + (1 if lines else 0)


def _reasoned(block: dict[str, Any], link: dict[str, Any], reach: Reach | None = None) -> bool:
    """Build a link's `why` when the allocation first tries it; False when it cannot be listed.

    The reason reads pages, so a link the allowance never reaches costs no read; the line's length is part of
    what the allowance charges, so it is built before the link is measured. A reader view that cannot be read
    leaves the link unlisted and the lane reported failed, as an unreadable graph does, and the link's target
    is then a page the turn reaches that the response does not serve.
    """
    build = link.pop("build_why", None)
    if build is None:
        return True
    try:
        link["why"] = build()
    except egress.ReaderViewUnavailable:
        _add_missing(block, _LANE_FAILED)
        if reach is not None:
            reach.stop(_LANE_FAILED)
        return False
    return True


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


#: Every request-budget stage of task discovery is named under this prefix, from the whole lane down to its links.
DISCOVERY_STAGE_PREFIX = "working_set.task_context"


@dataclass
class Reach:
    """The pages this response's task context serves for the turn, and why discovery stopped short of them, if it did.

    A lane that decides which pages are served records its failure here as it happens. A deadline is read from the
    request budget's own record, which names every discovery stage it skipped or stopped. The block's `budget`
    reason cannot stand in for either: it also names a lead that did not fit the character allowance, a bound by
    design that leaves the served pages complete.
    """

    paths: set[str] = field(default_factory=set)
    stopped: str | None = None

    def stop(self, reason: str) -> None:
        if self.stopped is None:
            self.stopped = reason

    def gap(self) -> str | None:
        if self.stopped is not None:
            return self.stopped
        budget = request_budget.current()
        cut = budget is not None and any(
            stage.startswith(DISCOVERY_STAGE_PREFIX) for stage in (*budget.skipped, *budget.truncated)
        )
        return "budget" if cut else None


#: The discovery reason that a lane deciding the served pages failed, as `working_set.task_candidates` and
#: `working_set.attach_task_links` report it. Their other reasons are deadlines, read from the budget, or bounds.
_LANE_FAILED = "lane_failed"


def _serve_without_deadline() -> None:
    """The final guard re-reads only leads already served, so a deadline that has passed must not drop them."""


def full_source_access(root: Path, caller: Any, *, provider=None) -> bool:
    """The raw fast path requires unrestricted content, an empty policy and no bound writer.

    An enrolled collection store keeps the fast path: each page it owns is still decided on its own by the
    walk filter and by `quick_page_visible`, so a store outage withholds the pages the store owns. A failure
    to prove ownership still makes the walk filter raise, and the task context reports unavailable.
    """
    return provider is None and egress.unrestricted_source_access(root, purpose=caller.purpose)


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
        public = {"path": source_ref, "frontmatter": dict(page.frontmatter), "body": page.body}
        reference = procedure_approval.page_ref(page.frontmatter)
        result = SourceProjection(
            public, page.snapshot_hash, page.snapshot_hash, _with_skill(methods, public, reference),
            egress.without_wikilinks(page.title), page.page_type, reference,
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
            result = SourceProjection(released, page.snapshot_hash, version, _with_skill(methods, released, reference),
                                      egress.without_wikilinks(title) if title else None, carrier if isinstance(carrier, str) else None,
                                      reference)
    if cache is not None and provider is None:
        cache[source_ref] = result
    return result


def skill_ref(source: SourceProjection) -> str:
    """The identity of a page served as one method as a whole: its own ref, as the catalogue serves it."""
    return source.ref or source.public["path"]


def _holds_method(source: SourceProjection, ref: str) -> bool:
    """Whether `source` still offers `ref`: a method it lists, or itself when it lists none and is served whole."""
    return source.methods is not None and (ref in source.methods or (not source.methods and ref == skill_ref(source)))


def _with_skill(methods: tuple[str, ...] | None, public: Mapping[str, Any], reference: str | None) -> tuple[str, ...] | None:
    """`methods` led by the page's own ref when the page, as released to this caller, is a skill."""
    if methods is None or vault_skills.recognise(public["path"], public.get("frontmatter")) is None:
        return methods
    return (reference or public["path"], *methods)


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
            if source is None or source.version != item["source_version"] or not _holds_method(source, item["ref"]):
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


def contract_of(body: str, max_chars: int) -> dict[str, Any] | None:
    """The released body of a skill as its contract, cut at a line boundary to `max_chars`; None when 0."""
    if max_chars <= 0:
        return None
    text = body.strip()
    if len(text) <= max_chars:
        return {"text": text, "complete": True}
    return {"text": _cut_lines(text, max_chars), "complete": False}


def _cut_lines(text: str, max_chars: int) -> str:
    """The longest run of whole leading lines of `text` within `max_chars`, else a whole-word prefix of the first."""
    if max_chars <= 0:
        return ""
    head = text[:max_chars + 1]
    cut = head.rfind("\n")
    if cut > 0:
        return head[:cut].rstrip()
    words = head[:max_chars].rsplit(" ", 1)[0] if len(text) > max_chars else head
    return words.rstrip()


def _fit_contract(block: dict[str, Any], method: dict[str, Any], allowance: int) -> bool:
    """Cut `method`'s contract until the block fits `allowance`; False when it cannot fit even without one."""
    contract = method.get("contract")
    while packet_chars(block) > allowance and contract and contract.get("text"):
        overflow = packet_chars(block) - allowance
        # The rendered text is escaped, so a cut of the overflow alone can fall short; it repeats until it fits.
        contract["text"] = _cut_lines(contract["text"], len(contract["text"]) - overflow)
        contract["complete"] = False
    if contract is not None and not contract.get("text"):
        method.pop("contract")
    return packet_chars(block) <= allowance


def _method_items(
    source: SourceProjection, source_ref: str, *, whole: bool = False, selection: str | None = None,
    contract_chars: int = 0,
) -> list[dict[str, Any]]:
    """The method items one released source offers.

    `whole` serves a page that lists no method as one method itself, for a page its owner named as the method.
    A skill carries its released body as its contract, within `contract_chars`, and the lead's `selection`.
    """
    skill = vault_skills.recognise(source.public["path"], source.public.get("frontmatter"))
    references = source.methods or ((skill_ref(source),) if whole and source.methods == () else ())
    items = []
    for reference in references:
        method = {
            "ref": reference,
            "source_ref": source_ref, "source_version": source.version,
            "applicability": "read_required", "read_ref": source_ref,
        }
        if skill is not None and reference == skill_ref(source):
            # A skill page is one method as a whole: it carries its full description and no unit attribution.
            method.update(
                kind=SKILL_KIND, name=skill.name, read_ref=skill_ref(source),
                description=" ".join(egress.without_wikilinks(skill.description).split()),
            )
            if selection:
                method["selection"] = selection
            if contract := contract_of(str(source.public.get("body") or ""), contract_chars):
                method["contract"] = contract
        elif source.title:
            method["attribution"] = source.title
        items.append(method)
    return items


def _skill_description(source: SourceProjection) -> str:
    """A skill's authored trigger text, as released: its frontmatter `description`."""
    return str((source.public.get("frontmatter") or {}).get("description") or "")


def _body_text(source: SourceProjection) -> str:
    return str(source.public.get("body") or "")


def _selected_methods(
    root: Path, *, caller: Any, catalogue: Any, turn: str, conversation: Any, sources: dict, contract_chars: int,
    index_token: tuple[int, int, int] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The skills and collections the method-fit judgment selects for the turn, as whole methods with their contracts.

    The carriers are the visible skill pages and collection manifests, each offered by the text its author wrote to say
    what it is for. Only a caller with full source access is offered them, as for every method. The judgment is
    optional and default-off (`context_fit.select_methods`); any failure selects nothing.
    """
    from . import context_fit

    limit = int(activation_conventions.load_conventions(root).conventions.budget("method_fit_max_items") or 0)
    if limit <= 0 or caller.restricted:
        return [], {"state": "disabled"}
    try:
        if context_fit.configured(root, context_fit.METHOD_FIT_ENV) is None:
            return [], {"state": "disabled"}
    except (ValueError, OSError):
        # A missing, unreadable or in-vault placement file is a failed judgment, never a failed turn.
        log.warning("method-fit placement unusable", exc_info=True)
        return [], {"state": "unavailable"}
    try:
        pending = set(catalogue.pending())
        carriers: list[dict[str, Any]] = []
        # Skills first: when the input allowance runs out, the user's own skills are offered before collections.
        for entry in catalogue.entries_by_id(catalogue.skill_ids()):
            if entry.path and entry.path not in pending:
                carriers.append({
                    "name": entry.skill, "path": entry.path, "kind": SKILL_KIND, "description_of": _skill_description,
                    "max_chars": vault_skills.DESCRIPTION_MAX_CHARS,
                })
        # A Records collection's manifest body is its authored contract. The manifests come from the index the update
        # published, so this enumerates no directory; their text is read only when the judgment offers them.
        manifests = working_set_index.records_manifests(root, index_token[1], token=index_token) if index_token else ()
        for manifest in manifests[:MAX_COLLECTION_CARRIERS]:
            path = str(getattr(manifest, "path", ""))
            if path:
                carriers.append({
                    "name": str(manifest.title or path), "path": path, "kind": COLLECTION_KIND,
                    "description_of": _body_text, "max_chars": COLLECTION_DESCRIPTION_CHARS,
                })
    except Exception:  # noqa: BLE001 - a carrier list that cannot be read in time is a failed judgment, never a failed turn
        log.warning("method-fit carriers unreadable", exc_info=True)
        return [], {"state": "unavailable"}
    chosen, state = context_fit.select_methods(
        root, caller=caller, carriers=carriers, turn=turn, conversation=conversation, limit=limit, sources=sources,
    )
    methods = []
    for carrier in chosen:
        try:
            methods.extend(_selected_method(root, carrier, caller, sources, contract_chars))
        except TaskDeadline:
            break
        except Exception:  # noqa: BLE001 - a selected method that cannot be read or built costs that method, never the turn
            log.warning("selected method unreadable", exc_info=True)
    return methods, state


def _selected_method(
    root: Path, carrier: dict[str, Any], caller: Any, sources: dict, contract_chars: int,
) -> list[dict[str, Any]]:
    """The served items of one selected carrier: a skill's whole method, or a collection with its contract."""
    source = project_source(root, carrier["path"], caller, cache=sources)
    if source is None:
        return []
    if carrier["kind"] == SKILL_KIND:
        items = _method_items(source, carrier["path"], whole=True, selection=METHOD_FIT, contract_chars=contract_chars)
        return [item for item in items if item.get("kind") == SKILL_KIND]
    method = {
        "ref": skill_ref(source), "source_ref": carrier["path"], "source_version": source.version,
        "applicability": "read_required", "read_ref": skill_ref(source), "kind": COLLECTION_KIND,
        "name": source.title or carrier["name"], "selection": METHOD_FIT,
        # The contract below carries the whole body, so the line's description is only its opening sentence.
        "description": vault_skills.summary(" ".join(egress.without_wikilinks(_body_text(source)).split()), 200),
    }
    if contract := contract_of(str(source.public.get("body") or ""), contract_chars):
        method["contract"] = contract
    return [method]


def _declared_methods(
    root: Path, caller: Any, leads: Sequence[Mapping[str, Any]], sources: dict, methods_by_source: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], bool, bool]:
    """The page each collection-manifest lead declares as its capture method, projected as a method.

    A lead's declaration is read from its own released frontmatter, so it names a page only to a caller who
    read the manifest. The named page is then projected by its own release decision, exactly as a lead is: a
    page the caller may not read is omitted, and reads as a declaration that names no page. A title or a stem
    reference resolves only through the broad resolver that is already resident, since building it reads the
    whole vault; a cold one costs this turn the method and schedules the build. A path or a ref costs one
    lookup. Each declaring lead costs at most one source projection. The method is advisory, so a failure
    on it costs the method and never the task context. Returns the method page by lead, whether the
    deadline cut the projection short, and whether a cold resolver left a reference unresolved.
    """
    from . import capture_method, find, structured_collections, vault

    cold = False

    def resident_resolver():
        nonlocal cold
        # A process with no watcher owner would schedule a build that nothing keeps current, as `working_set` decides.
        resolver = find.borrow_query_resolver(
            root, caller.visible, schedule_rebuild=vault.inbound_recovery_available(root),
        )
        cold = cold or resolver is None
        return resolver

    declared: dict[str, str] = {}
    for lead in leads:
        source = sources.get(lead["source_ref"])
        if source is None or source.public["path"].rpartition("/")[2] != structured_collections.MANIFEST_FILENAME:
            continue
        try:
            reference = structured_collections.parse_capture(source.public.get("frontmatter", {}).get("capture"))
        except structured_collections.CollectionError:
            continue  # a malformed declaration is the manifest's finding to report; it declares no method here
        if not reference:
            continue
        try:
            # Checked before the lookup, so a withheld page and a missing one behave alike on a spent budget.
            _deadline_check()
            path = capture_method.locate(root, reference, resolver_for=resident_resolver)
            if path is None or path == lead["source_ref"]:
                continue
            method = project_source(root, path, caller, cache=sources)
        except TaskDeadline:
            return declared, True, cold
        except Exception:  # noqa: BLE001 - the method is advisory: a failure on it costs the method, never the turn
            # Not reported: for a path-form reference, an error would reveal that a withheld page exists.
            continue
        if method is None:
            continue
        declared[lead["source_ref"]] = path
        if method.methods is not None and path not in methods_by_source and (items := _method_items(method, path, whole=True)):
            methods_by_source[path] = items
    return declared, False, cold


def compile_context(
    root: Path, *, caller: Any, catalogue: Any, turn: str, conversation: Any,
    snapshot: Any, held: Sequence[Mapping[str, str]], max_chars: int,
    timings: find_types.FindTimings | None = None,
    index_token: tuple[int, int, int] | None = None,
    prepare_slice: Callable[[], tuple[frozenset[str], int]] | None = None,
    reach: Reach | None = None,
) -> dict[str, Any]:
    """Revalidate before selection, then allocate complete source-bound read routes.

    `reach`, when given, receives the paths of the pages this response serves for the turn: each lead that this
    block or the turn slice routes, and each link target that the block lists. It also learns when a lane that
    decides those pages failed, or discovery did not run.
    """
    from . import working_set_runtime

    if not working_set_runtime.activation_released_corpus_tasks() and not full_source_access(root, caller):
        # Without the raw fast path, discovery and method revalidation would prepare released sources on this
        # request. It reports the state that a released corpus over its preparation allowance already reports,
        # and held methods reset as they do on every unavailable branch.
        if reach is not None:
            reach.stop("task_context_unavailable")
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
                discovered, missing, link_targets = working_set.task_candidates(
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
        if reach is not None and any(gap.get("reason") == _LANE_FAILED for gap in missing):
            reach.stop(_LANE_FAILED)
        candidates = []
        identities: dict[tuple[str, str], dict[str, Any]] = {}
        methods_by_source: dict[str, list[dict[str, Any]]] = {}
        contract_chars = int(
            activation_conventions.load_conventions(root).conventions.budget("method_contract_max_chars") or 0
        )
        for candidate in discovered:
            try:
                with find_types.timing_span(timings, "working_set.task_context_projection"):
                    source = project_source(root, candidate["source_ref"], caller, cache=sources)
            except TaskDeadline:
                # The leads already projected are complete; the rest stay unread and the block says so.
                _add_missing(block, "budget")
                break
            if source is None:
                # A discovered lead the caller cannot be served in full is a page the turn reaches and no block reads.
                _add_missing(block, _LANE_FAILED)
                if reach is not None:
                    reach.stop(_LANE_FAILED)
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
            if items := _method_items(
                source, candidate["source_ref"],
            ):
                methods_by_source.setdefault(candidate["source_ref"], []).extend(items)
        with find_types.timing_span(timings, "working_set.method_fit"):
            selected, block["method_fit"] = _selected_methods(
                root, caller=caller, catalogue=catalogue, turn=turn, conversation=conversation, sources=sources,
                contract_chars=contract_chars, index_token=index_token,
            )
        selected_paths = {method["source_ref"] for method in selected}
        declared_by_lead, cut_short, resolver_cold = _declared_methods(root, caller, candidates, sources, methods_by_source)
        if cut_short:
            _add_missing(block, "budget")
        if resolver_cold:
            # The miss comes before any page is looked up, so it says nothing of a page the caller may not read.
            _add_missing(block, "lane_failed")
        from . import context_fit

        with find_types.timing_span(timings, "working_set.context_fit"):
            candidates, block["interpretation"] = context_fit.interpret(
                root, caller=caller, candidates=candidates, turn=turn, conversation=conversation, sources=sources,
            )
        with find_types.timing_span(timings, "working_set.approvals"):
            kept = {item["source_ref"] for item in candidates}
            kept |= {declared_by_lead[path] for path in kept if path in declared_by_lead}
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
                if method.get("kind") == SKILL_KIND:
                    continue  # A skill is a whole page's method; an approval attaches to a procedure's units.
                method["approval"] = value
                if value == procedure_approval.APPROVED:
                    method["attribution"] = approved_title(sources[path])
                    # The read handle is the stable ref, never the path: a rename must not put words into the line.
                    method["read_ref"] = sources[path].ref
        target_paths: dict[str, str] = {}
        for reason in working_set.attach_task_links(
            root, candidates, link_targets=link_targets, caller=caller, timings=timings, target_paths=target_paths,
        ):
            _add_missing(block, reason)
            if reach is not None and reason == _LANE_FAILED:
                # The leads' link targets are pages the turn reaches; without them the served pages are short.
                reach.stop(_LANE_FAILED)
        # A lead is admitted together with its first connection when both fit; the rest fill the
        # room every lead and method leaves, in lead order. A lead's own method outranks its first
        # link, but a first link may take the room that a later lead would have used.
        linked: list[tuple[dict[str, Any], list[dict[str, str]], bool]] = []
        # The links each admitted lead may still list, by lead; its first link can return here.
        tails: dict[int, list[dict[str, str]]] = {}
        # A method the judgment selected for this turn is served first: it is the turn's answer to "how", so no
        # retrieval lead may take its room. Its contract is cut before the method is dropped.
        # Every selected method is seated by its plain line first, so one long contract never starves another
        # method of its place; the contracts then fill the room that is left, in selection order.
        contracts = {id(method): method.pop("contract", None) for method in selected}
        for method in selected:
            block["methods"]["items"].append(method)
            if packet_chars(block) > allowance:
                block["methods"]["items"].pop()
                _add_missing(block, "budget")
        for method in selected:
            if contracts[id(method)] and any(method is served for served in block["methods"]["items"]):
                method["contract"] = contracts[id(method)]
                _fit_contract(block, method, allowance)
        for item in candidates:
            links = item.pop("links", [])
            # Guarded turn leads already carry this route, and a page the method-fit judgment selected is routed by its
            # own method item; keep only the method projection for both.
            if item["source_ref"] in selected_paths:
                pass
            elif item["ref"] not in covered and item["source_ref"] not in covered:
                block["candidates"].append(item)
                if packet_chars(block) > allowance:
                    block["candidates"].pop()
                    _add_missing(block, "budget")
                elif links and _reasoned(block, links[0], reach):
                    item["links"] = links[:1]
                    if packet_chars(block) > allowance:
                        item.pop("links")
                        item["links_omitted"] = item.get("links_omitted", 0) + len(links)
                    else:
                        tails[id(item)] = links[1:]
                        linked.append((item, tails[id(item)], False))
            elif links:
                # The turn slice carries no links, so a named lead returns here only to list them.
                linked.append((item, links, True))
            for method in (*methods_by_source.get(item["source_ref"], ()), *methods_by_source.get(declared_by_lead.get(item["source_ref"], ""), ())):
                if any(method is served for served in block["methods"]["items"]):
                    continue  # a page that is both a lead and a lead's declared method is served once
                if method.get("kind") in (SKILL_KIND, COLLECTION_KIND) and method["source_ref"] in selected_paths:
                    continue  # the method-fit judgment already served this page whole, with its contract
                block["methods"]["items"].append(method)
                if packet_chars(block) > allowance and item.get("links") and id(item) in tails:
                    # The lead's own method outranks its first link, which returns if room remains.
                    first = item.pop("links")
                    if packet_chars(block) <= allowance:
                        tails[id(item)][:0] = first
                    else:
                        item["links"] = first
                if not _fit_contract(block, method, allowance):
                    block["methods"]["items"].pop()
                    _add_missing(block, "budget")
        for item, links, named in linked:
            if named:
                block["candidates"].append(item)
                if packet_chars(block) > allowance:
                    block["candidates"].pop()
                    _add_missing(block, "budget")
                    continue
            if not links:
                continue
            served = item.get("links")
            if served is None:
                served = item["links"] = []
            before = len(served)
            for link in links:
                if not _reasoned(block, link, reach):
                    break
                served.append(link)
                if packet_chars(block) > allowance:
                    served.pop()
                    break
            dropped = len(links) - (len(served) - before)
            if dropped:
                item["links_omitted"] = item.get("links_omitted", 0) + dropped
            if not served:
                del item["links"]
                if named:
                    # A named lead without one listed link would only repeat the turn slice's route.
                    block["candidates"].remove(item)
                    _add_missing(block, "budget")
        if reach is not None:
            _record_reach(reach.paths, candidates, block["candidates"], covered, target_paths)
        has_content = bool(block["candidates"] or block["methods"]["items"])
        block["state"] = "partial" if block["missing"] else ("ready" if has_content else "empty")
        _remember_sources(root, caller, sources)
        return block
    except Exception:  # noqa: BLE001 - optional discovery cannot discard a usable map
        log.warning("task context unavailable", exc_info=True)
        if reach is not None:
            reach.stop(_LANE_FAILED)
        return empty("unavailable", reset=bool(held))


def _record_reach(
    reach: set[str], candidates: Sequence[Mapping[str, Any]], served: Sequence[Mapping[str, Any]],
    covered: frozenset[str], target_paths: Mapping[str, str],
) -> None:
    """The paths of the served leads and of the link targets they list."""
    listed = {id(item) for item in served}
    for item in candidates:
        if id(item) not in listed and item["ref"] not in covered and item["source_ref"] not in covered:
            continue
        reach.add(item["source_ref"])
        for link in item.get("links") or ():
            path = target_paths.get(link["target_ref"])
            if path:
                reach.add(path)


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
                        or (method and not _holds_method(source, item["ref"]))):
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
        packet["map"], packet.get("turn_slice"), block, packet.get("next_action"),
    )
    return token
