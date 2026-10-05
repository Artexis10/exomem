"""Open categories stay reachable without bypassing categorical role owners."""

from pathlib import Path

import pytest
import yaml

from exomem import context_roles, lexstore, working_set, working_set_index, working_set_runtime
from exomem.working_set_resolve import analyze_turn


def _write(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _note(root: Path, name: str, body: str, *, project="harbor-study", updated="2026-01-01"):
    return _write(
        root, f"Knowledge Base/Notes/{name}.md",
        f"---\ntype: note\nproject: {project}\nstatus: active\nupdated: {updated}\n---\n"
        f"# {name}\n\n{body}\n",
    )


@pytest.fixture
def material_vault(vault: Path) -> Path:
    _write(vault, "Knowledge Base/_Schema/project-keys.yaml", yaml.safe_dump({"projects": {
        "harbor-study": {"folder": "Harbor Study", "category": "research"},
        "orchard-survey": {"folder": "Orchard Survey", "category": "research"},
    }}))
    _note(vault, "older", "- [observation] Turbine calibration uses amber gauges. ^older")
    _note(vault, "newer", "- [observation] The orchard harvest has begun. ^newer", updated="2026-10-01")
    _note(vault, "outside", "- [observation] Turbine calibration outside the study. ^outside", project="orchard-survey")
    _note(vault, "contact", "- [contact] Turbine calibration hotline is confidential. ^contact")
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def test_project_material_is_relevant_scoped_and_does_not_bypass_contact(material_vault: Path):
    # Catches both categorical exclusion and a recency sort erasing content rank.
    packet = working_set.compile_packet(material_vault, turn="harbor-study turbine calibration")
    assert [r["id"] for r in packet["roles"]] == [
        "preferences", "constraints", "recent_change", "active_plans", "material", "precedents",
    ]
    material = [u for u in packet["units"] if u["role"] == "material"]
    assert [u["provenance"]["path"] for u in material] == ["Knowledge Base/Notes/older.md"]
    assert material[0]["provenance"]["category"] == "observation"
    assert not any(p["ref"].endswith("contact.md") for p in packet["pointers"])


def test_quoted_parent_names_do_not_bypass_category_ownership(material_vault: Path):
    # SQLite's JSON path escaping varies by version; parent keys need exact equality.
    path = _note(material_vault, 'quoted"parent',
        "- [contact] Violet worksheet hotline is private. ^private\n"
        "- [observation] Violet worksheet uses amber ink. ^public")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study violet worksheet")
    assert [(unit["provenance"]["path"], unit["provenance"]["category"])
        for unit in packet["units"] if unit["role"] == "material"] == [(path, "observation")]


@pytest.mark.parametrize("mixed", [False, True])
def test_relevant_uncompiled_prose_is_only_a_read_pointer(material_vault: Path, mixed: bool):
    # A mixed page's unrelated units do not cover the prose the user needs.
    body = "Turbine calibration requires the violet worksheet."
    if mixed:
        body += "\n\n- [fact] There are two benches. ^benches"
    path = _note(material_vault, "worksheet", body)
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study violet worksheet")
    pointers = [p for p in packet["pointers"] if p["role"] == "material"]
    assert [p["ref"] for p in pointers] == [path]
    assert "read" in pointers[0]["why"].lower()
    assert not any(u["role"] == "material" for u in packet["units"])


def test_owner_narrowing_applies_to_carried_pages(material_vault: Path):
    # A carried page must not silently restore a default the owner narrowed.
    registry = context_roles.load_roles(material_vault, proposal={"schema_version": 1, "roles": {
        "material": {"anchor_defaults": []},
    }})
    assert "material" in context_roles.shipped_registry().roles
    chosen = working_set._carry_roles(registry, analyze_turn("turbine calibration"), frozenset({"observation"}))
    assert "material" not in [role["id"] for role in chosen]


def test_identity_project_default_survives_an_owner_units_lane(material_vault: Path):
    registry = context_roles.load_roles(material_vault, proposal={"schema_version": 1, "roles": {
        "identity": {"lane": "units"},
    }})
    roles = context_roles.select_roles(registry, anchor_kinds=("project",), analysis=analyze_turn("harbor-study"))
    assert roles[0]["id"] == "identity"
    assert "project" in registry.roles["identity"].anchor_defaults


def test_unselected_owner_aliases_resolve_in_each_parent_scope(material_vault: Path):
    # Global alias expansion would exclude the second project's distinct literal category.
    _write(material_vault, "Knowledge Base/_Schema/semantic-language-registry.yaml", yaml.safe_dump({
        "schema_version": 1,
        "categories": {"calibration_record": {"description": "Calibration records",
            "aliases": ["gauge_report"], "scope": {"projects": ["harbor-study"], "page_types": ["note"]}}},
    }))
    _write(material_vault, "Knowledge Base/_Schema/context-roles.yaml", yaml.safe_dump({
        "schema_version": 1, "roles": {"audit_owner": {"description": "Owned calibration records",
            "lane": "units", "categories": ["gauge_report"], "anchor_defaults": [], "cues": []}},
    }))
    _note(material_vault, "owned-alias", "- [calibration_record] Violet worksheet is owned here. ^owned")
    outside = _note(material_vault, "unowned-literal", "- [calibration_record] Violet worksheet differs here. ^literal", project="orchard-survey")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study and orchard-survey violet worksheet")
    material = [u for u in packet["units"] if u["role"] == "material"]
    assert [u["provenance"]["path"] for u in material] == [outside]
    assert "audit_owner" not in [r["id"] for r in packet["roles"]]


def test_name_alone_does_not_rank_material(material_vault: Path):
    # Being about a project is not relevance evidence for a claim on its page.
    _note(material_vault, "name-only", "- [observation] harbor-study has an unrelated harvest. ^name")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study")
    assert not any(u["role"] == "material" for u in packet["units"])
    assert {"role": "material", "reason": "no_material"} in packet["missing"]


def test_material_rank_survives_packet_recency_order(material_vault: Path):
    # Packet assembly must retain the catalogue's relevance order among material units.
    _note(material_vault, "strong-old", "- [observation] Violet worksheet violet worksheet. ^old", updated="2024-01-01")
    _note(material_vault, "weak-new", "- [observation] Violet details describe " + "a lengthy background description " * 25 + ". ^new", updated="2026-10-01")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study violet worksheet")
    material = [u for u in packet["units"] if u["role"] == "material"]
    assert material[0]["provenance"]["path"].endswith("strong-old.md")


def test_long_turn_keeps_useful_material_for_separate_topics(material_vault: Path):
    # Earlier rare incidental words used to consume the query before either need.
    incidental = (
        "calendar inspection invoice notebook equipment procedure monthly routine "
        "maintenance report reach earlier perform important excellent functional "
        "rain sunshine seeds roots soil garden water seasonal ledger purchase "
        "journal bench archive deadline schedule checklist"
    )
    _note(material_vault, "incidental", f"- [observation] {incidental}. ^incidental")
    gauge = _note(material_vault, "gauge-choice",
        "- [observation] Turbine calibration uses the violet worksheet. ^gauge-choice")
    harvest = _note(material_vault, "harvest-choice",
        "- [observation] Orchard harvest uses shaded crates against heat damage. ^harvest-choice",
        project="orchard-survey")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    topics = (
        "In harbor-study, what guides turbine calibration and the violet worksheet?",
        "In orchard-survey, what guides harvest crates against heat damage?",
    )
    for order in (topics, topics[::-1]):
        working_set_runtime.reset_caches_for_tests()
        packet = working_set.compile_packet(material_vault,
            turn=f"I have been thinking about {incidental}. {' '.join(order)}")
        paths = {unit["provenance"]["path"] for unit in packet["units"]
            if unit["role"] == "material"}
        assert {gauge, harvest} <= paths
        assert len(paths) <= 3


def _allocation_material(root: Path, *, compiled_harvest: bool = False):
    _note(root, "worksheet-procedure", "\n".join((
        "- [observation] Violet worksheet calibration uses amber gauges. ^instrument",
        "- [observation] Violet worksheet calibration starts with a zero-reference reading. ^zero",
        "- [observation] Violet worksheet calibration records the ambient temperature. ^ambient",
    )))
    prose = "Shaded crates protect the orchard harvest against heat damage."
    harvest = _note(root, "harvest-handling",
        f"- [observation] {prose} ^harvest" if compiled_harvest else prose,
        project="orchard-survey")
    rival = _note(root, "orchard-calibration",
        "Violet worksheet calibration at the orchard uses a separate inspection bench.",
        project="orchard-survey")
    _note(root, "calibration-archive", "Violet worksheet calibration has archived background essays.")
    lexstore.ensure_fresh(root)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    return harvest, rival


@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_named_requests_keep_compiled_claims_and_relevant_prose(
    material_vault: Path, reverse: bool,
):
    # Three distinct useful claims must not crowd out the other request's prose.
    harvest, rival = _allocation_material(material_vault)
    requests = (
        "In harbor-study, what guides violet worksheet calibration?",
        "In orchard-survey: what guides shaded crates against heat damage?",
    )
    packet = working_set.compile_packet(material_vault,
        turn=" ".join(requests[::-1] if reverse else requests))
    units = [unit for unit in packet["units"] if unit["role"] == "material"]
    pointers = [pointer for pointer in packet["pointers"] if pointer["role"] == "material"]
    assert len(units) == 2
    assert all("worksheet-procedure.md" in unit["ref"] for unit in units)
    assert [pointer["ref"] for pointer in pointers] == [harvest]
    assert rival not in [pointer["ref"] for pointer in pointers]


@pytest.mark.parametrize("turn", [
    "In harbor-study, what guides violet worksheet calibration?",
    "In harbor-study, what guides violet worksheet calibration? orchard-survey",
    "In harbor-study and orchard-survey, what guides violet worksheet calibration "
    "and shaded crates against heat damage?",
])
def test_a_single_request_keeps_three_claims_without_a_reserved_pointer(
    material_vault: Path, turn: str,
):
    # Bare names and a segment naming two contexts earn no separate request slot.
    _allocation_material(material_vault)
    packet = working_set.compile_packet(material_vault, turn=turn)
    units = [unit for unit in packet["units"] if unit["role"] == "material"]
    assert len(units) == 3
    assert all("worksheet-procedure.md" in unit["ref"] for unit in units)
    assert not [pointer for pointer in packet["pointers"] if pointer["role"] == "material"]


def test_a_literal_name_keeps_its_internal_request_separator(material_vault: Path):
    # The semicolon in an admitted title must not detach its request's query words.
    _write(material_vault, "Knowledge Base/_Schema/project-keys.yaml", yaml.safe_dump({"projects": {
        "harbor-study": {"folder": "Harbor; Study", "category": "research"},
        "orchard-survey": {"folder": "Orchard Survey", "category": "research"},
    }}))
    harvest, _rival = _allocation_material(material_vault)
    packet = working_set.compile_packet(material_vault, turn=(
        "In harbor-study (Harbor; Study), what guides violet worksheet calibration? "
        "In orchard-survey, what guides shaded crates against heat damage?"))
    assert len([unit for unit in packet["units"] if unit["role"] == "material"]) == 2
    assert [pointer["ref"] for pointer in packet["pointers"]
        if pointer["role"] == "material"] == [harvest]


def test_a_compiled_claim_can_cover_the_second_request(material_vault: Path):
    # Coverage prefers a compiled answer; it does not reserve a prose pointer.
    harvest, _rival = _allocation_material(material_vault, compiled_harvest=True)
    packet = working_set.compile_packet(material_vault, turn=(
        "In harbor-study, what guides violet worksheet calibration; "
        "in orchard-survey, what guides shaded crates against heat damage?"))
    units = [unit for unit in packet["units"] if unit["role"] == "material"]
    assert len(units) == 3
    assert harvest in [unit["provenance"]["path"] for unit in units]
    assert not [pointer for pointer in packet["pointers"] if pointer["role"] == "material"]


def test_repeated_named_request_has_no_extra_allocation_weight(material_vault: Path):
    # Repeating the first need cannot consume the second need's slot.
    harvest, _rival = _allocation_material(material_vault)
    first = "In harbor-study, what guides violet worksheet calibration? "
    packet = working_set.compile_packet(material_vault, turn=first * 3 +
        "In orchard-survey, what guides shaded crates against heat damage?")
    assert len([unit for unit in packet["units"] if unit["role"] == "material"]) == 2
    assert [pointer["ref"] for pointer in packet["pointers"]
        if pointer["role"] == "material"] == [harvest]


def test_focus_name_offsets_cannot_allocate_a_current_turn_request(material_vault: Path):
    # Focus token zero must not borrow the turn's calibration query for its context.
    _allocation_material(material_vault)
    packet = working_set.compile_packet(material_vault,
        turn="In harbor-study, what guides violet worksheet calibration?",
        conversation=working_set.working_set_conversation.Conversation(
            focus="orchard-survey", state="applied"))
    units = [unit for unit in packet["units"] if unit["role"] == "material"]
    assert len(units) == 3
    assert all("worksheet-procedure.md" in unit["ref"] for unit in units)
    assert not [pointer for pointer in packet["pointers"] if pointer["role"] == "material"]


def test_wider_material_query_still_filters_common_background(material_vault: Path):
    # Expanding the allowance must not turn off existing common-word filtering.
    common = (
        "calendar inspection invoice notebook equipment procedure monthly routine "
        "maintenance report rain sunshine seeds roots soil garden water seasonal "
        "ledger purchase journal bench archive deadline schedule checklist"
    )
    for number in range(20):
        _note(material_vault, f"incidental-{number}",
            f"- [observation] {common}. ^incidental-{number}")
    for number in range(100):
        _note(material_vault, f"other-{number}",
            f"- [observation] Distinct fauna species specimen sample {number}. ^other-{number}")
    target = _note(material_vault, "violet-decision",
        "- [observation] Turbine calibration uses violet worksheets. ^violet-decision")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn=(
        f"I finished thinking about {common}. "
        "In harbor-study, what guides turbine calibration violet worksheets?"
    ))
    paths = {unit["provenance"]["path"] for unit in packet["units"]
        if unit["role"] == "material"}
    assert target in paths
    assert not any("incidental-" in path for path in paths)


