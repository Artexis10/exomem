"""Explicit request capacity, bounded without hiding competing senses."""

from pathlib import Path
from dataclasses import replace

import pytest
import yaml

from exomem import lexstore, working_set, working_set_index, working_set_runtime
from exomem import working_set_resolve as resolver
from test_working_set_material import _note, _write
from test_working_set_resolve import _facts, _row


TOPICS = (
    ("amber-lab", "gauge calibration", "violet worksheet"),
    ("cedar-kiln", "glaze firing", "ceramic cones"),
    ("orchard-survey", "harvest handling", "shaded crates"),
    ("glacier-expedition", "rope inspection", "braided fibers"),
    ("marble-studio", "pigment mixing", "ochre powder"),
    ("raven-workshop", "bearing lubrication", "mineral grease"),
    ("tulip-kitchen", "bread fermentation", "rye starter"),
)


@pytest.fixture
def capacity_vault(vault: Path):
    _write(vault, "Knowledge Base/_Schema/project-keys.yaml", yaml.safe_dump({
        "projects": {key: {"folder": key.replace("-", " ").title(), "category": "research"}
            for key, _property, _value in TOPICS},
    }))
    paths = {
        key: _note(vault, key,
            f"- [observation] {property_name.title()} uses {value}. ^method", project=key)
        for key, property_name, value in TOPICS
    }
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault, paths


@pytest.mark.parametrize("count", [4, 7])
def test_each_evidenced_named_request_survives_in_both_orders(capacity_vault, count):
    # The three-item cap (and six-anchor cap) erased distinct supported answers.
    vault, paths = capacity_vault
    topics = TOPICS[:count]
    for order in (topics, topics[::-1]):
        packet = working_set.compile_packet(vault, turn=" ".join(
            f"In {key}, what guides {property_name}?" for key, property_name, _value in order))
        assert not packet["abstained"]
        assert {unit["provenance"]["path"] for unit in packet["units"]
            if unit["role"] == "material"} == {paths[key] for key, _property, _value in topics}
        assert packet["budget"]["used_chars"] <= 4000
        assert len(packet["roles"]) <= 6


@pytest.mark.parametrize("connected", [6, 24])
def test_late_competitor_cannot_be_cut_into_a_unique_sense(connected):
    # A connected prefix used to hide the disconnected sense beyond either cut.
    rows = [_row(f"a-{i:02}.md", "Copper Beacon", neighbourhood=("shared.md",))
        for i in range(connected)]
    rows.append(_row("z-rival.md", "Copper Beacon"))
    analysis = resolver.analyze_turn("Copper Beacon")
    result = resolver.resolve(resolver.candidates_for(analysis, rows), turn_tokens=analysis.tokens)
    assert result.status == "ambiguous"


@pytest.mark.parametrize("count, width", [(24, 10), (24, 300), (33, 10)])
def test_minted_continuity_never_exceeds_its_decoder(count, width):
    # Legitimate large packets must not mint an unusable token or trim its refs.
    refs = [f"page-{index}-" + "x" * width for index in range(count)]
    packet = {"anchors": [{"ref": ref, "status": "resolved"} for ref in refs]}
    token = working_set_runtime.mint_continuity(packet, identity="capacity")
    if count == 24 and width == 10:
        assert token
    assert token == "" or set(working_set_runtime.decode_continuity(token)["refs"]) == set(refs)


def test_focus_cannot_upgrade_weak_turn_contact_into_expanded_admission():
    # Focus exact names merged into current-turn partials still have only six slots.
    from exomem import working_set_conversation as conversation
    rows = [_row(f"item-{i}", f"Beacon{i} station") for i in range(7)]
    analysis = resolver.analyze_turn(" ".join(f"Beacon{i}" for i in range(7)))
    context = conversation.Conversation(focus="; ".join(row.title for row in rows), state="applied")
    candidates, _origins, _entries = conversation.apply(
        resolver.candidates_for(analysis, rows), conversation.analyze(context), context,
        rows=rows, routing_targets={})
    result = resolver.resolve(candidates, turn_tokens=conversation.analyze(context).turn_tokens(analysis))
    assert len(result.resolved_anchors) == 6


@pytest.mark.parametrize("evidence, segment, expected", [
    (("exact_alias",), "turn", 24),
    (("agent_choice",), "focus", 24),
    (("exact_alias",), "focus", 6),
    (("retrieval",), "turn", 6),
    (("recency",), "turn", 6),
])
def test_only_explicit_turn_names_and_agent_choices_expand_admission(evidence, segment, expected):
    # A focus name, recall hit or recency signal must not inherit the explicit allowance.
    candidates = tuple(replace(_facts(f"item-{i:02}", evidence=evidence,
        exact_alias_phrases=(f"name{i}",), neighbourhood=("shared",)),
        name_span_segment=segment) for i in range(30))
    result = resolver.resolve(candidates, referential=evidence == ("recency",))
    assert len(result.anchors) == expected
    assert result.truncated


def test_explicit_anchors_spend_the_ordinary_allowance_first():
    # Six explicit names cannot leave six additional slots for weaker candidates.
    explicit = tuple(_facts(f"explicit-{i}", evidence=("exact_alias",),
        exact_alias_phrases=(f"name{i}",)) for i in range(7))
    partial = tuple(_facts(f"partial-{i}", evidence=("retrieval",)) for i in range(6))
    result = resolver.resolve((*explicit, *partial))
    assert {anchor.anchor_id for anchor in result.anchors} == {item.anchor_id for item in explicit}


