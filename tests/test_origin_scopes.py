from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from exomem import relation_registry, semantic_contract, semantic_language_registry
from exomem.provenance import OriginError, match_origin_scope
from exomem.semantic_units import parse_semantic_units

_REF = "exomem://memory/00000000-0000-0000-0000-000000000001"
_PEER = "exomem://memory/00000000-0000-0000-0000-000000000002"
_OTHER = "exomem://memory/00000000-0000-0000-0000-000000000003"
_COLLECTION = "00000000-0000-0000-0000-000000000004"


def _document(first: str = "First", second: str = "Second"):
    return parse_semantic_units(
        f"- [finding] {first} ^first\n- [finding] {second} ^second\n", parent_ref=_REF
    )


def _relations(root: Path, body: str = "## Relations\n- supports [[Beta]]\n"):
    registry = relation_registry.core_registry()
    states = []
    for reference, title, text in ((_REF, "Alpha", body), (_PEER, "Beta", "Original.\n")):
        source = (
            f"---\ntitle: {title}\ntype: insight\nstatus: active\n"
            f"exomem_id: {reference.rsplit('/', 1)[1]}\n---\n\n{text}"
        )
        states.append(
            semantic_contract.build_page_state(
                root,
                f"Knowledge Base/Notes/{title}.md",
                source,
                relation_registry=registry,
                language_registry=semantic_language_registry.core_registry(),
                review_fingerprint="current",
            )
        )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        root,
        states,
        registry=registry,
        identity_census=semantic_contract.StableIdentityCensus(
            tuple(
                semantic_contract.StableIdentityEntry(state.path, state.identity)
                for state in states
            )
        ),
    )
    return tuple((fact, _REF, _PEER) for fact in corpus.relation_facts)


def test_unit_binding_cannot_borrow_a_neighbor_and_survives_only_unrelated_edits() -> None:
    """A retained claim keeps its support after a sibling edit, not after its own revision."""
    document = _document()
    scope = {"kind": "unit", "unit_ref": "#first", "fingerprint": document.units[0].fingerprint}
    matched = match_origin_scope(scope, document=document)
    assert matched.status == "found" and matched.scope == scope
    assert matched.unit == document.units[0]
    assert match_origin_scope(scope, document=_document(second="Changed")).scope == scope
    assert match_origin_scope(scope, document=_document(first="Changed")).status == "stale"
    stolen = {**scope, "unit_ref": "#second"}
    result = match_origin_scope(stolen, document=document)
    assert result.status == "stale" and result.scope is None


def test_unit_subspans_keep_exact_original_text_and_refuse_overrun() -> None:
    """Relative unit offsets must select original Unicode text, never page or neighbor offsets."""
    text = "Context.\r\n\r\n- [finding] α β ^first\r\n"
    document = parse_semantic_units(text, parent_ref=_REF)
    unit = document.units[0]
    start = unit.span.text.index("α")
    scope = {
        "kind": "unit",
        "unit_ref": "#first",
        "fingerprint": unit.fingerprint,
        "span": {"start_offset": start, "end_offset": start + 3},
    }
    matched = match_origin_scope(scope, document=document)
    assert matched.status == "found" and matched.text == "α β"
    assert matched.span == (unit.span.start_offset + start, unit.span.start_offset + start + 3)
    assert text[slice(*matched.span)] == matched.text
    overrun = {**scope, "span": {"start_offset": start, "end_offset": len(unit.span.text) + 1}}
    assert match_origin_scope(overrun, document=document).status == "unavailable"
    with pytest.raises(OriginError):
        match_origin_scope(overrun, document=document, authoring=True)


