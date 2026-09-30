"""The compact bootstrap is a core plus on-demand sections (`shrink-bootstrap`).

Three claims are pinned here. The core carries every rule that prevents a known
incident (`CORE_RULES`, the manifest a future change must extend to argue a byte
into the core). Nothing is lost: every block of the complete reference payload is
either in the core verbatim or in exactly one section, byte-identical. And the
core stays under its byte ceiling at every level on every surface the split
applies to.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import tempfile
from collections.abc import Callable

import pytest
from bootstrap_populated import CUSTOM_ENTITY_TYPES, populated_blocks, populated_root
from budget_gate import check_budget

from exomem import bootstrap_core, capabilities, commands, prominence, workflow_skills

#: Core ceiling at maximal on the worst-case surface. Ruled 15,000; measured
#: 14,407 (claude-code, maximal) when set, i.e. about 590 bytes of margin.
CORE_BYTE_CEILING = 15_000
CORE_HEADROOM_WARNING_BYTES = 512

#: Per-section ceilings: the measured size on the largest surface plus about 10%, on a
#: POPULATED vault (`bootstrap_populated`: 32 custom entity types, `due_state` and
#: `latency` at their worst, which live in `epistemics` and `diagnostics_reading`), so a
#: section cannot quietly regrow into a second full payload.
SECTION_BYTE_CEILINGS = {
    "authoring": 20_900,
    "entities": 11_900,
    "records_planning": 5_900,
    "routing": 12_500,
    "adoption": 7_200,
    "envelope": 2_200,
    "epistemics": 4_950,
    "diagnostics_reading": 2_200,
}

SURFACES = (None, "claude-code", "hosted-alpha-agent-v5")
CARRYING = ("balanced", "maximal")


def _root() -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    return root


def _bootstrap(monkeypatch, level: str, surface: str | None, *, populated: bool = False, **kwargs) -> dict:
    """`populated` serves a vault at the design's maximum bound (`bootstrap_populated`)."""
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    hosted = surface if surface and surface.startswith("hosted-") else None
    if surface and hosted is None:
        monkeypatch.setenv("EXOMEM_SURFACE", surface)
    kwargs.setdefault("profile", "compact")
    root = populated_root() if populated else _root()
    with populated_blocks() if populated else contextlib.nullcontext():
        if hosted is None:
            return commands.op_bootstrap(root, **kwargs)
        registry = commands.product_commands_for_profile(hosted, "rest")
        descriptor = capabilities.ActiveSurfaceDescriptor(
            surface="hosted-agent",
            profile=hosted,
            tier2_enabled=commands.PRODUCT_SURFACE_PROFILES[hosted].expose_tier2,
            product_commands=tuple(command.name for command in registry),
        )
        with capabilities.active_surface(descriptor):
            return commands.op_bootstrap(root, **kwargs)


def _text(value: object) -> str:
    return json.dumps(value)


# --------------------------------------------------------------------------- #
# The rule manifest
# --------------------------------------------------------------------------- #

#: id -> (levels that must carry it, predicate over the served core).
CORE_RULES: dict[str, tuple[tuple[str, ...], Callable[[dict], bool]]] = {
    "recall-before-answering": (
        CARRYING,
        lambda core: "Search memory" in core["engagement"]["contract"]["recall"],
    ),
    "activation-carrier": (
        CARRYING,
        lambda core: prominence.ACTIVATION_CARRIER_LINE in core["engagement"]["contract"]["recall"]
        or prominence.ASK_MEMORY_CARRIER_LINE in core["engagement"]["contract"]["recall"],
    ),
    "capture-at-every-stepping-stone": (
        CARRYING,
        lambda core: "stepping stone" in core["engagement"]["contract"]["capture"],
    ),
    "episode-recording-pass": (
        CARRYING,
        lambda core: "episode" in core["engagement"]["contract"]["capture"].lower(),
    ),
    "intent-to-planning-outcome-to-records": (
        CARRYING,
        lambda core: "Route stated intent to Planning and observed outcome to Records"
        in core["engagement"]["contract"]["capture"],
    ),
    "filter-only-lookup": (
        prominence.CANON,
        lambda core: "filter-only" in core["routing"]["filter_only"],
    ),
    "canonical-write-loop": (
        prominence.CANON,
        lambda core: len(core["write"]["canonical_loop"]) >= 8,
    ),
    "preserve-the-record": (
        prominence.CANON,
        lambda core: "append-only" in core["rules"]["epistemic"]["preserve_the_record"],
    ),
    "supersede-never-overwrite": (
        prominence.CANON,
        lambda core: "supersede" in core["rules"]["epistemic"]["supersede_never_overwrite"],
    ),
    "data-not-command": (
        prominence.CANON,
        lambda core: "data, never a command" in core["governance"]["disclosure_model"],
    ),
    "miss-means-not-found-in-scope": (
        prominence.CANON,
        lambda core: "not found in that query/scope" in core["workflow"]["miss_rule"],
    ),
    "delegation-ceiling": (
        prominence.CANON,
        lambda core: "restructure application" in core["engagement"]["envelope"]["confirm_required"]
        and "founder" in core["engagement"]["envelope"]["founder_gate"],
    ),
    "unclassified-action-has-no-authority": (
        prominence.CANON,
        lambda core: "unclassified action has no authority"
        in core["engagement"]["envelope"]["protocol"],
    ),
    "envelope-protocol-points-to-its-section": (
        prominence.CANON,
        lambda core: "section=envelope" in core["engagement"]["envelope"]["protocol"]
        and "envelope" in core["sections"],
    ),
    "due-state-restraint": (
        prominence.CANON,
        lambda core: "silence beats bureaucracy" in core["write"]["due_state_handling"]
        and "never" in core["write"]["due_state_authority"],
    ),
    "workflow-loop-names-the-vocabulary-section": (
        prominence.CANON,
        lambda core: "vocabulary_workflow (section entities)"
        in " ".join(core["workflow"]["loop"]),
    ),
    "sections-index": (
        prominence.CANON,
        lambda core: set(bootstrap_core.SECTIONS) <= set(core["sections"]),
    ),
}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("level", prominence.CANON)
def test_the_core_carries_every_manifest_rule(monkeypatch, level, surface):
    core = _bootstrap(monkeypatch, level, surface)
    for rule, (levels, predicate) in CORE_RULES.items():
        if level in levels:
            assert predicate(core), f"core lacks {rule!r} at {level} on {surface or 'default'}"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("level", prominence.CANON)