def test_resolved_lexical_contexts_survive_stronger_ranked_partial_hits():
    # Recall hits with more evidence kinds must not crowd every resolved context out.
    rows = [replace(_row(f"a{i}.md", f"Other option {i}", kind="resource",
        neighbourhood=("z0.md", "z1.md")), categories=("preference",)) for i in range(6)]
    rows += [
        _row("z0.md", "Copper Northern Beacon", kind="resource",
            terms=("copper", "northern", "beacon"), neighbourhood=("z1.md",)),
        _row("z1.md", "Maple Southern Beacon", kind="resource",
            terms=("maple", "southern", "beacon"), neighbourhood=("z0.md",)),
    ]
    analysis = resolver.analyze_turn("I prefer copper beacon and maple beacon")
    candidates = resolver.add_graph_corroboration(resolver.candidates_for(analysis, rows,
        retrieval_paths=frozenset(row.path for row in rows[:6]),
        eligible_categories=frozenset({"preference"})))
    result = resolver.resolve(candidates, turn_tokens=analysis.tokens)
    assert result.status == "resolved"
    assert {anchor.path for anchor in result.resolved_anchors} == {"z0.md", "z1.md"}
    assert len(result.anchors) == 6


@pytest.mark.parametrize("other", ["bare", "focus", "unsupported", "repeat"])
def test_requests_without_distinct_supported_anchors_do_not_expand_material(capacity_vault, other):
    # Available extra claims cannot fill slots earned only by names or unsupported questions.
    vault, _paths = capacity_vault
    _note(vault, "extra-claims", "\n".join(
        f"- [observation] Gauge calibration requires violet worksheet reading {i}. ^reading-{i}"
        for i in range(4)), project="amber-lab")
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    request = "In amber-lab, what guides gauge calibration? "
    conversation = None
    if other == "bare":
        request += "; ".join(key for key, _property, _value in TOPICS[1:])
    elif other == "focus":
        conversation = working_set.working_set_conversation.Conversation(
            focus="; ".join(key for key, _property, _value in TOPICS[1:]), state="applied")
    elif other == "unsupported":
        request += " ".join(f"In {key}, what about quartz tuning?" for key, *_ in TOPICS[1:])
    else:
        request *= 4
    packet = working_set.compile_packet(vault, turn=request, conversation=conversation)
    material = [item for item in packet["units"] + packet["pointers"] if item["role"] == "material"]
    assert len(material) == 3


def test_combined_roles_only_serve_extra_items_for_new_anchor_coverage():
    # One item covering several requests cannot leave unrelated spare slots in another role.
    coverage = ({"a", "b"}, {"a"}, {"c"}, {"b"}, {"d"})
    packet = working_set.build_packet(
        items=tuple(working_set.LaneItem(role="material" if i < 2 else "other_material",
            level="unit" if i < 3 else "page", ref=f"item-{i}", path=f"page-{i}",
            title=f"Page {i}", text="A useful claim.", lifecycle="active", updated="",
            anchor="", relevance_order=i, request_anchors=frozenset(covered))
            for i, covered in enumerate(coverage)),
        anchors=(), roles=tuple({"id": role, "lane": "material"}
            for role in ("material", "other_material")), current_state=(), ambiguity=(),
        missing=(), max_chars=4000, generation={}, status="resolved",
    )
    assert [item["ref"] for item in packet["units"]] == ["item-0", "item-1", "item-2"]
    assert [item["ref"] for item in packet["pointers"]] == ["item-4"]
    assert {"role": "other_material", "reason": "lane_truncated"} in packet["missing"]
    assert all("request_anchors" not in str(item) for item in packet.values())


def test_full_ambiguity_verdict_survives_bounded_display():
    # A menu prefix must not become a unique carry when the omitted sense competes.
    from exomem import working_set_conversation as conversation
    rows = [_row(f"a-{i:02}.md", "Copper Beacon", neighbourhood=("shared.md",))
        for i in range(24)] + [_row("z-rival.md", "Copper Beacon")]
    analysis = resolver.analyze_turn("Copper Beacon")
    drawn = resolver.candidates_for(analysis, rows)
    result = resolver.resolve(drawn, turn_tokens=analysis.tokens)
    packet = working_set.abstained_packet(reason=result.status, anchors=(),
        ambiguity=result.ambiguity, max_chars=4000, generation={})
    assert packet["abstention"] == {"reason": "ambiguous"}
    assert len(packet["ambiguity"]) == 24
    assert packet["missing"] == [{"role": "ambiguity", "reason": "lane_truncated"}]
    carry = conversation.carry(((conversation.Entry("user", "Copper Beacon"), analysis, drawn),))
    assert carry.status == "ambiguous"


def test_hidden_late_competitor_is_identical_to_an_absent_one(tmp_path: Path):
    # Full-set adjudication must still begin after audience filtering.
    from test_governance_egress import _external, write_rule, write_scope
    from exomem.governance.principal import request_scope

    packets = []
    for hidden in (False, True):
        root = tmp_path / str(hidden)
        _write(root, "Knowledge Base/Notes/visible.md",
            "---\ntitle: Copper Beacon\ntags: [hub]\nstatus: active\n---\n# Copper Beacon\n")
        if hidden:
            for i in range(25):
                _write(root, f"Knowledge Base/Notes/Patterns/hidden-{i}.md",
                    "---\ntitle: Copper Beacon\ntags: [hub]\nstatus: active\n---\n# Copper Beacon\n")
        write_scope(root)
        write_rule(root, ceiling=0)
        working_set_index.WorkingSetIndex(root).rebuild()
        with request_scope(_external()):
            packets.append(working_set.compile_packet(root, turn="Copper Beacon", freshness_key="k"))
    assert packets[0] == packets[1]
    assert not packets[0]["abstained"]