def test_role_ceiling_reports_material_omission(material_vault: Path):
    # Six higher-priority lanes cannot silently look like material was searched.
    packet = working_set.compile_packet(material_vault, turn="harbor-study who is currently turbine calibration")
    assert len(packet["roles"]) == 6
    assert "material" not in [r["id"] for r in packet["roles"]]
    assert {"role": "material", "reason": "role_limit"} in packet["missing"]


def test_category_and_parent_exclusions_precede_the_candidate_window(material_vault: Path):
    # A full window of owned or foreign matches must not hide the allowed claim.
    for name, category, project in (
        ("owned-bulk", "contact", "harbor-study"),
        ("foreign-bulk", "observation", "orchard-survey"),
    ):
        _note(material_vault, name, "\n".join(
            f"- [{category}] Turbine calibration turbine calibration. ^bulk-{i}"
            for i in range(205)
        ), project=project, updated="2026-10-01")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study turbine calibration")
    assert [u["provenance"]["path"] for u in packet["units"] if u["role"] == "material"] == [
        "Knowledge Base/Notes/older.md",
    ]


def test_material_cap_is_shared_by_units_and_prose_pointers(material_vault: Path):
    # Carried contexts share one served-material allowance, including page pointers.
    _note(material_vault, "many", "\n".join(
        f"- [observation] Violet worksheet reading {i}. ^row-{i}" for i in range(201)
    ))
    for i in range(4):
        _note(material_vault, f"prose-{i}", "Violet worksheet is needed for the notebook.")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study violet worksheet")
    assert sum(item["role"] == "material" for item in packet["units"] + packet["pointers"]) <= 3
    assert {"role": "material", "reason": "lane_truncated"} in packet["missing"]