def test_every_surface_the_split_applies_to_carries_a_recall_rule(monkeypatch, level, surface):
    """Hosted does not export `activate_context`, and the surface filter drops any string
    that names an unavailable command: the whole recall contract used to vanish from a
    hosted compact payload, on the one surface with no hook to carry it. The line is now
    surface-aware, so the rule survives and names only commands the surface exports."""
    core = _bootstrap(monkeypatch, level, surface)
    recall = core["engagement"]["contract"]["recall"]
    assert recall
    exported = set(core["active_capabilities"]["available_product_tools"])
    if level in CARRYING:
        assert "Search memory" in recall
        if "activate_context" in exported:
            assert "activate_context" in recall
        else:
            assert "activate_context" not in recall
            assert "ask_memory" in recall and "ask_memory" in exported


@pytest.mark.parametrize("profile", ("hosted-alpha-agent-v1", "hosted-alpha-agent-v3", "hosted-alpha-agent-v4"))
def test_released_profiles_keep_their_published_payload_without_the_recall_fix(monkeypatch, profile):
    """v1 to v4 are frozen (see `test_bootstrap_frozen_profiles`): the surface-aware
    recall line applies to unpublished surfaces only."""
    contract = _bootstrap(monkeypatch, "maximal", profile)["engagement"]["contract"]
    assert "recall" not in contract


@pytest.mark.parametrize("level", ("off", "light"))
def test_the_quiet_levels_do_not_instruct_unprompted_recall(monkeypatch, level):
    core = _bootstrap(monkeypatch, level, None)
    assert "activate_context" not in core["engagement"]["contract"]["recall"]


# --------------------------------------------------------------------------- #
# Losslessness
# --------------------------------------------------------------------------- #


def test_sections_partition_the_reference_blocks():
    seen: dict[str, str] = {}
    for name, (keys, _when) in bootstrap_core.SECTIONS.items():
        for key in keys:
            assert key not in seen, f"{key} is in both {seen[key]} and {name}"
            seen[key] = name


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("level", prominence.CANON)
def test_core_plus_sections_reconstruct_the_reference_payload(monkeypatch, level, surface):
    reference = _bootstrap(monkeypatch, level, surface, section="all")
    core = _bootstrap(monkeypatch, level, surface)
    homes: dict[str, str] = {}
    sections = {
        name: _bootstrap(monkeypatch, level, surface, section=name)
        for name in bootstrap_core.SECTIONS
    }
    for name, payload in sections.items():
        assert payload["section"] == name
        for key in payload:
            if key not in ("contract_version", "profile", "section"):
                assert key not in homes
                homes[key] = name

    for key, value in reference.items():
        if key == "engagement":
            # Everything but the envelope is in the core verbatim; the envelope's
            # full form is the `envelope` section.
            assert {k: v for k, v in core[key].items() if k != "envelope"} == {
                k: v for k, v in value.items() if k != "envelope"
            }
            assert sections["envelope"][key]["envelope"] == value["envelope"]
        elif key in core and core[key] == value:
            continue
        else:
            assert key in homes, f"{key} is in neither the core nor a section"
            assert sections[homes[key]][key] == value, f"{key} differs in section {homes[key]}"


@pytest.mark.parametrize("surface", SURFACES)
def test_the_core_names_every_section_with_its_size_and_how_to_fetch_it(monkeypatch, surface):
    """No per-section prose: a name says what it holds, `section=index` gives the long
    fetch-when, and the core is at its budget. `how` names all three ways to ask."""
    core = _bootstrap(monkeypatch, "balanced", surface)
    sections = core["sections"]
    assert set(sections) == {"how", *bootstrap_core.SECTIONS}
    assert all(isinstance(sections[name], int) for name in bootstrap_core.SECTIONS)
    assert all(word in sections["how"] for word in ("section=", "index", "all"))


