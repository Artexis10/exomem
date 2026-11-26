"""Structured, guarded mutation of first-class semantic units."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, assert_never, get_args

from . import (
    access,
    capabilities,
    edit,
    find,
    note_types,
    origin_bindings,
    page_ids,
    provenance,
    relation_registry,
    semantic_contract,
    semantic_index,
    semantic_kinds,
    semantic_labels,
    semantic_language_registry,
    semantic_units,
    semantic_writes,
    temporal,
    unit_edges,
    vault,
    working_set_currency,
)
from .cli_ops import OpError
from .governance import egress
from .kbdir import managed_form
from .mutation_timings import MutationTimings, write_timings_enabled

ObserveOperation = Literal["add", "update", "remove", "validate", "supersede", "assess"]
_OPERATIONS: tuple[str, ...] = get_args(ObserveOperation)
_COMPACT_KIND = "observation"
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_UPDATED_RE = re.compile(r"^updated:.*$", re.MULTILINE)
_TAG_RE = re.compile(r"^[^\s#]{1,64}$")
_ANCHOR_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")

#: The governed unit-metadata keys this command takes as arguments. The language's other
#: governed rows, such as a belief unit's credence, are written by hand and carried through
#: an edit as written, so this command never writes a belief on a holder's behalf.
# nosemgrep: ep-word-set -- this command's own argument names, fixed by its tool schema
_GOVERNED_ARGUMENT_KEYS = ("verdict", "check_by", "as_of", "until")

#: Metadata rows this module renders from its own arguments. Every *other*
#: authored row is carried through a reconstruction with its value intact.
#:
#: "With its value intact" rather than "verbatim": the row is re-emitted under
#: the parser's normalized key spelling, so `- source-note: x` comes back as
#: `- source_note: x`. That normalization already happened at parse time and is
#: what every consumer downstream sees; the authored value itself is untouched.
#:
#: This set is the whole reason the update path is safe. `_render_unit` rebuilds
#: a rich unit from arguments, so before governed metadata existed, any row not
#: listed here was silently deleted by an unrelated edit. Preserving the
#: remainder — not just the governed keys — keeps that true for the next
#: governed key, and for a row an author wrote by hand that the parser simply
#: does not interpret. Markdown is canonical; a mutation tool does not get to
#: delete bytes it does not understand.
_OWNED_METADATA_KEYS: frozenset[str] = frozenset(
    {"category", "id", "tags", "context", "relations"}
) | frozenset(_GOVERNED_ARGUMENT_KEYS)


@dataclass(slots=True)
class ObserveMemoryError(ValueError):
    code: str
    reason: str
    remediation: str | None = None
    resolution: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")

    def leaf_error(self) -> ValueError:
        """The leaf-contract refusal a caller reads: the code and reason, then the remediation."""
        message = f"{self.code}: {self.reason}"
        if self.remediation:
            message += f" Remediation: {self.remediation}"
        return ValueError(message)


def _legacy_routing_remediation(resolution: dict[str, Any]) -> str:
    """Keep unsupported structural edits with the existing authorized owner."""
    return (
        f"Use the existing authorized edit owner for {resolution['path']}; "
        "read its current content and hash first. If this surface cannot edit it, "
        "report that limit. Do not create a dated child or migrate the page for this correction."
    )


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
    verdict: str | None = None,
    check_by: str | None = None,
    as_of: str | None = None,
    until: str | None = None,
    id: str | None = None,
    unit_ref: str | None = None,
    unit: str | None = None,
    span: str | None = None,
    expected_fingerprint: str | None = None,
    expected_hash: str | None = None,
    transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
    reason: str | None = None,
    target_fingerprints: dict[str, str] | None = None,
    today: dt.date | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Validate or atomically mutate one semantic unit in a compiled or entity page.

    `validate_only` previews a supersede or assess, as `validate` previews an add or update:
    the reply carries the transition token that the same call then commits with.
    """
    op = str(operation or "").strip().lower()
    if op not in _OPERATIONS:
        raise ObserveMemoryError(
            "INVALID_OBSERVE_OPERATION",
            f"operation must be one of: {', '.join(_OPERATIONS)}",
        )
    # Only these operations have a preview entry; elsewhere the surface would queue
    # and report a preview as a write.
    if validate_only and op not in {"supersede", "assess"}:
        raise ObserveMemoryError(
            "INVALID_OBSERVE_INPUT",
            f"validate_only previews a supersede or assess; preview {op} as operation validate",
        )
    previewing = op == "validate" or bool(validate_only)
    if unit is not None and unit_ref:
        raise ObserveMemoryError(
            "INVALID_OBSERVE_INPUT",
            "give the unit id in `unit`, or unit_ref with its guards, not both",
        )
    if op in {"supersede", "assess"}:
        # The claim these operations correct is named exactly once: a unit id
        # or anchor, a raw unit_ref, or the exact text of one list item.
        named = sum(
            bool(target)
            for target in (unit, unit_ref, span and str(span).strip())
        )
        if named != 1:
            raise ObserveMemoryError(
                "INVALID_OBSERVE_INPUT",
                f"{op} names its target exactly once: `unit`, `unit_ref`, "
                "or an exact `span`",
            )
    if op == "supersede":
        if content is None or not str(content).strip():
            raise OpError(
                "INVALID_OBSERVE_INPUT",
                "The supersession is missing required fields.",
                "Provide the missing fields shown in the retry call.",
                details={"retry_with": {"content": "<text>"}},
            )
        if any(value is not None for value in (verdict, check_by, until, id)):
            raise ObserveMemoryError(
                "INVALID_OBSERVE_INPUT",
                "supersede takes no verdict, check_by, until, or id",
            )
    if op == "assess":
        if verdict is None and check_by is None and relations is None:
            raise ObserveMemoryError(
                "INVALID_OBSERVE_INPUT",
                "assess changes verdict, check_by, or relations; name at least one",
            )
        if any(
            value is not None
            for value in (category, content, tags, context, as_of, until, id)
        ):
            raise ObserveMemoryError(
                "INVALID_OBSERVE_INPUT",
                "assess changes only a unit's verdict, check_by, and relations; "
                "its text and other fields stay as they are",
            )
    # A unit id names the unit and the version the caller saw; the guards are filled from the
    # page the write reads under the lease, once the unit id has matched it.
    if op in {"update", "remove"} and not (unit_ref or unit):
        raise ObserveMemoryError(
            "UNIT_REFERENCE_REQUIRED",
            f"{op} requires the current unit_ref",
        )
    if op in {"update", "remove"} and unit is None and (
        not expected_hash or not expected_fingerprint
    ):
        raise ObserveMemoryError(
            "DRIFT_GUARDS_REQUIRED",
            f"{op} requires the current parent hash and unit fingerprint",
            "Re-read the parent and exact unit, then echo both expected_hash "
            "and expected_fingerprint.",
        )
    if op == "remove" and any(
        value is not None
        for value in (
            category,
            content,
            kind,
            tags,
            context,
            relations,
            verdict,
            check_by,
            as_of,
            until,
            id,
        )
    ):
        raise ObserveMemoryError(
            "INVALID_OBSERVE_INPUT",
            "remove accepts only the parent, unit reference, and drift guards",
        )
    proposal_mode = _proposal_mode(op, unit_ref=unit_ref, unit=unit)
    if proposal_mode in {"add", "update"}:
        # These are the closed required argument fields, spelled by the serving surface.
        missing = {}
        if category is None or not str(category).strip():
            missing["category"] = "<category>"
        if content is None:
            missing["content"] = "<text>"
        if missing:
            raise OpError(
                "INVALID_OBSERVE_INPUT",
                "The observation is missing required fields.",
                "Provide the missing fields shown in the retry call.",
                details={"retry_with": missing},
            )

    origin_block = None
    if content is not None:
        try:
            content, origin_block = origin_bindings.extract_origin_metadata(str(content))
        except provenance.OriginError as error:
            raise ObserveMemoryError(error.code, error.reason) from error

    try:
        editable = edit.load_editable(vault_root, path, expected_hash=expected_hash)
    except edit.EditError as error:
        if error.code in {"FRONTMATTER_REQUIRED", "OUTSIDE_GOVERNED_ROOT"}:
            # `load_editable` raised before resolving a canonical `rel_path`
            # (frontmatterless: found under Knowledge Base/ but refused before
            # reading further; outside-governed-root: never found under
            # Knowledge Base/ at all), so both resolutions below are derived
            # from the caller's own path text, normalised the same way
            # `edit._existing_page_outside_kb` normalises it (strip,
            # backslash to slash, lstrip leading "/", ".md" suffix) — one
            # shared normalised form for both branches. Casing is left as the
            # caller spelled it; the true on-disk casing is only known once a
            # governed read/edit actually succeeds against the page.
            given = _legacy_page_given_form(path)
            if error.code == "FRONTMATTER_REQUIRED":
                # Reachable only for a page `load_editable` already found
                # under Knowledge Base/, so re-root the given form the same
                # way `kbdir.kb_page_relative_form` would.
                rel_path = (
                    managed_form(given)
                )
            else:
                # `_existing_page_outside_kb` never returns a hit for a given
                # form that already starts with the KB prefix, so this is
                # exactly the vault-relative path of the real, outside-KB page.
                rel_path = given
            resolution = _legacy_edit_resolution(rel_path)
            raise ObserveMemoryError(
                error.code,
                error.reason,
                _legacy_routing_remediation(resolution),
                resolution=resolution,
            ) from error
        code = "STALE_PARENT_HASH" if error.code == "STALE_EDIT" else error.code
        raise ObserveMemoryError(code, error.reason) from error

    current = semantic_index.selected_parent_index_state(
        vault_root, editable.rel_path, source=editable.original_text
    )
    language = _page_language(current)
    current_unit = None
    if unit is not None:
        if op in {"supersede", "assess"}:
            unit_ref, expected_fingerprint = _bind_claim_id(
                vault_root, current.document, unit,
                definitions_unavailable=current.definitions_unavailable,
            )
        else:
            unit_ref, expected_fingerprint = _bind_unit_id(
                vault_root, current.document, unit,
                definitions_unavailable=current.definitions_unavailable,
            )
    if unit_ref:
        resolution = current.document.resolve_unit(
            unit_ref,
            expected_fingerprint=expected_fingerprint,
        )
        if resolution.status != "found" or resolution.unit is None:
            if current.definitions_unavailable and (resolution.status == "missing" or (
                resolution.status == "stale" and resolution.actual_fingerprint is None
            )):
                raise ObserveMemoryError(
                    "REGISTRY_UNAVAILABLE",
                    "the selected vocabulary definitions required to resolve this unit "
                    "are unavailable",
                    "Restore admitted definitions and read the unit again.",
                )
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
    dropped_relations: tuple[tuple[str, str], ...] = ()
    dropped_outcome: str | None = None
    lifted_frontmatter: list[tuple[str, str]] | None = None
    lift_resolver: Any = None
    link_warnings: list[str] = []
    if proposal_mode == "remove":
        assert current_unit is not None
        after_body = _remove_unit(
            body,
            current_unit.span.start_offset,
            current_unit.span.end_offset,
        )
        removed_unit = current_unit
    elif op in {"supersede", "assess"}:
        page_projects = _page_projects(frontmatter)
        page_type = _page_type(frontmatter)
        existing_anchors: set[str | None] = {
            unit.anchor
            for unit in current.document.units
            if unit.anchor
        }
        resolved = (
            _span_claim_target(body, str(span), current.document, existing_anchors)
            if span is not None
            else _resolved_claim(body, current_unit, existing_anchors)
        )
        if op == "supersede":
            assert content is not None
            rel_registry = (
                current.definitions.snapshots["relations"].typed
                if current.definitions is not None
                else relation_registry.core_registry()
            )
            superseding = _superseding_unit(
                current.document, rel_registry, editable.rel_path, resolved.unit
            )
            if superseding is not None:
                raise ObserveMemoryError(
                    "SUPERSEDE_TARGET_ALREADY_SUPERSEDED",
                    f"the claim is already superseded by unit "
                    f"{unit_id(superseding)}",
                    "Supersede the successor instead, or read the page's units "
                    "for the current ids.",
                )
            if kind is not None:
                selected_kind = _select_kind(
                    language,
                    kind=kind,
                    current_unit=None,
                    projects=page_projects,
                    page_type=page_type,
                )
            elif resolved.kind is not None:
                selected_kind = resolved.kind
            else:
                admitted = set(
                    _admitted_kind_keys(
                        language, projects=page_projects, page_type=page_type
                    )
                )
                replacements = sorted(language.kinds_with("replacement") & admitted)
                if len(replacements) != 1:
                    raise ObserveMemoryError(
                        "REPLACEMENT_KIND_REQUIRED",
                        f"{len(replacements)} admitted kinds carry the "
                        "replacement trait; the successor's kind must be named",
                        "Pass `kind` with one of the replacement kinds: "
                        + (", ".join(replacements) if replacements else "none admitted")
                        + ".",
                    )
                selected_kind = replacements[0]
            if selected_kind in semantic_kinds.compact_kinds():
                raise ObserveMemoryError(
                    "UNSUPPORTED_SEMANTIC_KIND",
                    "supersede writes a rich replacement unit; a compact "
                    "observation cannot carry a supersedes relation",
                )
            normalized_category = (
                _canonical_category(str(category))
                if category is not None and str(category).strip()
                else resolved.category
            )
            normalized_content = str(content).strip()
            if "[[" in normalized_content:
                normalized_content, link_warnings = vault.normalize_body_wikilinks(
                    normalized_content,
                    vault_root,
                    resolver=find.writer_resolver_snapshot(vault_root),
                )
            normalized_tags = _canonical_tags(tags or [])
            normalized_context = _canonical_context(context)
            normalized_relations = unit_edges.add_unit_edges(
                (unit_edges.supersession_edge(rel_registry, resolved.anchor),),
                _canonical_relations(relations or []),
            )
            normalized_verdict = None
            normalized_check_by = None
            normalized_as_of = _canonical_date(as_of, "as_of") if as_of else None
            normalized_until = None
            preserved_metadata: tuple[tuple[str, str], ...] = ()
            target_anchor = _generated_anchor(
                selected_kind,
                normalized_category,
                normalized_content,
                normalized_tags,
                normalized_context,
                normalized_relations,
                existing_anchors,
            )
            rendered = _render_unit(
                kind=selected_kind,
                category=normalized_category,
                content=normalized_content,
                tags=normalized_tags,
                context=normalized_context,
                relations=normalized_relations,
                anchor=target_anchor,
                as_of=normalized_as_of,
            )
            after_body = semantic_units.apply_splices(body, resolved.edits)
            after_body = _append_rich(after_body, rendered)
        else:
            # assess changes verdict, check_by, or relations — never the claim's text.
            normalized_verdict = _resolve_verdict(verdict, resolved.unit, language)
            normalized_check_by = _resolve_check_by(check_by, resolved.unit)
            added_relations = _canonical_relations(relations or [])
            normalized_category = resolved.category
            normalized_content = resolved.content
            if resolved.unit is not None and resolved.unit.form == "rich":
                selected_kind = str(resolved.kind)
                if kind is not None and _select_kind(
                    language,
                    kind=kind,
                    current_unit=None,
                    projects=page_projects,
                    page_type=page_type,
                ) != selected_kind:
                    raise ObserveMemoryError(
                        "INVALID_OBSERVE_INPUT",
                        "assess keeps the unit's kind",
                    )
                normalized_tags = tuple(resolved.unit.tags)
                normalized_context = resolved.unit.context
                normalized_relations = unit_edges.add_unit_edges(
                    ((relation.kind, relation.target) for relation in resolved.unit.relations),
                    added_relations,
                )
                normalized_as_of = resolved.unit.as_of
                normalized_until = resolved.unit.until
                preserved_metadata = _preserved_metadata(resolved.unit)
                target_anchor = resolved.anchor
                rendered = _render_unit(
                    kind=selected_kind,
                    category=normalized_category,
                    content=normalized_content,
                    tags=normalized_tags,
                    context=normalized_context,
                    relations=normalized_relations,
                    anchor=target_anchor,
                    verdict=normalized_verdict,
                    check_by=normalized_check_by,
                    as_of=normalized_as_of,
                    until=normalized_until,
                    preserved=preserved_metadata,
                )
                after_body = _replace_unit(
                    body,
                    resolved.unit.span.start_offset,
                    resolved.unit.span.end_offset,
                    rendered,
                )
            else:
                # A compact target has nowhere to put governed metadata, so an
                # assessment promotes it to a rich block at the body's end with
                # the same anchor; rewriting it in place under `## Observations`
                # would drop the compact lines below it into the new block.
                if kind is None:
                    raise ObserveMemoryError(
                        "COMPACT_METADATA_REQUIRES_RICH_KIND",
                        "governed unit metadata requires an explicit governed "
                        "non-observation kind",
                        _rich_kind_remediation(
                            language,
                            category=resolved.category,
                            projects=page_projects,
                            page_type=page_type,
                        ),
                    )
                selected_kind = _select_kind(
                    language,
                    kind=kind,
                    current_unit=None,
                    projects=page_projects,
                    page_type=page_type,
                )
                if selected_kind in semantic_kinds.compact_kinds():
                    raise ObserveMemoryError(
                        "UNSUPPORTED_SEMANTIC_KIND",
                        "assessment metadata requires a rich unit",
                    )
                normalized_tags = (
                    () if resolved.unit is None else tuple(resolved.unit.tags)
                )
                normalized_context = (
                    None if resolved.unit is None else resolved.unit.context
                )
                normalized_relations = unit_edges.add_unit_edges((), added_relations)
                normalized_as_of = None
                normalized_until = None
                preserved_metadata = ()
                target_anchor = resolved.anchor
                rendered = _render_unit(
                    kind=selected_kind,
                    category=normalized_category,
                    content=normalized_content,
                    tags=normalized_tags,
                    context=normalized_context,
                    relations=normalized_relations,
                    anchor=target_anchor,
                    verdict=normalized_verdict,
                    check_by=normalized_check_by,
                )
                after_body = _remove_unit(body, *resolved.span)
                after_body = _append_rich(after_body, rendered)
                removed_unit = resolved.unit
    else:
        assert content is not None
        page_projects = _page_projects(frontmatter)
        page_type = _page_type(frontmatter)
        selected_kind = _select_kind(
            language,
            kind=kind,
            current_unit=current_unit if proposal_mode == "update" else None,
            projects=page_projects,
            page_type=page_type,
        )
        normalized_category = _canonical_category(str(category))
        normalized_content = str(content).strip()
        if "[[" in normalized_content:
            # A link in a unit is stored as the vault stores one in a note body: `[[Title]]` becomes
            # the page's path form. A forward reference stays as written.
            normalized_content, link_warnings = vault.normalize_body_wikilinks(
                normalized_content, vault_root, resolver=find.writer_resolver_snapshot(vault_root)
            )
        normalized_tags = _canonical_tags(tags or [])
        normalized_context = _canonical_context(context)
        # add and update both render the unit's whole edge set, which the seam owns.
        normalized_relations = unit_edges.add_unit_edges((), _canonical_relations(relations or []))
        explicit_rich_kind = bool(
            kind is not None
            and semantic_language_registry.normalize_label(kind) != _COMPACT_KIND
        )
        if normalized_relations and not explicit_rich_kind:
            # A rich unit that keeps its kind on update carries its own relations, so only a unit
            # that is compact lifts a provenance link to the page.
            if selected_kind in semantic_kinds.compact_kinds():
                lift_resolver = find.writer_resolver_snapshot(vault_root)
                lifted_frontmatter = _liftable_frontmatter_targets(
                    current.definitions.snapshots["relations"].typed
                    if current.definitions is not None
                    else relation_registry.core_registry(),
                    normalized_relations,
                    vault_root=vault_root,
                    resolver=lift_resolver,
                )
            if lifted_frontmatter is None:
                raise ObserveMemoryError(
                    "COMPACT_RELATIONS_REQUIRE_RICH_KIND",
                    "typed unit relations require an explicit governed non-observation kind",
                    _rich_kind_remediation(
                        language,
                        category=normalized_category,
                        projects=page_projects,
                        page_type=page_type,
                    ),
                )
            # Every relation is a provenance link to captured material, so the unit commits
            # compact and each target lands in its page field in the same atomic write.
            normalized_relations = ()
        base_unit = current_unit if proposal_mode == "update" else None
        if base_unit is not None and relations is None and base_unit.relations:
            # `relations` replaces on update, so a call that omits it removes every relation the
            # unit had. A list the caller sent, even an empty one, is its stated intent.
            dropped_relations = tuple((item.kind, item.target) for item in base_unit.relations)
            dropped_outcome = _dropped_relations_outcome(
                dropped_relations, compact=selected_kind in semantic_kinds.compact_kinds()
            )
        normalized_verdict = _resolve_verdict(
            verdict, base_unit, language,
        )
        normalized_check_by = _resolve_check_by(check_by, base_unit)
        normalized_as_of = _resolve_governed_value(
            as_of, base_unit, key="as_of",
            normalize=lambda value: _canonical_date(value, "as_of"),
        )
        normalized_until = _resolve_governed_value(
            until, base_unit, key="until",
            normalize=lambda value: _canonical_date(value, "until"),
        )
        if (
            normalized_as_of and normalized_until
            and dt.date.fromisoformat(normalized_until) < dt.date.fromisoformat(normalized_as_of)
        ):
            raise ObserveMemoryError(
                "INVALID_SEMANTIC_DATE_ORDER", "until must be on or after as_of",
            )
        preserved_metadata = _preserved_metadata(base_unit)
        governed_passed = _governed_names(
            verdict=normalized_verdict,
            check_by=normalized_check_by,
            as_of=normalized_as_of,
            until=normalized_until,
        )
        if selected_kind == _COMPACT_KIND and (governed_passed or preserved_metadata):
            if base_unit is None or base_unit.form != "rich":
                # A compact unit, new or existing, carries no rows, so the only exit that applies
                # is to leave the fields out of this call.
                raise _compact_metadata_refusal(
                    passed=governed_passed,
                    kinds=_rich_kind_remediation(
                        language,
                        category=normalized_category,
                        projects=page_projects,
                        page_type=page_type,
                    ),
                )
            # Every exit named here has to actually exist. `verdict=""` and
            # `check_by=""` really do clear those rows, but no argument clears
            # an unowned row, so converting a unit that carries one means
            # editing the page to remove it first. Naming a remediation the
            # caller cannot perform is how a refusal becomes a dead end.
            raise ObserveMemoryError(
                "COMPACT_METADATA_REQUIRES_RICH_KIND",
                "governed unit metadata requires an explicit governed "
                "non-observation kind",
                "Compact observations carry no metadata rows. "
                + _rich_kind_remediation(
                    language,
                    category=normalized_category,
                    projects=page_projects,
                    page_type=page_type,
                )
                + " Or, to convert this unit to compact form, clear each governed field "
                "by passing an empty string and remove any remaining "
                "metadata rows by editing the page, since no argument clears a "
                "row this tool does not own.",
            )
        existing_anchors = {
            unit.anchor
            for unit in current.document.units
            if unit.anchor and unit is not current_unit
        }
        requested_anchor = _requested_anchor(id, existing_anchors)
        target_anchor = requested_anchor or (
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
            verdict=normalized_verdict,
            check_by=normalized_check_by,
            as_of=normalized_as_of,
            until=normalized_until,
            preserved=preserved_metadata,
        )
        if proposal_mode == "update":
            assert current_unit is not None
            if current_unit.form == "compact" and selected_kind != _COMPACT_KIND:
                # A promoted compact line cannot become a rich block in place:
                # the compact lines after it would fall inside the new block.
                # The line leaves and the block lands at the body end instead.
                after_body = _append_rich(
                    _remove_unit(
                        body,
                        current_unit.span.start_offset,
                        current_unit.span.end_offset,
                    ),
                    rendered,
                )
            else:
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

    # What the governed parser must read back for the rendered unit; a remove renders none.
    expected_unit: dict[str, Any] | None = None if proposal_mode == "remove" else {
        "kind": selected_kind,
        "category": normalized_category,
        "content": normalized_content,
        "tags": normalized_tags,
        "context": normalized_context,
        "relations": normalized_relations,
        "anchor": target_anchor,
        "verdict": normalized_verdict,
        "check_by": normalized_check_by,
        "as_of": normalized_as_of,
        "until": normalized_until,
        "preserved": preserved_metadata,
    }
    if origin_block is not None:
        after_body = origin_block + "\n\n" + after_body
    # Validation and its commit render the same authored day, including across midnight.
    pinned = temporal.parse(semantic_writes.transition_token_stamp(transition_token))
    authored_day = today or (pinned.day if pinned is not None else dt.date.today())
    fm_text = editable.fm_text
    lifted_cited: list[str] = []
    if lifted_frontmatter:
        fm_text, lifted_cited, lift_warnings = _lift_frontmatter_targets(
            fm_text,
            frontmatter,
            lifted_frontmatter,
            vault_root=vault_root,
            resolver=lift_resolver,
        )
        link_warnings.extend(lift_warnings)
    lifted_outcome = (
        _lifted_relations_outcome(lifted_cited, preview=previewing) if lifted_cited else None
    )
    after_source = _rebuild_source(
        editable.original_text,
        fm_text,
        after_body,
        today=authored_day,
    )
    # One collector spans preflight+commit, so the response accounts for the
    # whole governed write rather than two unrelated halves. `validate` never
    # commits, so it stays entirely uninstrumented and byte-identical.
    timings = MutationTimings() if not previewing else None
    try:
        try:
            with semantic_contract.call_context("write"):
                preflight = semantic_writes.preflight_existing(
                    vault_root,
                    path=editable.rel_path,
                    after_source=after_source,
                    operation="observe",
                    # The semantic seam is keyed on the newline-normalized logical
                    # source, so hand it that hash instead of forwarding the
                    # caller's `expected_hash`, which is over the file's raw bytes.
                    # `load_editable` has already enforced the caller's guard
                    # against those bytes; forwarding it again made one parameter
                    # carry two conventions, which no single value satisfies on a
                    # CRLF page. Matches what `edit` passes at the same seam.
                    expected_before_hash=editable.raw_hash,
                    transition_token=transition_token,
                    relation_disposition=relation_disposition,
                    relation_review_hash=relation_review_hash,
                    relation_review_reason=relation_review_reason,
                    validate_only=previewing,
                    stamp=temporal.stamp(authored_day),
                    timings=timings,
                )
        except semantic_writes.SemanticWriteError as error:
            raise ObserveMemoryError(error.code, error.reason) from error

        access_tier = access.access_tier(vault_root, editable.rel_path)
        if access_tier != access.TIER_READ_WRITE:
            # Same refusal code as an untyped page (compatibility), but a caller
            # cannot act on this one the way it can act on schema history: the
            # tier itself forbids the write, whatever the page's compiled type,
            # so no `resolution` is offered here.
            tools = capabilities.active_tools_text("observe_memory")
            raise ObserveMemoryError(
                "OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE",
                f"{tools} only mutates writable compiled pages",
                f"This page's access tier is {access_tier!r}, which is "
                f"policy-protected; {tools} has no override for it, "
                "regardless of the page's compiled type.",
            )
        # The authored type is looked up as written; a withheld definition refuses.
        note_type = note_types.Basis(vault_root).resolve(preflight.before.page_type).require()
        if note_type is None or not note_types.accepts_unit_writes(note_type):
            resolution = _legacy_edit_resolution(editable.rel_path)
            raise ObserveMemoryError(
                "OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE",
                f"{capabilities.active_tools_text('observe_memory')} only mutates "
                "writable compiled and entity pages",
                _legacy_routing_remediation(resolution),
                resolution=resolution,
            )

        proposed_unit = (
            _unit_by_anchor(preflight.after.document, target_anchor)
            if expected_unit is None
            else _round_tripped_unit(preflight.after.document, expected_unit)
        )

        if target_fingerprints is not None:
            # An episode correction must not change a different claim after target drift.
            # A refusal costs this leaf a re-read; ordinary page/heading edits keep their route.
            if not isinstance(target_fingerprints, dict):
                raise ObserveMemoryError(
                    "INVALID_UNIT_TARGETS", "target_fingerprints must be an object"
                )
            guards = []
            targets = {}
            for fact in semantic_contract.writer_authored_facts(
                preflight.after, preflight.after_corpus
            ):
                if fact.logical_source_ref != getattr(proposed_unit, "unit_ref", None):
                    continue
                if not semantic_contract.page_relation_has(
                    preflight.after, preflight.after_corpus, fact.canonical_relation, "corrects"
                ):
                    continue
                if fact.target_unit_status != "found" or fact.logical_target_ref is None:
                    raise ObserveMemoryError(
                        "EXACT_UNIT_TARGET_REQUIRED",
                        "a corrective target must name one current unit",
                    )
                targets[fact.logical_target_ref] = fact.target_unit_fingerprint
                state = preflight.after_corpus.pages[fact.logical_target_path]
                source, guard = vault.read_guarded_text(vault_root, Path(vault_root) / state.path)
                before_hash = preflight.before_corpus.pages[state.path].source_hash
                if vault.content_hash(source) != before_hash:
                    raise ObserveMemoryError(
                        "STALE_UNIT_TARGET", "a corrective target changed; read it again"
                    )
                if state.path != preflight.path:
                    guards.append(guard)
            if targets != target_fingerprints:
                raise ObserveMemoryError(
                    "STALE_UNIT_TARGET", "exact corrective target fingerprints do not match"
                )
            preflight = replace(preflight, read_dependency_guards=tuple(guards))

        if previewing:
            return _result(
                operation=op,
                path=editable.rel_path,
                before_hash=editable.raw_hash,
                after_hash=vault.content_hash(preflight.after_source),
                mutated=False,
                unit=proposed_unit,
                removed_unit=removed_unit,
                semantic=preflight.as_dict(),
                dropped_relations=dropped_relations,
                dropped_outcome=dropped_outcome,
                lifted_cited=lifted_cited,
                lifted_outcome=lifted_outcome,
                warnings=link_warnings,
            )

        try:
            log_plan = vault.plan_log_writes(
                vault_root,
                date_iso=temporal.stamp(today or temporal.now()),
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
            with semantic_contract.call_context("write"):
                committed = semantic_writes.commit_existing(
                    vault_root,
                    preflight=preflight,
                    history_reason=reason,
                    auxiliary_writes=log_plan.writes,
                    derived_auxiliary_writes=tuple(
                        ("operation-log", write) for write in log_plan.writes
                    ),
                    timings=timings,
                )
        except semantic_writes.SemanticWriteError as error:
            raise ObserveMemoryError(error.code, error.reason) from error

        final = semantic_index.selected_parent_index_state(vault_root, editable.rel_path)
        final_unit = _unit_by_anchor(final.document, target_anchor)
        return _result(
            operation=op,
            path=editable.rel_path,
            # Both hashes are the whole-file `content_hash` the caller is told to
            # echo back as the next `expected_hash` -- the same convention `get`
            # emits and `load_editable` compares. `final.parent_source_hash` is the
            # semantic index's hash of the *normalized* logical source, so reporting
            # it here handed the caller a value its own guard would then refuse on
            # any CRLF page. The preflight's authoritative source includes origin
            # normalization and is written verbatim by `commit_existing`.
            before_hash=editable.raw_hash,
            after_hash=vault.content_hash(preflight.after_source),
            mutated=committed.mutated,
            unit=final_unit,
            removed_unit=removed_unit,
            semantic=committed.as_dict(),
            dropped_relations=dropped_relations,
            dropped_outcome=dropped_outcome,
            lifted_cited=lifted_cited,
            lifted_outcome=lifted_outcome,
            timings=(
                timings.as_dict() if timings is not None and write_timings_enabled() else None
            ),
            warnings=link_warnings,
        )

    finally:
        if timings is not None:
            # The ledger gets the stages whatever the envelope flag says, and of a refused write
            # as well as of a committed one; see `MutationTimings.emit_call_spans`.
            timings.emit_call_spans()

def unit_id(unit: Any) -> str:
    """The short id that names one unit and its current version."""
    return page_ids.part_id(str(unit.unit_ref), str(unit.fingerprint))


def page_units(vault_root: Path, page: dict[str, Any]) -> Any:
    """The units of a page just read, each with the id an edit of it takes.

    A principal that sees a released view of the page gets `unavailable`: the unit refs and
    fingerprints come from the full page, so listing them would name what the view withholds.
    """
    if egress.caller_restricted(vault_root):
        return {
            "status": "unavailable",
            "reason": "this principal sees a released view of the page",
        }
    path = page.get("path")
    if not isinstance(path, str) or not path.endswith(".md"):
        return []
    selected = semantic_index.selected_parent_index_state(vault_root, path)
    if selected.definitions_unavailable:
        return {
            "status": "unavailable",
            "reason": (
                "the selected vocabulary definitions required to list all units are unavailable"
            ),
        }
    units = [unit for unit in selected.document.units if unit.unit_ref]
    rel_registry = (
        selected.definitions.snapshots["relations"].typed
        if selected.definitions is not None
        else relation_registry.core_registry()
    )
    # `superseded_by` is derived at read: the successor's `supersedes` edge is
    # the only record; nothing writes a back-pointer into the target.
    supersedes: dict[str, list[str]] = {}
    superseded_by: dict[str, list[str]] = {}
    for successor, target_unit in working_set_currency.same_page_supersessions(
        selected.document, path, rel_registry
    ):
        if successor.unit_ref and target_unit.unit_ref:
            supersedes.setdefault(unit_id(successor), []).append(unit_id(target_unit))
            superseded_by.setdefault(unit_id(target_unit), []).append(unit_id(successor))
    return [
        {
            "id": unit_id(unit),
            "category": unit.category,
            "text": unit.content,
            "as_of": unit.as_of,
            "supersedes": supersedes.get(unit_id(unit), []),
            "superseded_by": superseded_by.get(unit_id(unit), []),
        }
        for unit in units
    ]


def _bind_unit_id(
    vault_root: Path, document: Any, unit: str, *, definitions_unavailable: bool = False,
) -> tuple[str, str]:
    """The unit_ref and fingerprint of the one unit `unit` names, or a refusal that says why.

    A principal that sees a released view is never given unit ids (`page_units`), so a unit reads
    as unknown to it, and a stale-id refusal cannot name the current id of a withheld unit.
    """
    head, _, version = str(unit).partition(".")
    visible = not egress.caller_restricted(vault_root)
    found = [
        u for u in document.units
        if visible and u.unit_ref and page_ids.part_head(u.unit_ref) == head
    ]
    if not found:
        if definitions_unavailable:
            raise ObserveMemoryError(
                "REGISTRY_UNAVAILABLE",
                "the selected vocabulary definitions required to resolve this unit are unavailable",
                "Restore admitted definitions and read the unit again.",
            )
        raise ObserveMemoryError(
            "UNIT_NOT_FOUND",
            f"unit id {unit} names no unit of this note; read the note's units for the current ids",
        )
    if len(found) > 1:
        raise ObserveMemoryError(
            "AMBIGUOUS_UNIT_REFERENCE", f"unit id {unit} names more than one unit of this note"
        )
    current = found[0]
    if unit_id(current) != unit:
        raise ObserveMemoryError(
            "STALE_UNIT_REFERENCE",
            f"unit id {unit} is stale; the unit now has id {unit_id(current)}. "
            "Read the unit again and retry with the current id.",
        )
    return str(current.unit_ref), str(current.fingerprint)


def _bind_claim_id(
    vault_root: Path, document: Any, unit: str, *, definitions_unavailable: bool = False,
) -> tuple[str, str]:
    """The unit_ref and fingerprint of the claim `unit` names.

    A supersede or assess target may name its claim by anchor (the stable name a
    `supersedes` edge keeps, in any form the fragment resolver takes) or by the
    versioned unit id a read returned. An anchor names the claim as it stands, so
    it binds through the unit's current id; every id then binds as any other unit
    write binds it (`_bind_unit_id`), with its visibility rule and refusals.
    """
    text = str(unit).strip()
    if not egress.caller_restricted(vault_root):
        resolution = document.resolve_fragment(text)
        if resolution.status == "ambiguous":
            raise ObserveMemoryError(
                "AMBIGUOUS_UNIT_REFERENCE", f"unit {unit} names more than one unit of this note"
            )
        if resolution.unit is not None and resolution.unit.unit_ref:
            text = unit_id(resolution.unit)
    return _bind_unit_id(
        vault_root, document, text, definitions_unavailable=definitions_unavailable
    )


@dataclass(frozen=True, slots=True)
class _ResolvedClaim:
    """The claim a supersede or assess names: a parsed unit or a list item to convert."""

    unit: Any | None
    anchor: str
    category: str
    kind: str | None
    form: str
    content: str
    span: tuple[int, int]
    edits: tuple[tuple[int, int, str], ...] = ()


def _resolved_claim(
    body: str, unit: Any, existing_anchors: set[str | None],
) -> _ResolvedClaim:
    """A parsed unit as a claim target, gaining an anchor by splice when it has none."""
    category = unit.category_key or _canonical_category(unit.category or "")
    kind = unit.kind if unit.form == "rich" else None
    anchor = unit.anchor
    edits: tuple[tuple[int, int, str], ...] = ()
    if not anchor:
        anchor = _generated_anchor(
            kind or _COMPACT_KIND,
            category,
            unit.content,
            tuple(unit.tags),
            unit.context,
            (),
            existing_anchors,
        )
        if unit.form == "compact":
            edits = ((unit.span.end_offset, unit.span.end_offset, f" ^{anchor}"),)
        else:
            edits = (_rich_id_edit(body, unit, anchor),)
    return _ResolvedClaim(
        unit=unit,
        anchor=anchor,
        category=category,
        kind=kind,
        form=unit.form,
        content=unit.content,
        span=(unit.span.start_offset, unit.span.end_offset),
        edits=edits,
    )


def _span_claim_target(
    body: str, span: str, document: Any, existing_anchors: set[str | None],
) -> _ResolvedClaim:
    """The claim an exact span names: a unit it sits in, or the list item it converts."""
    targets = [
        target
        for target in semantic_units.claim_targets(body, document.units)
        # A rich unit's list rows are its metadata, not claims: `unit` names a rich unit.
        if target.unit is None or target.unit.form != "rich"
    ]
    matches = [target for target in targets if span in target.item.text]
    if len(matches) > 1:
        raise ObserveMemoryError(
            "SUPERSEDE_TARGET_AMBIGUOUS",
            f"the span lies inside {len(matches)} claim list items",
            "Quote more of the claim's exact text, or read the page's units "
            "and pass the unit's `unit` id.",
        )
    if not matches:
        raise ObserveMemoryError(
            "SUPERSEDE_TARGET_NOT_FOUND",
            "the span lies inside no top-level claim list item of this note",
            "Quote the exact text of one claim line, or read the page's units "
            "and pass the unit's `unit` id.",
        )
    target = matches[0]
    if target.unit is not None:
        return _resolved_claim(body, target.unit, existing_anchors)
    if target.refusal is not None:
        raise ObserveMemoryError("SUPERSEDE_TARGET_NOT_A_CLAIM", *_not_a_claim(target.refusal))
    if target.category is None:
        raise ObserveMemoryError(
            "INVALID_SEMANTIC_CATEGORY",
            "the claim's `##` section names no governed category",
            "Move the claim under a section that names a category, or pass "
            "`unit` naming the unit directly.",
        )
    anchor, conversion = claim_conversion(target, existing_anchors)
    return _ResolvedClaim(
        unit=None,
        anchor=anchor,
        category=target.category,
        kind=None,
        form="compact",
        content=target.item.text,
        span=(target.item.start, target.item.end),
        edits=(conversion,),
    )


def _not_a_claim(refusal: semantic_units.ClaimRefusal) -> tuple[str, str]:
    """Why a list item cannot become a unit in place, and the next step: one per refusal."""
    edit = capabilities.active_tools_text("edit_memory")
    other = "To correct a claim, quote that claim's own text or pass its `unit` id."
    match refusal:
        case "relation":
            return (
                "the span lies inside a typed relation line, which is a link, not a claim",
                f"To change the relation, edit its line with {edit}. {other}",
            )
        case "links_only":
            return (
                "the span lies inside a list item that holds only links",
                f"To change the links, edit the line with {edit}. {other}",
            )
        case "task":
            return (
                "the span lies inside a task item (`- [ ] ...`), which stays a task, not a claim",
                f"To change the task, edit its line with {edit}. {other}",
            )
        case "inline_field":
            return (
                "the span lies inside a Dataview inline field (`key:: value`); "
                "a converted line would change what Dataview reads",
                f"Edit the field's value with {edit}. {other}",
            )
        case "wraps":
            return (
                "the claim item runs past one line, so it cannot become a compact unit in place",
                f"Join the item into one line with {edit}, then send this call again.",
            )
    assert_never(refusal)


def claim_conversion(
    target: semantic_units.ClaimTarget, existing_anchors: set[str | None],
) -> tuple[str, tuple[int, int, str]]:
    """The anchor a convertible claim item gains and the splice that makes it a compact unit.

    The span path and the reviewed audit fix both convert through here: the item
    keeps its indent and text bytes and gains `[<category>]` and `^<anchor>`. The
    anchor joins `existing_anchors`, so a batch of conversions never collides.
    """
    item = target.item
    category = str(target.category)
    anchor = _generated_anchor(_COMPACT_KIND, category, item.text, (), None, (), existing_anchors)
    existing_anchors.add(anchor)
    return anchor, (item.start, item.end, f"{item.indent}- [{category}] {item.text} ^{anchor}")


def _rich_id_edit(body: str, unit: Any, anchor: str) -> tuple[int, int, str]:
    """The splice giving a rich unit an `- id:` row: after `category`, else first."""
    start = unit.span.start_offset
    end = unit.span.end_offset
    position = start
    fallback = start
    for index, line in enumerate(body[start:end].splitlines(keepends=True)):
        position += len(line)
        if index == 0:
            fallback = position
            continue
        key = semantic_units.rich_metadata_key(line)
        if key is None:
            break
        if key == "category":
            return (position, position, f"- id: {anchor}\n")
    return (fallback, fallback, f"- id: {anchor}\n")


def _superseding_unit(
    document: Any,
    rel_registry: relation_registry.RelationRegistry,
    path: str,
    unit: Any | None,
) -> Any | None:
    """The unit already superseding `unit` on this page, if any; a converted item has none."""
    if unit is None or unit.unit_ref is None:
        return None
    for successor, target in working_set_currency.same_page_supersessions(
        document, path, rel_registry
    ):
        if target.unit_ref == unit.unit_ref:
            return successor
    return None


def _legacy_page_given_form(path: str) -> str:
    """The caller's path text, normalised the way `edit._existing_page_outside_kb`
    normalises it: stripped, backslashes to slashes, leading `/` dropped, a
    `.md` suffix supplied. Shared by both `load_editable` failure branches
    that must build a legacy-page `resolution` before a canonical `rel_path`
    exists (`FRONTMATTER_REQUIRED`, `OUTSIDE_GOVERNED_ROOT`), so an inside-KB
    and an outside-KB caller path normalise through the exact same form
    before either branch decides what it means.
    """
    given = path.strip().replace("\\", "/").lstrip("/")
    return given if given.endswith(".md") else given + ".md"


def _legacy_edit_resolution(rel_path: str) -> dict[str, Any]:
    """A legacy correction stays with its existing authorized edit owner."""
    return {"action": "use-existing-edit-owner", "path": rel_path}


def _canonical_category(value: str) -> str:
    try:
        from .semantic_units import canonicalize_category

        return canonicalize_category(value)
    except ValueError as error:
        raise ObserveMemoryError("INVALID_SEMANTIC_CATEGORY", str(error)) from error


def _select_kind(
    language: semantic_language_registry.SemanticLanguageRegistry,
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
    registry = semantic_language_registry.for_attached_projects(
        language, projects
    )
    resolution = registry.resolve_kind(raw, page_type=page_type)
    keeps_its_kind = (
        current_unit is not None
        and current_unit.form == "rich"
        and current_unit.kind == resolution.resolved
    )
    refusal = _kind_refusal(registry, raw, resolution, keeps_its_kind=keeps_its_kind)
    if refusal is not None:
        raise refusal
    return str(resolution.resolved)


def _kind_refusal(
    registry: semantic_language_registry.SemanticLanguageRegistry,
    raw: str,
    resolution: Any,
    *,
    keeps_its_kind: bool,
) -> ObserveMemoryError | None:
    """Why this tool cannot write a unit of the resolved kind, or None when it can.

    `_select_kind` raises it and `_admitted_kind_keys` lists only the kinds it
    passes, so a remediation never offers a kind that the write then refuses.
    """
    if resolution.resolved is None or resolution.status in {
        "unregistered",
        "registry_invalid",
        "scope_violation",
        "deprecated",
    }:
        replacement = f"; use {resolution.replacement!r}" if resolution.replacement else ""
        return ObserveMemoryError(
            "UNSUPPORTED_SEMANTIC_KIND",
            f"kind {raw!r} is not an active governed rich kind{replacement}",
        )
    if registry.has(resolution.resolved, "credence") and not keeps_its_kind:
        # This tool takes no holder or credence, so a new belief from it would be nobody's.
        return ObserveMemoryError(
            "UNSUPPORTED_SEMANTIC_KIND",
            f"kind {raw!r} records one holder's credence, and "
            f"{capabilities.active_tools_text('observe_memory')} takes no holder or credence",
            _belief_remediation(),
        )
    return None


def _belief_remediation() -> str:
    """Where a belief is written instead: the page body, in the belief-unit form."""
    return (
        f"{semantic_units.BELIEF_FORM} Write that unit into the page body with "
        f"{capabilities.active_tools_text('edit_memory')}, or with "
        f"{capabilities.active_tools_text('remember')} for a new page."
    )


def _admitted_kind_keys(
    language: semantic_language_registry.SemanticLanguageRegistry,
    *,
    projects: tuple[str, ...],
    page_type: str | None,
) -> tuple[str, ...]:
    """The rich kind keys the page's selected registry admits for a new unit."""
    registry = semantic_language_registry.for_attached_projects(language, projects)
    return tuple(sorted(
        key
        for key in set(registry.registry.core_kinds) | set(registry.registry.kinds)
        if key not in semantic_kinds.compact_kinds()
        and _kind_refusal(
            registry, key, registry.resolve_kind(key, page_type=page_type), keeps_its_kind=False
        ) is None
    ))


def _rich_kind_remediation(
    language: semantic_language_registry.SemanticLanguageRegistry,
    *,
    category: str,
    projects: tuple[str, ...],
    page_type: str | None,
) -> str:
    """The named next step: pass `kind`, with the registry's admitted keys listed."""
    kinds = _admitted_kind_keys(language, projects=projects, page_type=page_type)
    hint = (
        f"`category` names the admitted kind {category!r}: pass `kind=\"{category}\"` instead. "
        if category in kinds else ""
    )
    return f"{hint}Pass `kind` with one of the admitted kinds: {', '.join(kinds)}."


def _compact_metadata_refusal(
    *, passed: tuple[str, ...], kinds: str, with_relations: bool = False
) -> ObserveMemoryError:
    """Governed metadata sent with a compact unit: name each field and both exits.

    The refusal for converting a rich unit to compact names no field, and its exits (convert
    the unit, edit the page to drop its rows) do not apply to a compact unit, which has no
    rows. Both exits here are a retry of the same call; `kinds` says which rich kinds to pass.
    A call that also passes relations keeps only the rich-kind exit: without its metadata, the
    page may still refuse its relations for the same reason.
    """
    fields = ", ".join(passed)
    remediation = (
        f"Keep {fields} and the relations by passing a rich kind. {kinds}"
        if with_relations
        else f"Retry one of two ways: omit {fields} to keep a compact observation, or keep "
        f"{fields} by passing a rich kind. {kinds}"
    )
    return ObserveMemoryError(
        "COMPACT_METADATA_REQUIRES_RICH_KIND",
        f"Governed unit metadata ({fields}) needs a rich kind, and a compact observation "
        "carries no metadata",
        remediation,
    )


def argument_refusal(arguments: Mapping[str, Any]) -> ObserveMemoryError | None:
    """The compact-metadata refusal that a call's arguments alone decide, or None.

    The busy intake queues a save and replays it later, so a save that is certain to be
    refused would otherwise come back as a queued receipt and fail only at the replay. This
    reads no page. A unit reference without `kind` keeps the unit's own kind, which only the
    page knows, so that call is left to the replay.
    """
    passed = _compact_names_passed(arguments)
    if not passed:
        return None
    return _compact_metadata_refusal(
        passed=passed,
        kinds=_core_kinds_remediation(),
        with_relations=bool(arguments.get("relations")),
    )


def _compact_names_passed(arguments: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The governed names a compact unit would carry, when the arguments alone pick compact.

    The one rule that both the busy intake and the direct path apply before reading a page.
    """
    mode = _proposal_mode(
        str(arguments.get("operation") or "").strip().lower(),
        unit_ref=arguments.get("unit_ref"),
        unit=arguments.get("unit"),
    )
    kind = arguments.get("kind")
    if kind is None:
        compact_kind = mode in {"add", "validate"}
    else:
        compact_kind = semantic_language_registry.normalize_label(kind) == _COMPACT_KIND
    if not compact_kind:
        return None
    # A blank value is no value, as `_resolve_governed_value` reads it on an add.
    return _governed_names(**{
        key: arguments[key]
        for key in _GOVERNED_ARGUMENT_KEYS
        if str(arguments.get(key) or "").strip()
    })


def refusal_before_page(
    vault_root: Path,
    *,
    page: Callable[[], str | None],
    operation: str,
    category: str | None,
    content: str | None,
    kind: str | None,
    unit: str | None,
    unit_ref: str | None,
    verdict: str | None,
    check_by: str | None,
    as_of: str | None,
    until: str | None,
    tags: list[str] | None,
    context: str | None,
    relations: list[dict[str, Any]] | None,
    validate_only: bool,
) -> ObserveMemoryError | None:
    """The compact-metadata refusal that an add's own arguments decide, or None.

    `observe_memory` reaches this refusal only after it has found and read the page, and finding a
    page by memory ref can walk the whole vault. A new unit's kind and governed fields are all in
    the arguments, so the caller refuses before it looks the page up. Only an add qualifies: an
    update keeps the unit's own kind and form, which only the page knows. Every refusal that
    `observe_memory` decides from the arguments before this one (category, tags, context, relations,
    dates and date order) is left to it, so it still wins. A call that passes relations is left to
    the page, which decides whether they refuse first. A refusal decided by the page (a missing
    path, an unknown reference, a stale hash, the page's verdict vocabulary, relations the page
    cannot lift) comes after this one.

    `page` names the page when it can do so without walking the vault, else None. Only the
    remediation reads the page, to list the rich kinds it admits; a page that `page` cannot name,
    or that cannot be read, gets the core kinds instead.
    """
    op = str(operation or "").strip().lower()
    # The two operations that write a new unit, as `observe_memory` handles them: without a unit
    # reference, `validate` previews an add.
    # nosemgrep: ep-word-membership -- operation names, fixed by the `ObserveOperation` tool schema
    if op not in ("add", "validate") or validate_only or unit is not None or unit_ref:
        return None
    if category is None or not str(category).strip() or content is None:
        return None
    passed = _compact_names_passed({
        "operation": op, "kind": kind,
        **dict(zip(_GOVERNED_ARGUMENT_KEYS, (verdict, check_by, as_of, until), strict=True)),
    })
    if not passed or relations:
        # Relations on a compact unit are refused before its metadata unless every one can move to
        # a page field, which only the page's relation registry and their targets decide.
        return None
    try:
        origin_bindings.extract_origin_metadata(str(content))
    except provenance.OriginError:
        return None
    try:
        # The argument checks that `observe_memory` makes before this refusal, in its order.
        _canonical_category(str(category))
        _canonical_tags(tags or [])
        _canonical_context(context)
        _canonical_relations(relations or [])
        dates = {
            key: _canonical_date(str(value), key)
            for key, value in (("as_of", as_of), ("until", until), ("check_by", check_by))
            if value is not None and str(value).strip()
        }
    except (ObserveMemoryError, ValueError):
        return None
    if "as_of" in dates and "until" in dates and dates["until"] < dates["as_of"]:
        return None  # INVALID_SEMANTIC_DATE_ORDER wins, as `observe_memory` checks order first
    rel_path = page()
    kinds = None if rel_path is None else _page_kinds_remediation(vault_root, rel_path, category)
    return _compact_metadata_refusal(passed=passed, kinds=kinds or _core_kinds_remediation())


def _page_kinds_remediation(vault_root: Path, rel_path: str, category: str) -> str | None:
    """The rich-kind step that `observe_memory` names for this page, or None if it is unreadable."""
    try:
        editable = edit.load_editable(vault_root, rel_path)
        current = semantic_index.selected_parent_index_state(
            vault_root, editable.rel_path, source=editable.original_text
        )
        normalized_category = _canonical_category(str(category))
    except (edit.EditError, ObserveMemoryError, OSError, ValueError):
        return None
    frontmatter, _body, _ = vault.parse_frontmatter(editable.original_text)
    return _rich_kind_remediation(
        _page_language(current),
        category=normalized_category,
        projects=_page_projects(frontmatter),
        page_type=_page_type(frontmatter),
    )


def _core_kinds_remediation() -> str:
    """The rich-kind step without a page: the core kinds, and the vault's own by reference."""
    kinds = _admitted_kind_keys(
        semantic_language_registry.core_registry(), projects=(), page_type=None
    )
    return f"Pass `kind` with a core rich kind ({', '.join(kinds)}) or one this vault defines."


def _proposal_mode(op: str, *, unit_ref: str | None, unit: str | None) -> str:
    """The operation as the write handles it: a preview that names a unit previews an update."""
    return "update" if op == "validate" and (unit_ref or unit) else op


def _governed_names(**values: Any) -> tuple[str, ...]:
    """The governed arguments that carry a value, named in `_GOVERNED_ARGUMENT_KEYS` order."""
    return tuple(key for key in _GOVERNED_ARGUMENT_KEYS if values.get(key) is not None)


def _page_type(frontmatter: dict[Any, Any]) -> str | None:
    return str(frontmatter["type"]) if frontmatter.get("type") is not None else None


def _page_language(current: Any) -> semantic_language_registry.SemanticLanguageRegistry:
    """The category language that a parent's selected definitions give, else the core one."""
    return (
        current.definitions.snapshots["categories"].typed
        if current.definitions is not None
        else semantic_language_registry.core_registry()
    )


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


def _liftable_frontmatter_targets(
    rel_registry: relation_registry.RelationRegistry,
    relations: tuple[tuple[str, str], ...],
    *,
    vault_root: Path,
    resolver: Any,
) -> list[tuple[str, str]] | None:
    """Each relation's (field, target) when every one is a source citation, else None.

    A relation lifts only when the registry gives it the closed `sources` behaviour, its core
    relation binds the page's `sources` field forward, and its target names captured material.
    A vault extension takes the field of its core parent, because only the shipped pack binds
    fields. Anything else keeps the compact refusal, so a supersession, an association or an
    evidence link never reaches a page field through this path.
    """
    from . import epistemic_graph, source_closure

    sources_field, _ = epistemic_graph.declared_sources_binding()
    lifted: list[tuple[str, str]] = []
    for kind, target in relations:
        definition = rel_registry.resolve(kind).definition
        if definition is None or not rel_registry.has_label(kind, "sources"):
            return None
        core = definition if definition.core else rel_registry.core.get(definition.parent or "")
        # The lift writes only the field whose closure the commit verifies; a relation bound to
        # another field keeps the refusal that names the rich form.
        if core is None or (sources_field, "forward") not in core.frontmatter_fields:
            return None
        # A target that is no captured material cannot be cited here; the refusal that names the
        # rich form is the honest answer, where the commit's closure refusal would say to drop it.
        if source_closure.resolve_reference(vault_root, target, resolver=resolver) is None:
            return None
        lifted.append((sources_field, target))
    return lifted


def _lift_frontmatter_targets(
    fm_text: str,
    frontmatter: dict[str, Any],
    lifted: list[tuple[str, str]],
    *,
    vault_root: Path,
    resolver: Any,
) -> tuple[str, list[str], list[str]]:
    """Splice each lifted target into its declared frontmatter field.

    The writer is the same canonical-link normaliser `create_note` runs for its
    `sources` argument, and the value lands in the canonical `[[path]]` form.
    Existing entries keep their bytes; a target the field already cites, in any
    spelling the normaliser reads as the same page, is not duplicated. Returns
    the patched YAML, the rendered links this call cites and any normaliser
    warnings.
    """
    from . import note, record_formats

    grouped: dict[str, list[str]] = {}
    for field, target in lifted:
        grouped.setdefault(field, []).append(target)
    cited: list[str] = []
    warnings: list[str] = []
    for field, targets in grouped.items():
        canonicals, field_warnings = note._normalize_sources(
            targets, vault_root=vault_root, resolver=resolver
        )
        warnings.extend(field_warnings)
        existing_raw = frontmatter.get(field)
        existing_values: list[Any] = (
            []
            if existing_raw is None
            else [existing_raw]
            if isinstance(existing_raw, str)
            else list(existing_raw)
        )
        existing_canonicals, _ = note._normalize_sources(
            [str(value) for value in existing_values], vault_root=vault_root, resolver=resolver
        )
        known = set(existing_canonicals)
        additions: list[str] = []
        for canonical in canonicals:
            rendered = vault.render_wikilink_target(canonical, vault_root)
            cited.append(rendered)
            if canonical in known:
                continue
            additions.append(rendered)
            known.add(canonical)
        if not additions:
            continue
        merged = [
            *existing_values,
            *(f"[[{rendered}]]" for rendered in additions),
        ]
        # `fm_text` conventionally ends on its last content byte while the
        # splice contract takes a terminated block; normalize in both directions.
        fm_text = record_formats.splice_frontmatter_fields(
            fm_text if fm_text.endswith("\n") else fm_text + "\n",
            {field: merged},
        ).removesuffix("\n")
    return fm_text, cited, warnings


def _lifted_relations_outcome(cited: list[str], *, preview: bool) -> str:
    named = ", ".join(cited)
    if preview:
        return f"Committing this will cite {named} in the page's frontmatter."
    return f"Cited {named} in the page's frontmatter."


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


def _resolve_verdict(
    supplied: str | None,
    current: Any | None,
    language: semantic_language_registry.SemanticLanguageRegistry,
) -> str | None:
    """Resolve `verdict` preserve-by-default; an explicit empty string clears it."""
    return _resolve_governed_value(
        supplied,
        current,
        key="verdict",
        normalize=lambda value: _canonical_verdict(value, language),
    )


def _resolve_check_by(supplied: str | None, current: Any | None) -> str | None:
    """Resolve `check_by` preserve-by-default; an explicit empty string clears it."""
    return _resolve_governed_value(
        supplied,
        current,
        key="check_by",
        normalize=_canonical_check_by,
    )


def _resolve_governed_value(
    supplied: str | None,
    current: Any | None,
    *,
    key: str,
    normalize: Any,
) -> str | None:
    """Preserve, clear, or replace one governed metadata value.

    Omission preserves, because the common edit is "fix the wording" and that
    must never cost the author their judgment. An explicit empty string is the
    one way to clear, so removing a verdict stays a deliberate act rather than
    something an incomplete argument list does by accident.

    An authored row the parser rejected has no normalized value to carry
    forward, and dropping it would be exactly the silent deletion this design
    exists to prevent. So an omitted argument over an invalid existing row is
    refused rather than resolved either way.
    """
    if supplied is None:
        if current is None:
            return None
        projected = getattr(current, key, None)
        if projected is None and key in dict(getattr(current, "metadata", {})):
            raise ObserveMemoryError(
                "INVALID_EXISTING_UNIT_METADATA",
                f"the current unit's authored {key} row is not valid, so an "
                "update can neither carry it forward nor drop it silently",
                f"Supply a valid {key} to replace the row, or an empty string "
                "to clear it deliberately.",
            )
        return projected
    if not str(supplied).strip():
        return None
    return normalize(str(supplied))


def _canonical_verdict(
    value: str, language: semantic_language_registry.SemanticLanguageRegistry
) -> str:
    verdict = language.resolve_label(semantic_labels.VERDICTS, value)
    if verdict is None:
        keys = language.label_keys(semantic_labels.VERDICTS)
        raise ObserveMemoryError(
            "INVALID_SEMANTIC_VERDICT",
            f"verdict must be exactly one of {', '.join(keys)}, as the key or its label",
            semantic_units.verdict_remediation(keys),
        )
    return verdict


def _canonical_check_by(value: str) -> str:
    return _canonical_date(value, "check_by")


def _canonical_date(value: str, key: str) -> str:
    candidate = semantic_units.calendar_date(value)
    if candidate is None:
        raise ObserveMemoryError(
            f"INVALID_SEMANTIC_{key.upper()}",
            f"{key} must be one exact ISO calendar date spelled YYYY-MM-DD",
        )
    return candidate


def _preserved_metadata(current: Any | None) -> tuple[tuple[str, str], ...]:
    """Authored rows this command does not own, in their authored order."""
    if current is None or getattr(current, "form", None) != "rich":
        return ()
    return tuple(
        (key, value)
        for key, value in dict(current.metadata).items()
        if key not in _OWNED_METADATA_KEYS
    )


def _requested_anchor(value: str | None, existing: set[str | None]) -> str | None:
    if value is None:
        return None
    anchor = str(value).strip()
    if not anchor or _ANCHOR_RE.fullmatch(anchor) is None:
        raise ObserveMemoryError(
            "INVALID_SEMANTIC_ANCHOR",
            f"invalid semantic-unit anchor: {value!r}",
            "Use 1-64 ASCII letters, digits, or hyphens, beginning and ending "
            "alphanumeric.",
        )
    if anchor in existing:
        raise ObserveMemoryError(
            "DUPLICATE_SEMANTIC_ANCHOR",
            f"another semantic unit on this parent already uses anchor {anchor!r}",
            "Choose an anchor that is unique within the parent page.",
        )
    return anchor


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
    verdict: str | None = None,
    check_by: str | None = None,
    as_of: str | None = None,
    until: str | None = None,
    preserved: tuple[tuple[str, str], ...] = (),
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
    if verdict is not None:
        metadata.append(f"- verdict: {verdict}")
    if check_by is not None:
        metadata.append(f"- check_by: {check_by}")
    if as_of is not None:
        metadata.append(f"- as_of: {as_of}")
    if until is not None:
        metadata.append(f"- until: {until}")
    # Rows this command does not own, re-emitted in their authored order.
    metadata.extend(_metadata_row(key, value) for key, value in preserved)
    return f"## {heading}\n" + "\n".join(metadata) + f"\n\n{clean_content}"


def _metadata_row(key: str, value: str) -> str:
    return f"- {key}: {value}" if value else f"- {key}:"


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
    newline = vault.document_newline(original)
    blank_line = bool(
        re.match(r"^---\r?\n.*?\r?\n---\r?\n\r?\n", original, re.DOTALL)
    )
    final_body = body if body.endswith(newline) else body + newline
    return vault.render_frontmatter_document(
        updated, final_body, newline=newline, blank_line=blank_line
    )


def _unit_by_anchor(document: Any, anchor: str | None) -> Any | None:
    if anchor is None:
        return None
    matches = [unit for unit in document.units if unit.anchor == anchor]
    return matches[0] if len(matches) == 1 else None


def _round_tripped_unit(document: Any, expected: Mapping[str, Any]) -> Any:
    """The rendered unit as the governed parser reads it back, refused unless it matches."""
    unit = _unit_by_anchor(document, expected["anchor"])
    if unit is None:
        diagnostics = [item.code for item in document.errors]
        raise ObserveMemoryError(
            "INVALID_SEMANTIC_UNIT",
            "rendered unit did not round-trip through the governed parser"
            + (f" ({', '.join(diagnostics)})" if diagnostics else ""),
        )
    _assert_round_trip(unit, **expected)
    return unit


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
    verdict: str | None = None,
    check_by: str | None = None,
    as_of: str | None = None,
    until: str | None = None,
    preserved: tuple[tuple[str, str], ...] = (),
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
        # Preserved rows are part of the expectation, so a reconstruction that
        # drops one fails the write instead of round-tripping its own deletion.
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
            **({"verdict": verdict} if verdict is not None else {}),
            **({"check_by": check_by} if check_by is not None else {}),
            **({"as_of": as_of} if as_of is not None else {}),
            **({"until": until} if until is not None else {}),
            **dict(preserved),
        }
        matches = (
            common_matches
            and dict(unit.metadata) == expected_metadata
            and unit.verdict == verdict
            and unit.check_by == check_by
            and unit.as_of == as_of
            and unit.until == until
        )
    if not matches:
        raise ObserveMemoryError(
            "AMBIGUOUS_SEMANTIC_UNIT_CONTENT",
            "unit fields collide with Markdown suffix, metadata, or heading syntax",
            "Move reserved trailing syntax into explicit tags/context/relations fields "
            "or select content that round-trips without reinterpretation.",
        )


#: How many dropped relations the outcome sentence names before it counts the rest.
_DROPPED_RELATIONS_NAMED = 5


def _dropped_relations_outcome(dropped: tuple[tuple[str, str], ...], *, compact: bool) -> str:
    """The sentence for an update that omits `relations` while its unit had some.

    An accepted update names the optional relations it dropped. Validation refuses an update
    that drops a required assessed-claim relation. A rich unit keeps relations when the call
    sends them again; a compact observation carries none.
    """
    named = "; ".join(f"{kind}: {target}" for kind, target in dropped[:_DROPPED_RELATIONS_NAMED])
    more = len(dropped) - _DROPPED_RELATIONS_NAMED
    if more > 0:
        named += f"; and {more} more"
    count = f"{len(dropped)} relation{'s' if len(dropped) > 1 else ''}"
    if compact:
        return (
            f"This update makes the unit a compact observation, which carries no relations, so "
            f"it loses {count} ({named})."
        )
    # `edit_note` carries `links` but no `kind`, and an update that sends relations must name its
    # rich kind, so on the listed tools the only way back is the unit's `relations:` line in the
    # page body.
    if capabilities.current_tool_shape() == "grouped":
        restore = "to keep them, send them again in `relations`, with the unit's `kind`"
    else:
        restore = (
            "to restore them, write them back on the unit's `relations:` line in the page body "
            "with "
            f"{capabilities.active_tools_text('edit_memory')}"
        )
    return (
        f"This update drops {count} the unit had ({named}). An update replaces a unit's "
        f"relations: {restore}."
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
    dropped_relations: tuple[tuple[str, str], ...] = (),
    dropped_outcome: str | None = None,
    lifted_cited: list[str] | None = None,
    lifted_outcome: str | None = None,
    timings: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operation": operation,
        "path": path,
        "mutated": mutated,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "semantic": semantic,
    }
    if dropped_relations:
        value["relations_dropped"] = [
            {"kind": kind, "target": target} for kind, target in dropped_relations
        ]
        value["outcome"] = dropped_outcome
    elif lifted_cited and lifted_outcome is not None:
        value["relations_cited"] = lifted_cited
        value["outcome"] = lifted_outcome
    if timings is not None:
        value["timings"] = timings
    if warnings:
        value["warnings"] = warnings
    if unit is not None:
        value["unit_ref"] = unit.unit_ref
        value["unit_id"] = unit_id(unit)
        value["unit"] = unit.to_dict()
    if removed_unit is not None:
        value["removed_unit_ref"] = removed_unit.unit_ref
        value["removed_unit_id"] = unit_id(removed_unit)
        value["removed_unit"] = removed_unit.to_dict()
    return value