@pytest.mark.parametrize("failed", [False, True])
def test_empty_or_failed_carried_attempts_remain_visible(material_vault: Path, monkeypatch, failed: bool):
    # Returning None from the carry used to erase both catalogue failure and a true miss.
    if failed:
        monkeypatch.setattr(lexstore.LexicalStore, "search_semantic_units_result", lambda *a, **k:
            lexstore.CatalogQueryResult(None, lexstore.CatalogReadiness("unavailable", False, "sqlite")))
    packet = working_set.compile_packet(
        material_vault, turn="unknown subject", anchor="Knowledge Base/Notes/older.md",
    )
    assert packet["abstained"] is True
    assert {"role": "material", "reason": "lane_failed" if failed else "no_material"} in packet["missing"]


def test_recency_context_never_selects_material():
    # The material exception must not turn continuity alone into query-relevance evidence.
    roles = context_roles.select_roles(
        context_roles.shipped_registry(), anchor_kinds=("hub",),
        analysis=analyze_turn("continue"), prior_only_kinds=frozenset({"hub"}),
    )
    assert "material" not in [role["id"] for role in roles]


def test_a_shared_word_in_a_unit_does_not_cover_separate_prose(material_vault: Path):
    # Coverage is an authored source span, not every occurrence of a unit's word.
    path = _note(material_vault, "mixed-words",
        "Only violet worksheets can calibrate the sensors.\n\n- [fact] Violet ^color")
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study violet")
    assert path in [p["ref"] for p in packet["pointers"] if p["role"] == "material"]


