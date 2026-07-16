"""Structured, guarded mutation of first-class semantic units."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import access, edit, semantic_index, semantic_language_registry, semantic_writes, vault

ObserveOperation = Literal["add", "update", "remove", "validate"]

_OPERATIONS = frozenset({"add", "update", "remove", "validate"})
_COMPILED_PAGE_TYPES = frozenset(
    {
        "experiment",
        "failure",
        "insight",
        "pattern",
        "production-log",
        "research-note",
    }
)
_COMPACT_KIND = "observation"
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_UPDATED_RE = re.compile(r"^updated:.*$", re.MULTILINE)
_TAG_RE = re.compile(r"^[^\s#]{1,64}$")


@dataclass(slots=True)
class ObserveMemoryError(ValueError):
    code: str
    reason: str
    remediation: str | None = None

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


def observe_memory(
    vault_root: Path,
    *,
    path: str,
    operation: ObserveOperation = "add",
    category: str | None = None,
    content: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    context: str | None = None,
    relations: list[dict[str, Any]] | None = None,
    unit_ref: str | None = None,
    expected_fingerprint: str | None = None,
    expected_hash: str | None = None,
    transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Validate or atomically mutate one semantic unit in a compiled page."""
    op = str(operation or "").strip().lower()
    if op not in _OPERATIONS:
        raise ObserveMemoryError(
            "INVALID_OBSERVE_OPERATION",
            "operation must be add, update, remove, or validate",
        )
    if op in {"update", "remove"} and not unit_ref:
        raise ObserveMemoryError(
            "UNIT_REFERENCE_REQUIRED",
            f"{op} requires the current unit_ref",
        )
    if op in {"update", "remove"} and (
        not expected_hash or not expected_fingerprint
    ):
        raise ObserveMemoryError(
            "DRIFT_GUARDS_REQUIRED",
            f"{op} requires the current parent hash and unit fingerprint",
            "Re-read the parent and exact unit, then echo both expected_hash "
            "and expected_fingerprint.",
        )
    if op == "remove" and any(
        value is not None for value in (category, content, kind, tags, context, relations)
    ):
        raise ObserveMemoryError(
            "INVALID_OBSERVE_INPUT",
            "remove accepts only the parent, unit reference, and drift guards",
        )
    proposal_mode = "update" if op == "validate" and unit_ref else op
    if proposal_mode in {"add", "update"} and (
        category is None or not str(category).strip() or content is None
    ):
        raise ObserveMemoryError(
            "INVALID_OBSERVE_INPUT",
            f"{op} requires non-empty category and content fields",
        )

    try:
        editable = edit.load_editable(vault_root, path, expected_hash=expected_hash)
    except edit.EditError as error:
        code = "STALE_PARENT_HASH" if error.code == "STALE_EDIT" else error.code
        raise ObserveMemoryError(code, error.reason) from error

    current = semantic_index.current_parent_index_state(
        vault_root, editable.rel_path, source=editable.original_text
    )
    current_unit = None
    if unit_ref:
        resolution = current.document.resolve_unit(
            unit_ref,
            expected_fingerprint=expected_fingerprint,
        )
        if resolution.status != "found" or resolution.unit is None:
            code = {
                "stale": "STALE_UNIT_REFERENCE",
                "ambiguous": "AMBIGUOUS_UNIT_REFERENCE",
                "missing": "UNIT_NOT_FOUND",
            }.get(resolution.status, "UNIT_NOT_FOUND")
            raise ObserveMemoryError(
                code,
                f"unit reference is {resolution.status}; re-read the parent and retry",
            )
        current_unit = resolution.unit

    frontmatter, body, _ = vault.parse_frontmatter(editable.original_text)
    target_anchor: str | None = None
    removed_unit = None
    if proposal_mode == "remove":
        assert current_unit is not None
        after_body = _remove_unit(
            body,
            current_unit.span.start_offset,
            current_unit.span.end_offset,
        )
        removed_unit = current_unit
    else:
        assert content is not None
        selected_kind = _select_kind(
            vault_root,
            kind=kind,
            current_unit=current_unit if proposal_mode == "update" else None,
            projects=_page_projects(frontmatter),
            page_type=(
                str(frontmatter["type"]) if frontmatter.get("type") is not None else None
            ),
        )
        normalized_category = _canonical_category(str(category))
        normalized_content = str(content).strip()
        normalized_tags = _canonical_tags(tags or [])
        normalized_context = _canonical_context(context)
        normalized_relations = _canonical_relations(relations or [])
        explicit_rich_kind = bool(
            kind is not None
            and semantic_language_registry.normalize_label(kind) != _COMPACT_KIND
        )
        if normalized_relations and not explicit_rich_kind:
            raise ObserveMemoryError(
                "COMPACT_RELATIONS_REQUIRE_RICH_KIND",
                "typed unit relations require an explicit governed non-observation kind",
                "Select an explicit governed non-observation kind for rich form, "
                "or author a canonical note-level relation.",
            )
        existing_anchors = {
            unit.anchor
            for unit in current.document.units
            if unit.anchor and unit is not current_unit
        }
        target_anchor = (
            current_unit.anchor
            if current_unit is not None and current_unit.anchor
            else _generated_anchor(
                selected_kind,
                normalized_category,
                normalized_content,
                normalized_tags,
                normalized_context,
                normalized_relations,
                existing_anchors,
            )
        )
        rendered = _render_unit(
            kind=selected_kind,
            category=normalized_category,
            content=normalized_content,
            tags=normalized_tags,
            context=normalized_context,
            relations=normalized_relations,
            anchor=target_anchor,
        )
        if proposal_mode == "update":
            assert current_unit is not None
            after_body = _replace_unit(
                body,
                current_unit.span.start_offset,
                current_unit.span.end_offset,
                rendered,
            )
        else:
            after_body = (
                _add_compact(body, rendered)
                if selected_kind == _COMPACT_KIND
                else _append_rich(body, rendered)
            )

    after_source = _rebuild_source(
        editable.original_text,
        editable.fm_text,
        after_body,
        today=today or dt.date.today(),
    )
    try:
        preflight = semantic_writes.preflight_existing(
            vault_root,
            path=editable.rel_path,
            after_source=after_source,
            operation="observe",
            expected_before_hash=expected_hash or vault.content_hash(editable.original_text),
            transition_token=transition_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    except semantic_writes.SemanticWriteError as error:
        raise ObserveMemoryError(error.code, error.reason) from error

    if (
        preflight.before.page_type not in _COMPILED_PAGE_TYPES
        or access.access_tier(vault_root, editable.rel_path) != access.TIER_READ_WRITE
    ):
        raise ObserveMemoryError(
            "OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE",
            "observe_memory only mutates writable compiled pages",
        )

    proposed_unit = _unit_by_anchor(preflight.after.document, target_anchor)
    if proposal_mode != "remove" and proposed_unit is None:
        diagnostics = [item.code for item in preflight.after.document.errors]
        raise ObserveMemoryError(
            "INVALID_SEMANTIC_UNIT",
            "rendered unit did not round-trip through the governed parser"
            + (f" ({', '.join(diagnostics)})" if diagnostics else ""),
        )
    if proposal_mode != "remove":
        assert proposed_unit is not None
        _assert_round_trip(
            proposed_unit,
            kind=selected_kind,
            category=normalized_category,
            content=normalized_content,
            tags=normalized_tags,
            context=normalized_context,
            relations=normalized_relations,
            anchor=target_anchor,
        )

    if op == "validate":
        return _result(
            operation=op,
            path=editable.rel_path,
            before_hash=vault.content_hash(editable.original_text),
            after_hash=vault.content_hash(after_source),
            mutated=False,
            unit=proposed_unit,
            removed_unit=removed_unit,
            semantic=preflight.as_dict(),
        )

    if preflight.contract_result.should_block:
        raise ObserveMemoryError(
            "SEMANTIC_CONTRACT_BLOCKED",
            "semantic contract has blocking findings; validate first and supply "
            "the returned transition review fields when remediation requires them",
        )
    try:
        log_plan = vault.plan_log_writes(
            vault_root,
            date_iso=(today or dt.date.today()).isoformat(),
            op="observe",
            rel_path_no_ext=editable.rel_path.removesuffix(".md"),
            body=f"Structured semantic-unit {op} via observe_memory.",
            operation_token=(
                "observe:"
                + hashlib.sha256(
                    f"{op}\0{editable.rel_path}\0{preflight.before.source_hash}"
                    f"\0{preflight.after.source_hash}".encode()
                ).hexdigest()
            ),
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ObserveMemoryError(
            "LOG_PLAN_CONFLICT", "observe log update could not be planned safely"
        ) from error
    try:
        committed = semantic_writes.commit_existing(
            vault_root,
            preflight=preflight,
            auxiliary_writes=log_plan.writes,
        )
    except semantic_writes.SemanticWriteError as error:
        raise ObserveMemoryError(error.code, error.reason) from error

    final = semantic_index.current_parent_index_state(vault_root, editable.rel_path)
    final_unit = _unit_by_anchor(final.document, target_anchor)
    return _result(
        operation=op,
        path=editable.rel_path,
        before_hash=vault.content_hash(editable.original_text),
        after_hash=final.parent_source_hash,
        mutated=committed.mutated,
        unit=final_unit,
        removed_unit=removed_unit,
        semantic=committed.as_dict(),
    )


def _canonical_category(value: str) -> str:
    try:
        from .semantic_units import canonicalize_category

        return canonicalize_category(value)
    except ValueError as error:
        raise ObserveMemoryError("INVALID_SEMANTIC_CATEGORY", str(error)) from error


def _select_kind(
    vault_root: Path,
    *,
    kind: str | None,
    current_unit: Any | None,
    projects: tuple[str, ...],
    page_type: str | None,
) -> str:
    raw = kind
    if raw is None and current_unit is not None and current_unit.form == "rich":
        raw = current_unit.kind
    if raw is None or semantic_language_registry.normalize_label(raw) == _COMPACT_KIND:
        return _COMPACT_KIND
    registry = semantic_language_registry.load_registry(vault_root)
    resolution = semantic_language_registry.for_attached_projects(
        registry, projects
    ).resolve_kind(raw, page_type=page_type)
    if resolution.resolved is None or resolution.status in {
        "unregistered",
        "registry_invalid",
        "scope_violation",
        "deprecated",
    }:
        replacement = f"; use {resolution.replacement!r}" if resolution.replacement else ""
        raise ObserveMemoryError(
            "UNSUPPORTED_SEMANTIC_KIND",
            f"kind {raw!r} is not an active governed rich kind{replacement}",
        )
    if resolution.resolved == _COMPACT_KIND:
        return _COMPACT_KIND
    return resolution.resolved


def _page_projects(frontmatter: dict[Any, Any]) -> tuple[str, ...]:
    projects: set[str] = set()
    project = frontmatter.get("project")
    if project:
        projects.add(str(project))
    attached = frontmatter.get("projects")
    if isinstance(attached, (list, tuple)):
        projects.update(str(value) for value in attached if str(value))
    elif attached:
        projects.add(str(attached))
    return tuple(sorted(projects))


def _canonical_tags(values: list[str]) -> tuple[str, ...]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw in values:
        tag = unicodedata.normalize("NFKC", str(raw).strip()).casefold()
        valid = bool(
            _TAG_RE.fullmatch(tag)
            and (tag[0].isalpha() or tag[0].isdigit())
            and not tag.endswith("/")
            and "//" not in tag
            and all(char.isalpha() or char.isdigit() or char in "_-/" for char in tag)
        )
        if not valid:
            raise ObserveMemoryError(
                "INVALID_SEMANTIC_TAG", f"invalid compact observation tag: {raw!r}"
            )
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tuple(tags)


def _canonical_context(value: str | None) -> str | None:
    if value is None:
        return None
    context = str(value).strip()
    if not context or "\n" in context or "\r" in context:
        raise ObserveMemoryError(
            "INVALID_SEMANTIC_CONTEXT", "context must be non-empty single-line text"
        )
    return context


def _canonical_relations(values: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    relations: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != {"kind", "target"}:
            raise ObserveMemoryError(
                "INVALID_SEMANTIC_RELATION",
                f"relations[{index}] must contain exactly kind and target",
            )
        kind = semantic_language_registry.normalize_label(str(value["kind"]))
        target = str(value["target"]).strip()
        if not kind or not target or any(char in target for char in "\r\n"):
            raise ObserveMemoryError(
                "INVALID_SEMANTIC_RELATION",
                f"relations[{index}] has an empty or multiline kind/target",
            )
        relations.append((kind, target))
    return tuple(relations)


def _generated_anchor(
    kind: str,
    category: str,
    content: str,
    tags: tuple[str, ...],
    context: str | None,
    relations: tuple[tuple[str, str], ...],
    existing: set[str | None],
) -> str:
    payload = json.dumps(
        [kind, category, content, tags, context, relations],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prefix = "obs" if kind == _COMPACT_KIND else "unit"
    base = f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"
    candidate = base
    occurrence = 2
    while candidate in existing:
        candidate = f"{base}-{occurrence}"
        occurrence += 1
    return candidate


def _render_unit(
    *,
    kind: str,
    category: str,
    content: str,
    tags: tuple[str, ...],
    context: str | None,
    relations: tuple[tuple[str, str], ...],
    anchor: str,
) -> str:
    clean_content = content.strip()
    if not clean_content:
        raise ObserveMemoryError("INVALID_OBSERVE_INPUT", "content must not be empty")
    if kind == _COMPACT_KIND:
        if "\n" in clean_content or "\r" in clean_content:
            raise ObserveMemoryError(
                "INVALID_COMPACT_CONTENT",
                "compact observation content must fit on one Markdown line",
            )
        suffix = "".join(f" #{tag}" for tag in tags)
        if context is not None:
            suffix += f" ({context})"
        return f"- [{category}] {clean_content}{suffix} ^{anchor}"

    heading = kind.replace("_", " ").title()
    metadata = [f"- category: {category}", f"- id: {anchor}"]
    if tags:
        metadata.append(f"- tags: {', '.join(tags)}")
    if context is not None:
        metadata.append(f"- context: {context}")
    if relations:
        metadata.append(
            "- relations: " + ", ".join(f"{rel}: {target}" for rel, target in relations)
        )
    return f"## {heading}\n" + "\n".join(metadata) + f"\n\n{clean_content}"


def _replace_unit(body: str, start: int, end: int, rendered: str) -> str:
    if not 0 <= start <= end <= len(body):
        raise ObserveMemoryError("INVALID_UNIT_SPAN", "unit source span is outside its parent")
    original = body[start:end]
    trailing_newlines = original[len(original.rstrip("\r\n")) :]
    if trailing_newlines and not rendered.endswith(("\n", "\r")):
        rendered += trailing_newlines
    return body[:start] + rendered + body[end:]


def _remove_unit(body: str, start: int, end: int) -> str:
    if not 0 <= start <= end <= len(body):
        raise ObserveMemoryError("INVALID_UNIT_SPAN", "unit source span is outside its parent")
    if end < len(body) and body[end] == "\n":
        end += 1
    return body[:start] + body[end:]


def _add_compact(body: str, rendered: str) -> str:
    section_ends = _observation_section_ends(body)
    if len(section_ends) > 1:
        raise ObserveMemoryError(
            "AMBIGUOUS_OBSERVATIONS_SECTION",
            "parent contains multiple canonical ## Observations sections",
        )
    if not section_ends:
        separator = _append_separator(body)
        return f"{body}{separator}## Observations\n\n{rendered}\n"
    point = section_ends[0]
    before, after = body[:point], body[point:]
    separator = "" if before.endswith("\n\n") else "\n" if before.endswith("\n") else "\n\n"
    tail = "\n" if not after else "\n\n"
    return before + separator + rendered + tail + after


def _append_rich(body: str, rendered: str) -> str:
    separator = _append_separator(body)
    return f"{body}{separator}{rendered}\n"


def _append_separator(body: str) -> str:
    if not body or body.endswith("\n\n"):
        return ""
    return "\n" if body.endswith("\n") else "\n\n"


def _observation_section_ends(body: str) -> list[int]:
    lines = body.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    headings: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for line in lines:
        text = line.rstrip("\r\n")
        fence = _FENCE_RE.match(text)
        if fence_char is not None:
            if (
                fence is not None
                and fence.group("fence")[0] == fence_char
                and len(fence.group("fence")) >= fence_length
                and not fence.group("info").strip()
            ):
                fence_char = None
                fence_length = 0
            offset += len(line)
            continue
        if fence is not None:
            marker = fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            offset += len(line)
            continue
        heading = _HEADING_RE.match(text)
        if heading is not None:
            headings.append((offset, len(heading.group(1)), heading.group(2).strip()))
        offset += len(line)
    for index, (_start, level, title) in enumerate(headings):
        if level != 2 or semantic_language_registry.normalize_label(title) != "observations":
            continue
        end = len(body)
        for candidate_start, candidate_level, _ in headings[index + 1 :]:
            if candidate_level <= level:
                end = candidate_start
                break
        offsets.append(end)
    return offsets


def _rebuild_source(
    original: str,
    fm_text: str,
    body: str,
    *,
    today: dt.date,
) -> str:
    date = today.isoformat()
    updated = (
        _UPDATED_RE.sub(f"updated: {date}", fm_text, count=1)
        if _UPDATED_RE.search(fm_text)
        else fm_text.rstrip() + f"\nupdated: {date}"
    )
    closing = original.find("\n---\n")
    blank = (
        "\n"
        if closing >= 0 and original.startswith("\n", closing + len("\n---\n"))
        else ""
    )
    final_body = body if body.endswith("\n") else body + "\n"
    return f"---\n{updated}\n---\n{blank}{final_body}"


def _unit_by_anchor(document: Any, anchor: str | None) -> Any | None:
    if anchor is None:
        return None
    matches = [unit for unit in document.units if unit.anchor == anchor]
    return matches[0] if len(matches) == 1 else None


def _assert_round_trip(
    unit: Any,
    *,
    kind: str,
    category: str,
    content: str,
    tags: tuple[str, ...],
    context: str | None,
    relations: tuple[tuple[str, str], ...],
    anchor: str | None,
) -> None:
    relation_values = tuple((item.kind, item.target) for item in unit.relations)
    common_matches = bool(
        unit.kind == kind
        and unit.category_key == category
        and unit.content == content
        and unit.anchor == anchor
        and relation_values == relations
    )
    if kind == _COMPACT_KIND:
        matches = common_matches and unit.tags == tags and unit.context == context
    else:
        expected_metadata = {
            "category": category,
            "id": anchor,
            **({"tags": ", ".join(tags)} if tags else {}),
            **({"context": context} if context is not None else {}),
            **(
                {
                    "relations": ", ".join(
                        f"{relation}: {target}" for relation, target in relations
                    )
                }
                if relations
                else {}
            ),
        }
        matches = common_matches and dict(unit.metadata) == expected_metadata
    if not matches:
        raise ObserveMemoryError(
            "AMBIGUOUS_SEMANTIC_UNIT_CONTENT",
            "unit fields collide with Markdown suffix, metadata, or heading syntax",
            "Move reserved trailing syntax into explicit tags/context/relations fields "
            "or select content that round-trips without reinterpretation.",
        )


def _result(
    *,
    operation: str,
    path: str,
    before_hash: str,
    after_hash: str,
    mutated: bool,
    unit: Any | None,
    removed_unit: Any | None,
    semantic: dict[str, Any],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operation": operation,
        "path": path,
        "mutated": mutated,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "semantic": semantic,
    }
    if unit is not None:
        value["unit_ref"] = unit.unit_ref
        value["unit"] = unit.to_dict()
    if removed_unit is not None:
        value["removed_unit_ref"] = removed_unit.unit_ref
        value["removed_unit"] = removed_unit.to_dict()
    return value
