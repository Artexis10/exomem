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
from pathlib import Path
from typing import Any

from . import (
    context_roles,
    find_corpus,
    find_types,
    lifecycle_statuses,
    semantic_index,
    working_set,
)
from .governance import egress, principal

log = logging.getLogger(__name__)

# Request resource bounds; these do not define any knowledge vocabulary.
MAX_CANDIDATES = 12
RESET_ADVICE = "Discard held method projections. Read a current source before reusing a method."
READ_ADVICE = "Retrieval lead; read the source and its current state before using it."


def empty(state: str = "empty", *, reset: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "state": state, "candidates": [], "methods": {"reset": reset, "items": []},
        "missing": [], "interpretation": {"state": "disabled"},
    }
    if reset:
        block["methods"]["advice"] = RESET_ADVICE
    return block


def item_line(item: Mapping[str, Any], *, method: bool = False) -> str:
    if method:
        return f"Method: {item.get('attribution', '')}. Read the source for applicability and exclusions."
    return f"{item.get('title', '')}: {item.get('reason', '')}"


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


def _revalidate(root: Path, held: Sequence[Mapping[str, str]], caller: Any) -> bool:
    basis = lifecycle_statuses.Basis(root)
    categories = context_roles.projection_categories(context_roles.load_roles(root), "method")
    references: dict[str, set[str]] = {}
    for item in held:
        if working_set.budget_exhausted("working_set.methods"):
            return True
        try:
            page = _source(root, item["source_ref"], caller)
            if (page is None or page.snapshot_hash != item["source_version"]
                    or not working_set._is_current_page(root, item["source_ref"], status_basis=basis)):
                return True
            if page.rel_path not in references:
                document = semantic_index.selected_parent_index_state(root, page.rel_path).document
                references[page.rel_path] = {
                    unit.unit_ref or page.rel_path for unit in document.units if unit.category in categories
                }
            if item["ref"] not in references[page.rel_path]:
                return True
        except Exception:  # noqa: BLE001 - absent, withheld and uncheckable invalidate identically
            return True
    return False


def compile_context(
    root: Path, *, caller: Any, catalogue: Any, turn: str, conversation: Any,
    snapshot: Any, held: Sequence[Mapping[str, str]], max_chars: int,
    timings: find_types.FindTimings | None = None,
    index_token: tuple[int, int, int] | None = None,
    prepare_slice: Callable[[], tuple[frozenset[str], int]] | None = None,
) -> dict[str, Any]:
    """Revalidate before selection, then allocate complete source-bound read routes."""
    with find_types.timing_span(timings, "working_set.methods"):
        reset = _revalidate(root, held, caller)
    block = empty(reset=reset)
    covered, protected = prepare_slice() if prepare_slice is not None else (frozenset(), 0)
    # Reserve the possible continuation-capacity reset before admitting complete projections.
    allowance = max_chars - protected - (len(RESET_ADVICE) + 1 if held and not reset else 0)
    if not turn.strip() and not conversation.focus:
        return block
    if working_set.budget_exhausted("working_set.task_context"):
        return empty("unavailable", reset=bool(held))
    try:
        with find_types.timing_span(timings, "working_set.task_context"):
            discovered, missing = working_set.task_candidates(
                root, catalogue=catalogue, caller=caller, turn=turn,
                focus=conversation.focus, snapshot=snapshot, limit=MAX_CANDIDATES,
                index_token=index_token,
            )
        block["missing"] = missing
        categories = context_roles.projection_categories(context_roles.load_roles(root), "method")
        candidates = []
        identities: dict[tuple[str, str], dict[str, Any]] = {}
        methods_by_source: dict[str, list[dict[str, Any]]] = {}
        for candidate in discovered:
            page = _source(root, candidate["source_ref"], caller)
            if page is None or not page.snapshot_hash:
                block["missing"] = [{"role": "task_context", "reason": "lane_failed"}]
                continue
            item = {
                **candidate, "source_version": page.snapshot_hash,
                "title": egress.without_wikilinks(page.title), "carrier": str(page.page_type or ""),
                "reason": READ_ADVICE, "read_ref": candidate["source_ref"],
            }
            identity = item["ref"], item["source_version"]
            previous = identities.get(identity)
            if previous is not None:
                previous["origins"] = list(dict.fromkeys((*previous["origins"], *item["origins"])))
                continue
            identities[identity] = item
            candidates.append(item)
            document = semantic_index.selected_parent_index_state(root, page.rel_path).document
            for unit in document.units:
                if unit.category not in categories:
                    continue
                method = {
                    "ref": unit.unit_ref or page.rel_path,
                    "source_ref": page.rel_path, "source_version": page.snapshot_hash,
                    "attribution": egress.without_wikilinks(page.title),
                    "applicability": "read_required", "read_ref": page.rel_path,
                }
                methods_by_source.setdefault(page.rel_path, []).append(method)
        from . import context_fit

        with find_types.timing_span(timings, "working_set.context_fit"):
            candidates, block["interpretation"] = context_fit.interpret(
                root, caller=caller, candidates=candidates, turn=turn, conversation=conversation,
            )
        for item in candidates:
            # Guarded turn leads already carry this route; keep only the additional method projection.
            if item["ref"] not in covered and item["source_ref"] not in covered:
                block["candidates"].append(item)
                if packet_chars(block) > allowance:
                    block["candidates"].pop()
                    block["missing"] = [{"role": "task_context", "reason": "lane_truncated"}]
            for method in methods_by_source.get(item["source_ref"], ()):
                block["methods"]["items"].append(method)
                if packet_chars(block) > allowance:
                    block["methods"]["items"].pop()
                    block["missing"] = [{"role": "task_context", "reason": "lane_truncated"}]
        has_content = bool(block["candidates"] or block["methods"]["items"])
        block["state"] = "partial" if block["missing"] else ("ready" if has_content else "empty")
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
        store[key] = copy.deepcopy(frozen)
    return frozen


def guarded(root: Path, caller: Any, block: Mapping[str, Any], *, limit: int) -> dict[str, Any]:
    """Restore only compiled fields and recheck each operative source version."""
    store = principal.invocation_served()
    from . import context_views

    key = (*context_views._served_key(root, "task_context", caller, block.get("generation"), None), limit)
    held = None if store is None else store.get(key)
    if held is None:
        return empty("unavailable", reset=True)
    result = copy.deepcopy(held)
    for section in (result["candidates"], result["methods"]["items"]):
        kept = []
        for item in section:
            try:
                page = _source(root, item["source_ref"], caller)
                if page is not None and page.snapshot_hash == item["source_version"]:
                    kept.append(item)
            except Exception:  # noqa: BLE001 - source races and admission failures release no projection
                pass
        if len(kept) != len(section):
            return empty("partial", reset=True)
    return result


def continuity_token(packet: dict[str, Any], *, held: Sequence[Mapping[str, str]], **payload: Any) -> str:
    """Carry every retained dependency, or reset the held set before bounded replacement."""
    from . import context_views, working_set_runtime

    block = packet["task_context"]
    methods = block["methods"]

    def versions(items):
        # These are fixed version-hint fields of the activation protocol.
        return [{key: item[key] for key in ("ref", "source_ref", "source_version")} for item in items]

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
        block["missing"] = [{"role": "task_context", "reason": "lane_truncated"}]
    packet["budget"]["used_chars"] = context_views.packet_chars(packet["map"], packet.get("turn_slice"), block)
    return token
