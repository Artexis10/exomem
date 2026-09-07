"""Governed point-read review of observed vocabulary work, never a corpus scan."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import entity_types, get_page, memory_refs, relation_registry, vocabulary_evidence
from .governance import egress
from .governance.policy import DISCLOSURE_MAX
from .governance.principal import effective_principal
from .vocabulary_state import VocabularyState
from .vocabulary_workflow import (
    Evidence,
    WorkItem,
    choice_contract,
    make_item,
    validate_decision,
    validation_feedback,
)


def registry_hashes(vault_root: Path) -> dict[str, str]:
    relations = relation_registry.load_registry(vault_root)
    entities = entity_types.load_entity_types(vault_root)
    return {
        "relations": f"{relations.core_version}:{relations.extension_hash}",
        "entity_types": f"{entities.core_version}:{entities.extension_hash}",
    }


def _validate_live_choice(vault_root: Path, item: WorkItem, decision: Mapping[str, Any]) -> None:
    choice = decision["choice"]
    if choice is None:
        return
    canonical = choice["canonical"]
    proposing = decision["outcome"] == "propose-new"
    findings = []
    if item.family == "entity-instance/v1":
        state = dict(item.projection_currency).get("candidate_state")
        if state == "ambiguous" or (state == "hydration" and proposing):
            raise ValueError(
                "VOCABULARY_DECISION_INVALID: resolve identity ambiguity or enrich "
                "the existing entity"
            )
    if item.family == "relation-type/v1":
        from . import memory_schema

        registry = relation_registry.load_registry(vault_root)
        if proposing:
            if registry.resolve(canonical).canonical is not None:
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: this canonical meaning already exists"
                )
            try:
                proposal = relation_registry.merge_extension_delta(
                    memory_schema.relation_registry_proposal(registry),
                    {"upsert": {canonical: choice["definition"]}},
                )
            except ValueError as exc:
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: proposal conflicts with the current registry; "
                    + str(exc)[:1024]
                ) from None
            findings = relation_registry.validate_proposal(proposal)
        else:
            definition = registry.definition(canonical)
            if definition is None or definition.status != "active":
                raise ValueError("VOCABULARY_DECISION_INVALID: reuse a current canonical relation")
    elif item.family == "entity-type/v1":
        registry = entity_types.load_entity_types(vault_root)
        if proposing:
            if registry.resolve(canonical) is not None:
                raise ValueError("VOCABULARY_DECISION_INVALID: this canonical type already exists")
            definitions = {}
            for key, definition in registry.extensions.items():
                value = asdict(definition)
                value.pop("id")
                value.pop("core")
                definitions[key] = {
                    field: list(entry) if isinstance(entry, tuple) else entry
                    for field, entry in value.items()
                    if entry is not None
                }
            definitions[canonical] = choice["definition"]
            findings = entity_types.validate_proposal(
                {"schema_version": 1, "entity_types": definitions}
            )
        else:
            definition = registry.resolve(canonical)
            if definition is None or definition.id != canonical or definition.status != "active":
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: reuse a current canonical entity type"
                )
    elif proposing:
        definition = choice["definition"]
        registry = entity_types.load_entity_types(vault_root)
        if registry.resolve(definition["entity_type"]) is None:
            raise ValueError(
                "VOCABULARY_DECISION_INVALID: register the entity type before proposing an instance"
            )
    else:
        if canonical not in dict(item.target_versions):
            raise ValueError(
                "VOCABULARY_DECISION_INVALID: choose a reviewed canonical entity identity"
            )
        page = _read(vault_root, item.to_dict(), canonical)
        frontmatter = page.get("frontmatter", {})
        if (
            frontmatter.get("type") != "entity"
            or frontmatter.get("status") in {"deprecated", "archived", "deleted"}
            or entity_types.load_entity_types(vault_root).resolve(
                frontmatter.get("entity_type", "")
            )
            is None
        ):
            raise ValueError("VOCABULARY_DECISION_INVALID: choose a current canonical entity page")
    if findings:
        raise ValueError(
            "VOCABULARY_DECISION_INVALID: proposal conflicts with the current registry; "
            + validation_feedback(findings)
        )


def _path(item: Mapping[str, Any], ref: str) -> str:
    # Hints locate point reads; they never authorize them or establish identity.
    path = item.get("paths", {}).get(ref, ref)
    if not isinstance(path, str) or "://" in path:
        raise ValueError("VOCABULARY_ITEM_NOT_FOUND: refresh observed work")
    return path


def _visible(vault_root: Path, item: Mapping[str, Any]) -> bool:
    refs = list(
        dict.fromkeys([*item["target_versions"], *(entry["ref"] for entry in item["evidence"])])
    )
    # Provider rows have bounded targets and evidence.  Treat a malformed
    # sidecar row as unavailable before it can turn one review page into an
    # unbounded egress fan-out.
    if len(refs) > 16:
        return False
    try:
        return all(
            egress.release_level_for(vault_root, _path(item, ref)) == DISCLOSURE_MAX for ref in refs
        )
    except (ValueError, OSError):
        return False


def _read(vault_root: Path, item: Mapping[str, Any], ref: str) -> dict[str, Any]:
    path = _path(item, ref)
    if egress.release_level_for(vault_root, path) != DISCLOSURE_MAX:
        raise ValueError("VOCABULARY_ITEM_NOT_FOUND: refresh observed work")
    try:
        raw = get_page.get_page(vault_root, path=path)
    except (get_page.GetError, OSError) as exc:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: refresh the canonical evidence") from exc
    if ref.startswith(memory_refs.REF_PREFIX) and memory_refs.ref_from_markdown(raw.content) != ref:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: canonical identity has changed")
    released = egress.annotate_page(vault_root, raw.as_dict(), snapshot_content=raw.content)
    if not released or "body" not in released or released.get("content_hash") != raw.content_hash:
        raise ValueError("VOCABULARY_ITEM_NOT_FOUND: refresh observed work")
    return released


def _fresh(
    vault_root: Path, stored: Mapping[str, Any]
) -> tuple[WorkItem, dict[str, dict[str, Any]]]:
    if stored["signal"] == "entity-lifecycle":
        from . import vocabulary_entities

        # New contexts or a newly created identity can change the provider's
        # signal even while every formerly reviewed page has unchanged bytes.
        current = vocabulary_entities.refresh(vault_root, stored["logical_identity"])
        current_data = current.to_dict()
        refs = [*current_data["target_versions"], *(entry.ref for entry in current.evidence)]
        return current, {ref: _read(vault_root, current_data, ref) for ref in dict.fromkeys(refs)}
    pages = {}
    refs = [*stored["target_versions"], *(entry["ref"] for entry in stored["evidence"])]
    for ref in dict.fromkeys(refs):
        pages[ref] = _read(vault_root, stored, ref)
    current_hashes = registry_hashes(vault_root)
    current = make_item(
        family=stored["family"],
        signal=stored["signal"],
        targets={ref: pages[ref]["content_hash"] for ref in stored["target_versions"]},
        evidence=[
            Evidence(entry["ref"], pages[entry["ref"]]["content_hash"], entry.get("origin"))
            for entry in stored["evidence"]
        ],
        registry_hashes={key: current_hashes[key] for key in stored["registry_hashes"]},
        # Point reads refresh bytes, not the provenance projection. Only the
        # owning evidence provider may promote unavailable/warming to current.
        projection_status=stored["projection"]["status"],
        continuation=stored.get("continuation"),
        paths=stored.get("paths"),
        question=stored.get("question"),
        logical_identity=stored.get("logical_identity"),
        projection_currency=stored["projection"].get("currency", {}),
    )
    return current, pages


def review(
    vault_root: Path,
    *,
    limit: int = 4,
    continuation: str | None = None,
    state: str = "open",
) -> dict[str, Any]:
    from . import vocabulary_delivery

    recovery = vocabulary_delivery.recover(vault_root) if continuation is None else None
    owner = VocabularyState(vault_root)
    result = owner.page(
        limit=limit,
        continuation=continuation,
        state=state,
        # The index carries the three vocabulary dispositions.  Egress still
        # runs for this fixed candidate window immediately before disclosure.
        visible=lambda item: _visible(vault_root, item),
    )
    if recovery and (recovery["processed"] or recovery["state"] != "current"):
        result["recovery"] = recovery
    return result


def context(
    vault_root: Path,
    *,
    ref: str,
    expected_fingerprint: str | None = None,
    max_body_chars: int = 4000,
    max_related_pages: int = 8,
    continuation: str | None = None,
) -> dict[str, Any]:
    if type(max_body_chars) is not int or not 0 <= max_body_chars <= 12000:
        raise ValueError("VOCABULARY_LIMIT_INVALID: body budget must be 0 to 12000")
    if type(max_related_pages) is not int or not 1 <= max_related_pages <= 64:
        raise ValueError("VOCABULARY_LIMIT_INVALID: evidence page budget must be 1 to 64")
    store = VocabularyState(vault_root)
    current, pages = _fresh(vault_root, store.get(ref))
    if expected_fingerprint is not None and current.fingerprint != expected_fingerprint:
        raise ValueError("VOCABULARY_DECISION_STALE: refresh the changed consideration")
    offset = 0
    page_evidence = list(current.evidence)
    source_continuation = current.continuation
    checkpoint_continuation = None
    if continuation is not None:
        try:
            checkpoint_page = (
                json.loads(continuation).get("kind") == "vocabulary-provenance-context/v1"
            )
        except (TypeError, ValueError, AttributeError):
            checkpoint_page = False
        if continuation == current.continuation or checkpoint_page:
            from . import vocabulary_projection

            more = vocabulary_projection.more_evidence(vault_root, continuation=continuation)
            page_evidence = more["evidence"]
            source_continuation = more["continuation"]
            checkpoint_continuation = continuation
            for entry in page_evidence:
                pages[entry.ref] = _read(
                    vault_root,
                    {**current.to_dict(), "paths": dict(current.paths) | more["paths"]},
                    entry.ref,
                )
        else:
            try:
                cursor = json.loads(continuation)
                if not isinstance(cursor, dict) or set(cursor) not in {
                    frozenset({"ref", "fingerprint", "offset"}),
                    frozenset({"ref", "fingerprint", "offset", "checkpoint"}),
                }:
                    raise ValueError
                offset = cursor["offset"]
                checkpoint_continuation = cursor.get("checkpoint")
                if type(offset) is not int or (
                    checkpoint_continuation is not None
                    and not isinstance(checkpoint_continuation, str)
                ):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh item context") from exc
            if cursor["ref"] != current.ref or cursor["fingerprint"] != current.fingerprint:
                raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh item context")
            if checkpoint_continuation is not None:
                from . import vocabulary_projection

                more = vocabulary_projection.more_evidence(
                    vault_root, continuation=checkpoint_continuation
                )
                page_evidence = more["evidence"]
                source_continuation = more["continuation"]
                for entry in page_evidence:
                    pages[entry.ref] = _read(
                        vault_root,
                        {**current.to_dict(), "paths": dict(current.paths) | more["paths"]},
                        entry.ref,
                    )
            if not 0 <= offset <= len(page_evidence):
                raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh item context")
    observed = store.observe(current)
    if current.family == "relation-type/v1":
        definitions = vocabulary_evidence.relation_evidence(
            relation_registry.load_registry(vault_root),
            query="review relationship meaning",
            limit=max_related_pages,
        )
    else:
        definitions = vocabulary_evidence.resolve_entity_type(
            entity_types.load_entity_types(vault_root),
            query="review entity type fit",
            limit=max_related_pages,
        )

    def bounded(page_ref):
        page = pages[page_ref]
        return {
            "ref": page_ref,
            "path": page["path"],
            "content_hash": page["content_hash"],
            "frontmatter": {
                key: str(page["frontmatter"][key])[:256]
                for key in ("type", "title", "exomem_id")
                if key in page["frontmatter"]
            },
            "body": page["body"][:max_body_chars],
            "truncated": len(page["body"]) > max_body_chars,
        }

    evidence_refs = [entry.ref for entry in page_evidence]
    next_offset = offset + max_related_pages
    return {
        "item": observed,
        "choice_contract": choice_contract(current.family),
        "targets": [bounded(target) for target, _ in current.target_versions],
        "evidence": [bounded(evidence_ref) for evidence_ref in evidence_refs[offset:next_offset]],
        "evidence_continuation": json.dumps(
            {
                "ref": current.ref,
                "fingerprint": current.fingerprint,
                "offset": next_offset,
                **(
                    {"checkpoint": checkpoint_continuation}
                    if checkpoint_continuation is not None
                    else {}
                ),
            },
            sort_keys=True,
        )
        if next_offset < len(evidence_refs)
        else source_continuation,
        "definitions": definitions,
        "authority": {
            "mutation_granted": False,
            "application": "use existing canonical writers and permissions",
        },
    }


def decide(vault_root: Path, *, ref: str, decision: dict[str, Any]) -> dict[str, Any]:
    store = VocabularyState(vault_root)
    current, _ = _fresh(vault_root, store.get(ref))
    validate_decision(current, decision)  # Stale requests must not even update currency.
    _validate_live_choice(vault_root, current, decision)
    actor = effective_principal()
    if not actor.resolved:
        raise ValueError("VOCABULARY_DECISION_INVALID: a resolved principal is required")
    store.observe(current)
    return store.decide(current, decision, actor=actor.audience_id)
