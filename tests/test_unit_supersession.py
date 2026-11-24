"""Unit-level supersession inside a note (OpenSpec `add-unit-level-supersession`).

One governed write supersedes, assesses or links a single claim in place: the
successor's `supersedes` relation is the only record, `superseded_by` is derived
at read, and an entity page's facet prose converts to addressable units in place.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import (
    audit_fix,
    commands,
    maintenance_jobs,
    public_surface,
    public_tools,
)
from exomem.governance.principal import owner_principal, request_scope

PAGE_ID = "00000000-0000-4000-8000-000000000091"
PAGE = "Knowledge Base/Notes/Insights/supersede.md"
ENTITY_PAGE = "Knowledge Base/Entities/Harbour Co-op.md"
ENTITY_ID = "00000000-0000-4000-8000-000000000092"


def _page_source(body: str, *, type: str = "insight", extra: str = "") -> str:
    return (
        "---\n"
        "title: Supersede\n"
        f"type: {type}\n"
        "status: active\n"
        f"exomem_id: {PAGE_ID}\n"
        "updated: 2026-07-15\n"
        f"{extra}---\n\n"
        f"{body.rstrip()}\n"
    )


def _entity_source(body: str) -> str:
    return (
        "---\n"
        "title: Harbour Co-op\n"
        "type: entity\n"
        "entity_type: organisation\n"
        "status: active\n"
        f"exomem_id: {ENTITY_ID}\n"
        "updated: 2026-07-15\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def _write_page(root: Path, *, rel: str = PAGE, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source.encode("utf-8"))
    return path


def _observations_unit(body: str) -> str:
    return f"# Entity\n\n## Suppliers\n- Juniper takes preorders in November.\n\n## Observations\n{body}"


def _translate(tool: str, arguments: dict) -> tuple[str, dict]:
    """The grouped command and leaf call one public tool call stands for."""
    wire_tool = public_tools.TOOLS[tool]
    wire = wire_tool.model.model_validate(arguments).model_dump(exclude_none=True)
    return wire_tool.translate(wire, public_surface._CONTEXT)  # noqa: SLF001


def _call(tool: str, root: Path, arguments: dict):
    """Drive the public tool through its real translation into the leaf command."""
    command, leaf = _translate(tool, arguments)
    leaf_fn = {
        "observe_memory": commands.op_observe_memory,
        "edit_memory": commands.op_edit_memory,
        "replace_memory": commands.op_replace_memory,
    }[command]
    return leaf_fn(root, **leaf)


def _edit_note(root: Path, **arguments):
    arguments.setdefault("note", PAGE)
    arguments.setdefault("why", "correct the claim")
    return _call("edit_note", root, arguments)


def _units_of(root: Path, rel: str) -> list[dict]:
    return commands.op_read_memory(root, path=rel, units=True)["units"]


def _body(page: Path) -> str:
    return page.read_bytes().decode("utf-8").split("---\n", 2)[-1]


# --------------------------------------------------------------------------- #
# 1.1  Entity pages accept unit writes
# --------------------------------------------------------------------------- #


def test_add_observation_appends_a_compact_unit_to_an_entity_page(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        rel=ENTITY_PAGE,
        source=_entity_source("# Harbour Co-op\n\n## Suppliers\n- Sells seeds.\n\n## Observations\n\n- [vendors] Opens at nine. ^seed-1\n"),
    )
    before = page.read_bytes()

    with request_scope(owner_principal(surface="cli")):
        result = commands.op_observe_memory(
            tmp_path, path=ENTITY_PAGE, operation="add",
            category="vendors", content="Opens at ten on Sundays.",
        )

    assert result["mutated"] is True
    after = before.decode("utf-8").splitlines(keepends=True)
    added = [
        line for line in page.read_bytes().decode("utf-8").splitlines(keepends=True)
        if line not in after
    ]
    # One new compact line under `## Observations`; the frontmatter `updated` row may move.
    compact = [line for line in added if line.startswith("- [vendors] Opens at ten on Sundays.")]
    assert len(compact) == 1 and " ^" in compact[0]
    text = page.read_bytes().decode("utf-8")
    assert "Opens at ten on Sundays." in text.split("## Observations", 1)[1]
    # No other body line changed.
    before_body = before.decode("utf-8").split("---\n", 2)[-1]
    after_body = text.split("---\n", 2)[-1]
    for line in before_body.splitlines():
        assert line in after_body.splitlines()


def test_add_observation_rich_with_links_and_as_of_on_an_entity_page(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        rel=ENTITY_PAGE,
        source=_entity_source("# Harbour Co-op\n\n## Observations\n"),
    )

    with request_scope(owner_principal(surface="cli")):
        result = commands.op_observe_memory(
            tmp_path, path=ENTITY_PAGE, operation="add",
            category="vendors", content="Winter hours start in December.",
            kind="correction", as_of="2026-12-01",
            relations=[{"kind": "supports", "target": "[[Harbour Co-op]]"}],
        )

    assert result["mutated"] is True
    unit = result["unit"]
    assert unit["kind"] == "correction"
    assert unit["as_of"] == "2026-12-01"
    assert any(relation["kind"] == "supports" for relation in unit["relations"])


@pytest.mark.parametrize("tier", ["readonly", "excluded"])
def test_a_policy_bound_entity_page_still_refuses(tmp_path: Path, tier: str) -> None:
    page = _write_page(
        tmp_path,
        rel=ENTITY_PAGE,
        source=_entity_source("# Harbour Co-op\n\n## Observations\n"),
    )
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        f"{tier}:\n  - Entities\n", encoding="utf-8"
    )
    before = page.read_bytes()

    with pytest.raises(ValueError, match="OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE") as raised:
        commands.op_observe_memory(
            tmp_path, path=ENTITY_PAGE, operation="add",
            category="vendors", content="Policy refusal.",
        )

    assert tier in str(raised.value)
    assert page.read_bytes() == before


# --------------------------------------------------------------------------- #
# 1.2/1.3  Rich-kind refusals name the governed kinds
# --------------------------------------------------------------------------- #


def test_links_on_a_compact_unit_list_the_admitted_kinds(tmp_path: Path) -> None:
    _write_page(tmp_path, source=_page_source("# Observe\n\nExisting prose.\n"))

    with pytest.raises(ValueError, match="COMPACT_RELATIONS_REQUIRE_RICH_KIND") as raised:
        commands.op_observe_memory(
            tmp_path, path=PAGE, operation="add",
            category="rule", content="A claim.",
            relations=[{"kind": "supports", "target": "[[Other]]"}],
        )

    message = str(raised.value)
    assert "correction" in message
    assert "claim" in message
    assert "kind" in message


def test_a_kind_passed_as_category_is_named(tmp_path: Path) -> None:
    _write_page(tmp_path, source=_page_source("# Observe\n\nExisting prose.\n"))

    with pytest.raises(ValueError, match="COMPACT_RELATIONS_REQUIRE_RICH_KIND") as raised:
        commands.op_observe_memory(
            tmp_path, path=PAGE, operation="add",
            category="correction", content="A claim.",
            relations=[{"kind": "supports", "target": "[[Other]]"}],
        )

    message = str(raised.value)
    assert 'kind="correction"' in message or "kind=`correction`" in message or "kind=correction" in message


def test_metadata_on_a_compact_unit_lists_the_admitted_kinds(tmp_path: Path) -> None:
    from exomem import semantic_language_registry

    _write_page(tmp_path, source=_page_source("# Observe\n\nExisting prose.\n"))
    overlay = semantic_language_registry.registry_path(tmp_path)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "schema_version: 1\nkinds:\n  vault_claim:\n"
        "    description: A vault claim.\n    labels: {en: Vault claim}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="COMPACT_METADATA_REQUIRES_RICH_KIND") as raised:
        commands.op_observe_memory(
            tmp_path, path=PAGE, operation="add",
            category="rule", content="A claim.", as_of="2026-07-01",
        )

    message = str(raised.value)
    assert "correction" in message
    assert "vault_claim" in message


# --------------------------------------------------------------------------- #
# 1.4  supersede_observation
# --------------------------------------------------------------------------- #


def _compact_entity_page(root: Path) -> Path:
    return _write_page(
        root,
        rel=ENTITY_PAGE,
        source=_entity_source(
            "# Harbour Co-op\n\n"
            "## Suppliers\n- Sells seeds.\n\n"
            "## Observations\n\n- [vendors] Juniper takes preorders in November. ^claim-1\n"
            "- [vendors] Opens at nine. ^claim-2\n"
        ),
    )


def test_supersede_note_cannot_target_one_unit() -> None:
    assert "unit" not in public_tools.TOOLS["supersede_note"].model.model_fields
    assert "span" not in public_tools.TOOLS["supersede_note"].model.model_fields


def test_supersede_observation_replaces_one_compact_claim_on_an_entity_page(
    tmp_path: Path,
) -> None:
    page = _compact_entity_page(tmp_path)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            note=ENTITY_PAGE,
            action="supersede_observation",
            unit="claim-1",
            text="Cedar Two replaces the Juniper preorder.",
            category="vendors",
            as_of="2026-11-10",
            links=[{"relation": "owns", "page": "[[Cedar Two]]"}],
            why="preorder corrected after the gift arrived",
        )

    assert result["mutated"] is True
    text = page.read_bytes().decode("utf-8")
    # The target's bytes are unchanged; the successor is one appended rich unit.
    assert "- [vendors] Juniper takes preorders in November. ^claim-1" in text
    successor = result["unit"]
    relations = {relation["kind"]: relation["target"] for relation in successor["relations"]}
    assert "supersedes" in relations and "#claim-1" in relations["supersedes"]
    assert relations.get("owns") == "[[Cedar Two]]"
    assert successor["as_of"] == "2026-11-10"
    # History records `why` and both units.
    with request_scope(owner_principal(surface="cli")):
        history = commands.op_read_memory(
            tmp_path, path=ENTITY_PAGE, include_history=True
        )["revision_history"]["revisions"]
    assert history[0]["reason"] == "preorder corrected after the gift arrived"
    affected = {unit["unit_ref"] for unit in history[0]["affected_units"]}
    assert successor["unit_ref"] in affected
    assert any(ref.rsplit("#", 1)[-1] == "claim-1" for ref in affected)


def test_supersede_observation_keeps_the_target_kind_for_a_rich_target(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        source=_page_source(
            "# Observe\n\n"
            "## Claim\n- category: rule\n- id: old-claim\n\nOpens at nine.\n"
        ),
    )
    units = _units_of(tmp_path, PAGE)
    (target,) = units

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            action="supersede_observation",
            unit=target["id"],
            text="Opens at ten.",
        )

    assert result["unit"]["kind"] == "claim"


def test_supersede_observation_uses_the_replacement_kind_for_a_compact_target(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        source=_page_source("# Observe\n\n## Observations\n\n- [rule] Opens at nine. ^old-1\n"),
    )
    units = _units_of(tmp_path, PAGE)
    (target,) = units

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            action="supersede_observation",
            unit=target["id"],
            text="Opens at ten.",
        )

    assert result["unit"]["kind"] == "correction"


def test_a_stale_unit_id_refuses_and_leaves_the_page_unchanged(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source("# Observe\n\n## Observations\n\n- [rule] Opens at nine. ^old-1\n"),
    )
    before = page.read_bytes()
    stale = _units_of(tmp_path, PAGE)[0]["id"] + "stale"

    with request_scope(owner_principal(surface="cli")), pytest.raises(
        ValueError, match="STALE_UNIT_REFERENCE|UNIT_NOT_FOUND"
    ):
        _edit_note(
            tmp_path,
            action="supersede_observation",
            unit=stale,
            text="Opens at ten.",
        )

    assert page.read_bytes() == before


# --------------------------------------------------------------------------- #
# 1.5  Span targets and their refusals
# --------------------------------------------------------------------------- #


def _facet_entity_page(root: Path) -> Path:
    return _write_page(
        root,
        rel=ENTITY_PAGE,
        source=_entity_source(
            "# Harbour Co-op\n\n"
            "## Suppliers\n"
            "- Juniper One takes preorders in November.\n"
            "- Sells seeds.\n\n"
            "## Observations\n\n- [vendors] Opens at nine. ^claim-2\n"
        ),
    )


def test_a_span_inside_a_facet_item_converts_only_that_item_and_supersedes_it(
    tmp_path: Path,
) -> None:
    page = _facet_entity_page(tmp_path)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            note=ENTITY_PAGE,
            action="supersede_observation",
            span="takes preorders in November",
            text="Cedar Two replaces the Juniper preorder.",
            category="vendors",
            why="preorder corrected",
        )

    text = page.read_bytes().decode("utf-8")
    # The prose item became a compact unit under its heading's category, text unchanged.
    assert "- [suppliers] Juniper One takes preorders in November. ^" in text
    assert "- Sells seeds.\n" in text
    relations = {relation["kind"]: relation["target"] for relation in result["unit"]["relations"]}
    supersedes = relations["supersedes"]
    assert supersedes.startswith("[[#")
    anchor = supersedes.removeprefix("[[#").removesuffix("]]")
    assert f"^{anchor}" in text
    # The converted item is the superseded one.
    units = _units_of(tmp_path, ENTITY_PAGE)
    converted = next(unit for unit in units if unit["category"] == "suppliers")
    assert converted["superseded_by"] == [result["unit_id"]]


def test_an_ambiguous_span_refuses_naming_the_match_count(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        rel=ENTITY_PAGE,
        source=_entity_source(
            "# Harbour Co-op\n\n"
            "## Suppliers\n- Juniper sells seeds.\n\n"
            "## Staff\n- Juniper runs the desk.\n"
        ),
    )
    before = page.read_bytes()

    with request_scope(owner_principal(surface="cli")), pytest.raises(
        ValueError, match="SUPERSEDE_TARGET_AMBIGUOUS"
    ) as raised:
        _edit_note(
            tmp_path, note=ENTITY_PAGE, action="supersede_observation",
            span="Juniper", text="Corrected.", category="vendors",
        )

    assert "2" in str(raised.value)
    assert page.read_bytes() == before


def test_a_missing_span_refuses_saying_how_to_read_unit_ids(tmp_path: Path) -> None:
    page = _facet_entity_page(tmp_path)
    before = page.read_bytes()

    with request_scope(owner_principal(surface="cli")), pytest.raises(
        ValueError, match="SUPERSEDE_TARGET_NOT_FOUND"
    ):
        _edit_note(
            tmp_path, note=ENTITY_PAGE, action="supersede_observation",
            span="no such claim anywhere", text="Corrected.", category="vendors",
        )

    assert page.read_bytes() == before


def test_an_already_superseded_target_names_its_successor(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source(
            "# Observe\n\n"
            "## Observations\n\n- [rule] Opens at nine. ^old-1\n\n"
            "## Correction\n- category: rule\n- id: newer\n"
            "- relations: supersedes: [[#old-1]]\n\nOpens at ten.\n"
        ),
    )
    before = page.read_bytes()
    units = _units_of(tmp_path, PAGE)
    target = next(unit for unit in units if unit["category"] == "rule" and "nine" in unit["text"])

    with request_scope(owner_principal(surface="cli")), pytest.raises(
        ValueError, match="SUPERSEDE_TARGET_ALREADY_SUPERSEDED"
    ) as raised:
        _edit_note(
            tmp_path, action="supersede_observation",
            unit=target["id"], text="Opens at eleven.",
        )

    successor = next(unit for unit in units if unit["id"] != target["id"])
    assert successor["id"] in str(raised.value)
    assert page.read_bytes() == before


# --------------------------------------------------------------------------- #
# 1.5a  assess_observation / link_observation
# --------------------------------------------------------------------------- #


def _rich_claim_page(root: Path) -> Path:
    return _write_page(
        root,
        source=_page_source(
            "# Observe\n\n"
            "## Claim\n- category: rule\n- id: claim-1\n\nThe wall is load-bearing.\n"
        ),
    )


def test_assess_observation_refutes_a_claim_with_evidence_keeping_its_text(
    tmp_path: Path,
) -> None:
    _rich_claim_page(tmp_path)
    (target,) = _units_of(tmp_path, PAGE)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            action="assess_observation",
            unit=target["id"],
            verdict="refuted",
            links=[{"relation": "evidenced_by", "page": "[[Surveyor report]]"}],
            why="the surveyor says otherwise",
        )

    unit = result["unit"]
    assert unit["verdict"] == "refuted"
    assert unit["content"] == "The wall is load-bearing."
    assert {r["kind"] for r in unit["relations"]} == {"evidenced_by"}
    with request_scope(owner_principal(surface="cli")):
        history = commands.op_read_memory(
            tmp_path, path=PAGE, include_history=True
        )["revision_history"]["revisions"]
    assert history[0]["reason"] == "the surveyor says otherwise"


def test_link_observation_adds_a_link_keeping_existing_relations(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        source=_page_source(
            "# Observe\n\n"
            "## Claim\n- category: rule\n- id: claim-1\n"
            "- relations: supports: [[Design]]\n\nThe wall is load-bearing.\n"
        ),
    )
    (target,) = _units_of(tmp_path, PAGE)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            action="link_observation",
            unit=target["id"],
            links=[{"relation": "evidenced_by", "page": "[[Surveyor report]]"}],
        )

    relations = {r["kind"] for r in result["unit"]["relations"]}
    assert relations == {"supports", "evidenced_by"}
    assert result["unit"]["content"] == "The wall is load-bearing."


def test_assess_on_a_compact_target_promotes_only_with_a_kind(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source(
            "# Observe\n\n"
            "## Observations\n\n- [rule] Opens at nine. ^obs-1\n- [rule] Opens Saturdays. ^obs-2\n"
        ),
    )
    units = _units_of(tmp_path, PAGE)
    target = next(unit for unit in units if "nine" in unit["text"])
    sibling = next(unit for unit in units if "Saturdays" in unit["text"])

    with request_scope(owner_principal(surface="cli")), pytest.raises(
        ValueError, match="COMPACT_METADATA_REQUIRES_RICH_KIND"
    ) as raised:
        _edit_note(
            tmp_path, action="assess_observation", unit=target["id"], verdict="confirmed",
        )
    assert "correction" in str(raised.value)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            action="assess_observation",
            unit=target["id"],
            verdict="confirmed",
            kind="claim",
        )

    unit = result["unit"]
    assert unit["kind"] == "claim"
    assert unit["verdict"] == "confirmed"
    assert unit["anchor"] == "obs-1"
    assert unit["content"] == "Opens at nine."
    text = page.read_bytes().decode("utf-8")
    # Promotion removes the compact line and appends the rich block at the end of the body.
    assert "- [rule] Opens at nine. ^obs-1" not in text
    assert text.rindex("## Claim") > text.rindex("## Observations")
    # Every other unit parses exactly as before.
    remaining = {unit["id"]: unit for unit in _units_of(tmp_path, PAGE)}
    assert any(
        other["text"] == "Opens Saturdays." and other["id"] == sibling["id"]
        for other in remaining.values()
    )


def test_an_unknown_verdict_refuses_listing_the_admitted_verdicts(tmp_path: Path) -> None:
    _rich_claim_page(tmp_path)
    (target,) = _units_of(tmp_path, PAGE)

    with request_scope(owner_principal(surface="cli")), pytest.raises(
        ValueError, match="INVALID_SEMANTIC_VERDICT"
    ) as raised:
        _edit_note(
            tmp_path, action="assess_observation", unit=target["id"], verdict="bogus",
        )

    message = str(raised.value)
    for admitted in ("refuted", "confirmed"):
        assert admitted in message


def test_edit_observation_forwards_kind_as_of_and_verdict(tmp_path: Path) -> None:
    _rich_claim_page(tmp_path)
    (target,) = _units_of(tmp_path, PAGE)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            action="edit_observation",
            observation=target["id"],
            category="rule",
            text="The wall is load-bearing.",
            verdict="qualified",
            as_of="2026-07-01",
        )

    assert result["unit"]["verdict"] == "qualified"
    assert result["unit"]["as_of"] == "2026-07-01"


# --------------------------------------------------------------------------- #
# 1.6  Units show their supersession
# --------------------------------------------------------------------------- #


def test_page_units_show_both_ends_of_a_supersession(tmp_path: Path) -> None:
    _compact_entity_page(tmp_path)

    with request_scope(owner_principal(surface="cli")):
        result = _edit_note(
            tmp_path,
            note=ENTITY_PAGE,
            action="supersede_observation",
            unit="claim-1",
            text="Cedar Two replaces the Juniper preorder.",
            category="vendors",
            as_of="2026-11-10",
        )

    units = _units_of(tmp_path, ENTITY_PAGE)
    target = next(unit for unit in units if "preorders" in unit["text"])
    successor = next(unit for unit in units if "Cedar" in unit["text"])
    assert target["superseded_by"] == [result["unit_id"]]
    assert successor["supersedes"] == [target["id"]]
    assert successor["as_of"] == "2026-11-10"
    assert successor["superseded_by"] == []


def test_the_currency_dimension_groups_the_same_page_pair(tmp_path: Path) -> None:
    from exomem import epistemic_graph, graph_sync
    from exomem import find as find_module

    _compact_entity_page(tmp_path)
    with request_scope(owner_principal(surface="cli")):
        _edit_note(
            tmp_path,
            note=ENTITY_PAGE,
            action="supersede_observation",
            unit="claim-1",
            text="Cedar Two replaces the Juniper preorder.",
            category="vendors",
        )
    assert graph_sync.drain_active_rebuilds()
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    find_module.clear_cache()

    reply = commands.op_ask_memory(
        tmp_path,
        query="",
        mode="keyword",
        scope="kb-only",
        result_level="unit",
        filters={"unit.dimension": {"$eq": "currency"}},
        limit=20,
    )

    replaced = [
        hit for hit in reply["hits"]
        if (hit.get("display") or {}).get("replaced")
    ]
    assert replaced, reply["hits"]
    assert any("preorders" in hit["excerpt"] for hit in replaced)


# --------------------------------------------------------------------------- #
# 1.7  The audit finds and fixes entity facet claims
# --------------------------------------------------------------------------- #


def _facet_audit_vault(root: Path) -> Path:
    return _write_page(
        root,
        rel=ENTITY_PAGE,
        source=_entity_source(
            "# Harbour Co-op\n\n"
            "## Suppliers\n"
            "- Juniper One takes preorders in November.\n"
            "- [[Cedar Two]], [[Winter catalog]]\n"
            "- supplies [[Cedar Two]]\n\n"
            "## Observations\n\n- [vendors] Opens at nine. ^claim-2\n"
        ),
    )


def _facet_findings(root: Path) -> list:
    report = audit_module.audit(root, categories=["entity_facet_unaddressed"])
    return [f for f in report.findings if f.category == "entity_facet_unaddressed"]


def test_the_audit_reports_facet_prose_items_but_not_links_units_or_relations(
    tmp_path: Path,
) -> None:
    _facet_audit_vault(tmp_path)

    findings = _facet_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.path == ENTITY_PAGE
    assert "Suppliers" in finding.detail
    assert "preorders" in finding.detail


def test_a_dry_run_fix_lists_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    page = _facet_audit_vault(tmp_path)
    before = page.read_bytes()

    report = audit_fix.audit_fix(tmp_path, dry_run=True)

    assert report.plan_id
    assert page.read_bytes() == before


def test_the_local_apply_does_not_convert_facet_items(tmp_path: Path) -> None:
    page = _facet_audit_vault(tmp_path)
    before = page.read_bytes()

    audit_fix.audit_fix(tmp_path)

    assert page.read_bytes() == before


def test_the_reviewed_plan_converts_items_in_place(tmp_path: Path) -> None:
    page = _facet_audit_vault(tmp_path)

    preview = audit_fix.audit_fix(tmp_path, dry_run=True)
    assert preview.plan_id

    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "maintain_memory")
    with request_scope(owner_principal(surface="cli")):
        start = maintenance_jobs.start_leaf(
            command,
            (tmp_path,),
            {
                "mode": "fix",
                "dry_run": False,
                "plan_id": preview.plan_id,
                "why": "convert facet claims to units",
            },
        )
        handle = start()
        deadline = time.monotonic() + 60
        while True:
            status = maintenance_jobs.job_status(tmp_path, handle["run_id"])
            if status["state"] != "running":
                break
            assert time.monotonic() < deadline, f"job still {status}"
            time.sleep(0.05)

    assert status["state"] == "completed", status
    text = page.read_bytes().decode("utf-8")
    # The prose item is now a compact unit under its heading's category, same text, new anchor.
    assert "- [suppliers] Juniper One takes preorders in November. ^" in text
    # Link-only and relation items are untouched; nothing else changed.
    assert "- [[Cedar Two]], [[Winter catalog]]" in text
    assert "- supplies [[Cedar Two]]" in text
    # A later supersede can name the converted unit's anchor.
    assert _facet_findings(tmp_path) == []