def test_unit_resolution_abstains_without_a_current_unique_parent_bound_unit() -> None:
    """Missing or duplicate anchors and an unavailable parent cannot produce persisted scopes."""
    scope = {"kind": "unit", "unit_ref": "#first"}
    missing = match_origin_scope(
        {**scope, "unit_ref": "#missing"}, document=_document(), authoring=True
    )
    assert missing.status == "missing" and missing.scope is None
    duplicate = parse_semantic_units(
        "- [finding] One ^first\n- [finding] Two ^first\n", parent_ref=_REF
    )
    assert match_origin_scope(scope, document=duplicate, authoring=True).status == "ambiguous"
    unbound = parse_semantic_units("- [finding] One ^first\n")
    assert match_origin_scope(scope, document=unbound, authoring=True).status == "unavailable"


def test_record_unit_uses_the_existing_item_path_without_inventing_a_memory_identity() -> None:
    """A file Record can bind its own unit; a different owning item cannot lend that unit."""
    document = parse_semantic_units(
        "- [finding] The retained reading. ^reading\n",
        path="Knowledge Base/Records/Readings/Entries/one.md",
    )
    scope = {"kind": "unit", "unit_ref": "#reading"}
    owner = document.parent_ref
    matched = match_origin_scope(
        scope, document=document, owner_ref=owner,
        record_identity=(_COLLECTION, "one"), authoring=True,
    )
    assert matched.status == "found"
    assert matched.scope["fingerprint"] == document.units[0].fingerprint
    assert match_origin_scope(
        matched.scope, document=document, owner_ref=owner,
        record_identity=(_COLLECTION, "one"),
    ).status == "found"
    assert match_origin_scope(scope, document=document, authoring=True).status == "unavailable"
    assert match_origin_scope(
        scope, document=document, owner_ref=owner.replace("one.md", "two.md"),
        record_identity=(_COLLECTION, "two"), authoring=True,
    ).status == "unavailable"


def test_authoring_fills_only_an_omitted_fingerprint_without_mutating_the_request() -> None:
    """Preparation cannot silently refresh a supplied old fingerprint or a retained omission."""
    request = {"kind": "field", "field": "summary"}
    original = match_origin_scope(request, fields={"summary": "Before"}, authoring=True)
    assert original.status == "found" and original.scope["fingerprint"]
    assert "fingerprint" not in request
    assert match_origin_scope(request, fields={"summary": "Before"}).status == "unavailable"
    assert (
        match_origin_scope(original.scope, fields={"summary": "After"}, authoring=True).status
        == "stale"
    )
    assert (
        match_origin_scope(original.scope, fields={"summary": "Before", "other": "Changed"}).scope
        == original.scope
    )


def test_field_fingerprints_distinguish_presence_names_and_typed_yaml_values() -> None:
    """Null is present; scalar coercion, field renaming and YAML type flattening are unsafe."""
    request = {"kind": "field", "field": "summary"}
    values = (None, False, 0, 0.0, "0", date(2026, 10, 3), "2026-10-03")
    scopes = [
        match_origin_scope(request, fields={"summary": value}, authoring=True).scope
        for value in values
    ]
    assert len({scope["fingerprint"] for scope in scopes}) == len(values)
    assert match_origin_scope(scopes[0], fields={}).status == "missing"
    assert match_origin_scope(scopes[0], fields=None).status == "unavailable"
    renamed = match_origin_scope(
        {"kind": "field", "field": "title"}, fields={"title": None}, authoring=True
    )
    assert renamed.scope["fingerprint"] != scopes[0]["fingerprint"]
    left = {"date": datetime(2026, 10, 3, tzinfo=UTC), "items": {"b", "a"}, "raw": b"x"}
    right = {
        "raw": b"x",
        "items": {"a", "b"},
        "date": datetime(2026, 10, 3, 2, tzinfo=timezone(timedelta(hours=2))),
    }
    bound = match_origin_scope(request, fields={"summary": left}, authoring=True)
    assert match_origin_scope(bound.scope, fields={"summary": right}).status == "found"
    assert match_origin_scope(scopes[0], fields={"summary": float("nan")}).status == "unavailable"
    with pytest.raises(OriginError):
        match_origin_scope(request, fields={"summary": object()}, authoring=True)