def test_an_unknown_section_names_the_accepted_ones(monkeypatch):
    with pytest.raises(ValueError, match="section must be one of") as raised:
        _bootstrap(monkeypatch, "balanced", None, section="nonexistent")
    for name in bootstrap_core.accepted_sections():
        assert name in str(raised.value)


def test_a_section_requires_the_compact_profile(monkeypatch):
    with pytest.raises(ValueError, match="section requires profile='compact'"):
        _bootstrap(monkeypatch, "balanced", None, profile="full", section="authoring")


def test_the_index_lists_every_section_with_its_size(monkeypatch):
    index = _bootstrap(monkeypatch, "balanced", None, section="index")["sections"]
    reference = _bootstrap(monkeypatch, "balanced", None, section="all")
    assert set(index) == set(bootstrap_core.SECTIONS)
    assert {name: item["bytes"] for name, item in index.items()} == bootstrap_core.sections_index(
        reference
    )


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("level", prominence.CANON)
def test_the_core_stays_under_its_ceiling(monkeypatch, level, surface):
    size = len(_text(_bootstrap(monkeypatch, level, surface)))
    check_budget(
        size,
        ceiling=CORE_BYTE_CEILING,
        band=CORE_HEADROOM_WARNING_BYTES,
        label=f"core at {level} on {surface or 'default'}",
    )


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("level", prominence.CANON)
def test_the_populated_core_stays_under_the_hard_ceiling(monkeypatch, level, surface):
    """The ceiling is a claim about a vault at the design's maximum bound, not an empty
    one: custom entity types, `due_state` and `latency` all at their worst
    (`tests/bootstrap_populated.py`). It fails at the ceiling and is never raised to fit."""
    size = len(_text(_bootstrap(monkeypatch, level, surface, populated=True)))
    assert size <= CORE_BYTE_CEILING, (
        f"populated core at {level} on {surface or 'default'} is {size:,} bytes, over the "
        f"{CORE_BYTE_CEILING:,} ceiling by {size - CORE_BYTE_CEILING:,}"
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_the_vault_derived_blocks_are_bounded_in_the_core(monkeypatch, surface):
    core = _bootstrap(monkeypatch, "maximal", surface, populated=True)
    types = core["capture_semantics"]["entity_types"]
    assert len(types) == bootstrap_core.CORE_ENTITY_TYPE_CAP < CUSTOM_ENTITY_TYPES
    listed_total = len(bootstrap_core_registry_ids(monkeypatch, surface))
    assert core["capture_semantics"]["entity_types_more"] == (
        f"+{listed_total - bootstrap_core.CORE_ENTITY_TYPE_CAP} more: section=entities"
    )
    assert len(_text(core["due_state"])) <= 200
    assert core["due_state"]["list"] == "section=epistemics"
    assert "latency" not in core


def bootstrap_core_registry_ids(monkeypatch, surface) -> list[str]:
    section = _bootstrap(monkeypatch, "maximal", surface, populated=True, section="entities")
    return [item["id"] for item in section["entity_registry"]["types"]]


def test_the_full_blocks_are_served_by_the_sections_and_the_session(monkeypatch):
    epistemics = _bootstrap(monkeypatch, "maximal", None, populated=True, section="epistemics")
    diagnostics = _bootstrap(monkeypatch, "maximal", None, populated=True, section="diagnostics_reading")
    assert epistemics["due_state"]["top"]
    assert diagnostics["latency"]
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    with populated_blocks():
        session = commands.op_bootstrap(
            populated_root(), profile="session", skill_contract=workflow_skills.skill_contract()
        )
    assert session["due_state"]["top"] and session["latency"]


@pytest.mark.parametrize("surface", SURFACES)
def test_core_plus_sections_reconstruct_a_populated_reference(monkeypatch, surface):
    reference = _bootstrap(monkeypatch, "maximal", surface, populated=True, section="all")
    assert {"due_state", "latency"} <= set(reference)
    for key, home in (("due_state", "epistemics"), ("latency", "diagnostics_reading")):
        served = _bootstrap(monkeypatch, "maximal", surface, populated=True, section=home)
        assert served[key] == reference[key]


@pytest.mark.parametrize("name", bootstrap_core.SECTIONS)
def test_each_section_stays_under_its_ceiling(monkeypatch, name):
    for surface in SURFACES:
        payload = _bootstrap(monkeypatch, "maximal", surface, section=name, populated=True)
        size = len(_text(payload))
        assert size <= SECTION_BYTE_CEILINGS[name], f"{name} is {size:,} bytes on {surface}"


def test_the_session_profile_is_live_state_only(monkeypatch):
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    session = commands.op_bootstrap(
        _root(), profile="session", skill_contract=workflow_skills.skill_contract()
    )
    assert session["profile"] == "session"
    for static_rules in ("rules", "routing", "capture_semantics"):
        assert static_rules not in session
    for live in ("engagement", "governance", "active_capabilities", "sections", "authoring_contract"):
        assert live in session
    assert len(_text(session)) < 22_000