def test_scoped_rich_blocks_do_not_masquerade_as_uncompiled_prose(material_vault: Path):
    # Custom headings and rich metadata require the parser's exact source coverage.
    _write(material_vault, "Knowledge Base/_Schema/semantic-language-registry.yaml", yaml.safe_dump({
        "schema_version": 1, "kinds": {"calibration_record": {
            "description": "Calibration records", "heading_aliases": ["Gauge Register"],
            "scope": {"projects": ["harbor-study"], "page_types": ["note"]},
        }},
    }))
    rich = "## Gauge Register\n- category: contact\n- id: rich-contact\n\nViolet worksheet contact details.\n"
    _note(material_vault, "covered-rich", "\n\n" + rich)
    mixed = _note(material_vault, "mixed-rich", "Violet worksheet has a separate public schedule.\n\n" + rich)
    lexstore.ensure_fresh(material_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(material_vault).rebuild()
    packet = working_set.compile_packet(material_vault, turn="harbor-study violet worksheet")
    assert [p["ref"] for p in packet["pointers"] if p["role"] == "material"] == [mixed]
    assert not any(u["role"] == "material" for u in packet["units"])


@pytest.mark.parametrize("separate_role", [False, True])
def test_material_overflow_from_separate_contexts_is_reported(separate_role: bool):
    # Each carried page may fit locally while their combined lane exceeds its allowance.
    packet = working_set.build_packet(
        items=tuple(working_set.LaneItem(
            role="other_material" if separate_role and i >= 2 else "material",
            level="unit" if i < 2 else "page", ref=f"{'z' if i < 2 else 'a'}-page-{i}",
            path=f"page-{i}", title=f"Page {i}", text="A relevant claim." if i < 2 else "",
            lifecycle="active", updated="", anchor=f"page-{i}",
        ) for i in range(4)),
        anchors=(), roles=tuple({"id": role, "lane": "material", "source": "retrieval_carried"}
            for role in ("material", "other_material") if separate_role or role == "material"),
        current_state=(), ambiguity=(), missing=(), max_chars=4000, generation={}, status="resolved",
    )
    assert len(packet["units"]) == 2
    assert len(packet["pointers"]) == 1
    assert len(packet["units"]) + len(packet["pointers"]) == 3
    assert {"role": "other_material" if separate_role else "material",
        "reason": "lane_truncated"} in packet["missing"]


@pytest.mark.parametrize("level", ["page", "unit"])
def test_ranked_material_pointer_spends_its_slot_before_later_claims(level: str):
    # Both authored prose and a downgraded claim retain the allocation they won.
    packet = working_set.build_packet(
        items=(working_set.LaneItem(
            role="material", level=level, ref="ranked", path="ranked", title="Ranked page",
            text="Long claim " * 100 if level == "unit" else "", lifecycle="active",
            updated="", anchor="ranked", relevance_order=0,
        ), *(working_set.LaneItem(
            role="material", level="unit", ref=f"claim-{i}", path=f"claim-{i}",
            title=f"Claim {i}", text=f"Useful claim {i}.", lifecycle="active", updated="",
            anchor=f"claim-{i}", relevance_order=i,
        ) for i in range(1, 4))),
        anchors=(), roles=({"id": "material", "lane": "material"},), current_state=(),
        ambiguity=(), missing=(), max_chars=4000, generation={}, status="resolved",
    )
    assert [unit["ref"] for unit in packet["units"]] == ["claim-1", "claim-2"]
    assert [pointer["ref"] for pointer in packet["pointers"]] == ["ranked"]
    assert packet["pointers"][0]["reason"] == ("requires_read" if level == "page" else "unit_too_long")


def test_ranked_material_pointer_spends_chars_before_a_later_claim():
    # A lower-ranked claim must not spend characters already allocated to a pointer.
    packet = working_set.build_packet(
        items=(working_set.LaneItem(
            role="material", level="page", ref="ranked", path="ranked", title="Ranked page",
            text="", lifecycle="active", updated="", anchor="ranked", relevance_order=0,
            why="Relevant authored prose; read the page for its content.",
        ), *(working_set.LaneItem(
            role="material", level="unit", ref=f"claim-{i}", path=f"claim-{i}",
            title=f"Claim {i}", text="x" * length, lifecycle="active", updated="",
            anchor=f"claim-{i}", relevance_order=i,
        ) for i, length in ((1, 260), (2, 200)))),
        anchors=(), roles=({"id": "material", "lane": "material"},), current_state=(),
        ambiguity=(), missing=(), max_chars=500, generation={}, status="resolved",
    )
    assert [unit["ref"] for unit in packet["units"]] == ["claim-1"]
    assert "ranked" in [pointer["ref"] for pointer in packet["pointers"]]
    assert packet["budget"]["used_chars"] <= 500


@pytest.mark.parametrize("role", ["constraints", "identity", "methods"])
def test_material_pointer_respects_role_priority(role: str):
    # Protect both deferred constraints and identity pages not yet processed
    # when a lower-priority material pointer competes for the remaining budget.
    constraint = role == "constraints"
    material_first = role == "methods"
    roles = ({"id": role, "lane": "entity" if role == "identity" else "units"},
        {"id": "material", "lane": "material"})
    packet = working_set.build_packet(
        items=(working_set.LaneItem(
            role=role, level="page" if role == "identity" else "unit",
            ref="earlier", path="earlier", title="Handling constraint" if constraint else "Context identity",
            text="Scope-qualified handling requirement. " * 35 if constraint else "A" * 50,
            lifecycle="active", updated="", anchor="earlier",
            provenance={"source": "profile"} if role == "identity" else {},
        ), working_set.LaneItem(
            role="material", level="page", ref="prose", path="prose", title="Field worksheet",
            text="", lifecycle="active", updated="", anchor="prose",
            why="Read the field worksheet.",
        )),
        anchors=(), roles=tuple(reversed(roles)) if material_first else roles,
        current_state=({"path": "state-one", "statement": "x" * 220},
            {"path": "state-two", "statement": "y" * 220}),
        ambiguity=(), missing=(), max_chars=500, generation={}, status="resolved",
    )
    if material_first:
        assert packet["units"] == []
        assert [(p["ref"], p["reason"]) for p in packet["pointers"]] == [("prose", "requires_read")]
    elif constraint:
        assert packet["units"] == []
        assert [(p["ref"], p["reason"]) for p in packet["pointers"]] == [("earlier", "unit_too_long")]
    else:
        assert [(u["ref"], u["text"]) for u in packet["units"]] == [("earlier", "A" * 50)]
        assert packet["pointers"] == []
    assert packet["missing"] == [{"role": role if material_first else "material", "reason": "budget"}]
    assert packet["budget"]["used_chars"] <= 500