def test_record_fields_bind_actual_collection_and_item_identity() -> None:
    """A sibling item with equal values cannot reuse another item's field fingerprint."""
    request = {
        "kind": "record_field",
        "collection_id": _COLLECTION,
        "item_key": "one",
        "field": "body",
    }
    fields = {"body": "Same retained value"}
    bound = match_origin_scope(
        request, fields=fields, record_identity=(_COLLECTION, "one"), authoring=True
    )
    assert bound.status == "found"
    assert (
        match_origin_scope(bound.scope, fields=fields, record_identity=(_COLLECTION, "two")).status
        == "missing"
    )
    assert (
        match_origin_scope(
            bound.scope, fields=fields, record_identity=(_OTHER.rsplit("/", 1)[1], "one")
        ).status
        == "missing"
    )
    copied = {**bound.scope, "item_key": "two"}
    assert (
        match_origin_scope(
            copied, fields=fields, record_identity=(_COLLECTION, "two"), authoring=True
        ).status
        == "stale"
    )
    plain = match_origin_scope({"kind": "field", "field": "body"}, fields=fields, authoring=True)
    assert plain.scope["fingerprint"] != bound.scope["fingerprint"]


def test_relation_binding_uses_the_exact_directed_tuple_not_line_numbers(tmp_path: Path) -> None:
    """Line movement preserves an authored occurrence; direction or endpoint edits do not."""
    request = {"kind": "relation", "relation": "supports", "direction": "outbound", "peer": _PEER}
    relations = _relations(tmp_path)
    assert len(relations) == 1
    bound = match_origin_scope(request, owner_ref=_REF, relations=relations, authoring=True)
    assert bound.status == "found"
    moved = _relations(tmp_path, "Prose.\n\n## Relations\n- supports [[Beta]]\n")
    assert moved[0][0].authored_line != relations[0][0].authored_line
    assert match_origin_scope(bound.scope, owner_ref=_REF, relations=moved).status == "found"
    wrong_direction = {**bound.scope, "direction": "inbound"}
    assert (
        match_origin_scope(wrong_direction, owner_ref=_REF, relations=relations).status == "missing"
    )
    changed_peer = {**bound.scope, "peer": _OTHER}
    assert match_origin_scope(changed_peer, owner_ref=_REF, relations=relations).status == "missing"
    retargeted = ((relations[0][0], _REF, _OTHER),)
    assert match_origin_scope(changed_peer, owner_ref=_REF, relations=retargeted).status == "stale"
    inbound = {**bound.scope, "direction": "inbound", "peer": _REF}
    assert match_origin_scope(inbound, owner_ref=_PEER, relations=relations).status == "found"


def test_relation_occurrences_are_selected_exactly_and_invalid_endpoints_abstain(
    tmp_path: Path,
) -> None:
    """A duplicate occurrence is not interchangeable; unavailable or unregistered facts give no scope."""
    request = {"kind": "relation", "relation": "supports", "direction": "outbound", "peer": _PEER}
    relations = _relations(tmp_path, "## Relations\n- supports [[Beta]]\n- supports [[Beta]]\n")
    assert len(relations) == 2
    assert (
        match_origin_scope(request, owner_ref=_REF, relations=relations, authoring=True).status
        == "ambiguous"
    )
    bound = match_origin_scope(request, owner_ref=_REF, relations=relations[:1], authoring=True)
    assert (
        match_origin_scope(bound.scope, owner_ref=_REF, relations=relations[1:]).status == "stale"
    )
    fact, source, target = relations[0]
    assert (
        match_origin_scope(bound.scope, owner_ref=_REF, relations=((fact, source, None),)).status
        == "unavailable"
    )
    ambiguous = replace(fact, target_status="ambiguous")
    assert (
        match_origin_scope(
            bound.scope, owner_ref=_REF, relations=((ambiguous, source, target),)
        ).status
        == "ambiguous"
    )
    unregistered = replace(fact, registry_status="unregistered")
    assert (
        match_origin_scope(
            bound.scope, owner_ref=_REF, relations=((unregistered, source, target),)
        ).status
        == "unavailable"
    )
