"""Disclosed activation cases for a concrete, admitted conversation witness."""

from pathlib import Path

import pytest
from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_hot_projection import _live, _one_old_tick

from exomem import commands, lexstore, working_set, working_set_heat, working_set_index, working_set_runtime
from exomem.governance import egress
from exomem.governance.principal import owner_principal, request_scope

SUBJECT = "Knowledge Base/Notes/Insights/lattice-transport.md"
SECRET = "Knowledge Base/Notes/Restricted/restricted-dependency.md"
OTHER = "Knowledge Base/Notes/Insights/prism-routing.md"
SUPPORT = "Maintenance replacement costs twenty tokens."
CONVERSATION = {"recent": [{"role": "user", "text": "Tell me about Lattice transport."}]}


def _write(root: Path, path: str, body: str, title: str = "Lattice transport") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\ntype: insight\nstatus: active\ntags: [hub]\n---\n\n# {title}\n\n{body}\n")


def _ready(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    working_set_index.WorkingSetIndex(root).rebuild()
    _reset_caches()
    _one_old_tick(root)
    _live(root)
    lexstore.ensure_fresh(root)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))


def _activate(root: Path, turn: str = "What does it cost?", **kwargs) -> dict:
    with request_scope(_external()):
        return commands.op_activate_context(root, turn=turn, conversation=kwargs.pop("conversation", CONVERSATION), **kwargs)


def test_public_activation_retains_the_unit_that_licenses_a_title_missing_term(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^z-support")
    _ready(vault, monkeypatch)
    packet = _activate(vault, "What would the cost of that be for us?")
    assert packet["generation"].get("carried_by") == "conversation"
    assert not packet["abstained"]
    witness = next(unit for unit in packet["units"] if unit["ref"].endswith("#z-support"))
    assert witness["text"] == SUPPORT
    assert witness["provenance"]["path"] == SUBJECT
    assert packet["anchors"][0]["title"] == "Lattice transport"


@pytest.mark.parametrize("ceiling", [0, 1])
def test_withheld_support_including_notice_only_is_not_a_licence(vault, monkeypatch, ceiling):
    _write(vault, SECRET, "Private fixture.", "Restricted dependency")
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} See [[Restricted dependency]]. ^support")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=ceiling)
    _ready(vault, monkeypatch)
    withheld = _activate(vault)
    _write(vault, SUBJECT, "An ordinary introduction with no semantic unit.")
    _ready(vault, monkeypatch)
    absent = _activate(vault)
    for packet in (withheld, absent):
        assert packet["generation"].get("carried_by") != "conversation"
        assert packet["anchors"] == []
        assert packet["units"] == []
        assert packet["abstention"]["reason"] == "unresolved"



def test_a_withheld_subject_in_the_conversation_answers_as_an_absent_one(vault, monkeypatch):
    conversation = {
        "recent": [{"role": "user", "text": "Compare Lattice transport with Restricted dependency."}],
        "refs": [SUBJECT, SECRET],
    }
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^support")
    _write(vault, SECRET, f"- [fact] {SUPPORT} ^support", "Restricted dependency")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=0)
    _ready(vault, monkeypatch)
    withheld = _activate(vault, conversation=conversation)
    (vault / SECRET).unlink()
    _ready(vault, monkeypatch)
    absent = _activate(vault, conversation=conversation)
    for packet in (withheld, absent):
        packet.pop("continuity", None)
        packet.pop("timings", None)
        assert packet["generation"].get("carried_by") == "conversation"
        assert packet["anchors"][0]["title"] == "Lattice transport"
    assert withheld == absent

def test_hidden_units_do_not_spend_the_admitted_support_bound(vault, monkeypatch):
    _write(vault, SECRET, "Private fixture.", "Restricted dependency")
    hidden = "\n".join(f"- [fact] Entry {i} cites [[Restricted dependency]]. ^a-{i:03}" for i in range(201))
    _write(vault, SUBJECT, hidden + f"\n- [fact] {SUPPORT} ^z-support")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=0)
    _ready(vault, monkeypatch)
    with request_scope(owner_principal(surface="library")):
        assert len(lexstore.get_store(vault).units_of([SUBJECT])) == 202
    packet = _activate(vault)
    assert packet["generation"].get("carried_by") == "conversation"
    assert any(unit["text"] == SUPPORT and unit["ref"].endswith("#z-support") for unit in packet["units"])
    # The owner admits those 201 units: late support is outside their bound.
    with request_scope(owner_principal(surface="library")):
        owner = commands.op_activate_context(vault, turn="What does it cost?", conversation=CONVERSATION)
    assert owner["generation"].get("carried_by") != "conversation"


@pytest.mark.parametrize("turn,body", [
    ("What does it cost?", "- [fact] Maintenance is scheduled. ^maintenance"),
    ("Does it require uranium?", f"- [fact] {SUPPORT} ^cost"),
    ("What is its maintenance cost?", "- [fact] Maintenance is scheduled. ^maintenance\n- [fact] Costs are low. ^cost"),
])
def test_unlicensed_content_and_split_units_fall_through(vault, monkeypatch, turn, body):
    _write(vault, SUBJECT, body)
    _ready(vault, monkeypatch)
    packet = _activate(vault, turn)
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet.get("abstention", {}).get("reason") != "unavailable"


@pytest.mark.parametrize("other_supports", [False, True])
def test_every_ambiguous_candidate_needs_its_own_support(vault, monkeypatch, other_supports):
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^cost")
    _write(vault, OTHER, "- [fact] " + (SUPPORT if other_supports else "Maintenance is scheduled.") + " ^cost", "Prism routing")
    _ready(vault, monkeypatch)
    packet = _activate(vault, conversation={"recent": [{"role": "user", "text": "Lattice transport and Prism routing"}]})
    assert packet["abstained"]
    assert packet["units"] == []
    assert packet["anchors"] == []
    if other_supports:
        assert packet["abstention"]["reason"] == "ambiguous"
        assert {item["title"] for item in packet["ambiguity"]} == {"Lattice transport", "Prism routing"}
    else:
        assert packet["abstention"]["reason"] == "unresolved"
        assert packet["ambiguity"] == []


def test_unaffordable_witness_cannot_license_an_inferred_packet(vault, monkeypatch):
    _write(vault, SUBJECT, "- [fact] " + SUPPORT + " Supporting explanation." * 20 + " ^support")
    _ready(vault, monkeypatch)
    packet = _activate(vault, max_chars=500)
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["anchors"] == []


def test_newest_subject_does_not_retry_an_older_supported_subject(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^cost")
    _write(vault, OTHER, "- [fact] Maintenance is scheduled. ^maintenance", "Prism routing")
    _ready(vault, monkeypatch)
    packet = _activate(vault, conversation={"recent": [
        {"role": "user", "text": "Lattice transport"},
        {"role": "user", "text": "Prism routing"},
    ]})
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["anchors"] == []


def test_source_edit_revokes_the_previous_request_witness(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^support")
    _ready(vault, monkeypatch)
    assert _activate(vault)["generation"].get("carried_by") == "conversation"
    _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^support")
    packet = _activate(vault)
    assert packet["generation"].get("carried_by") != "conversation"
    assert all(SUPPORT != unit["text"] for unit in packet["units"])


@pytest.mark.parametrize("changed", ["source", "language_registry", "relation_registry"])
def test_changes_between_query_pages_revoke_the_supporting_witness(vault, monkeypatch, changed):
    import yaml

    from exomem import relation_registry, semantic_language_registry

    _write(vault, SECRET, "Private fixture.", "Restricted dependency")
    hidden = "\n".join(f"- [fact] Entry {i} cites [[Restricted dependency]]. ^a-{i:03}" for i in range(201))
    _write(vault, SUBJECT, hidden + f"\n- [fact] {SUPPORT} ^z-support")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=0)
    _ready(vault, monkeypatch)
    search = lexstore.LexicalStore.search_semantic_units_result

    def change_before_hydration(store, *args, **kwargs):
        result = search(store, *args, **kwargs)
        if kwargs.get("after_unit_ref") and result.value:
            if changed == "source":
                _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^z-support")
            else:
                path = (
                    semantic_language_registry.registry_path(vault)
                    if changed == "language_registry"
                    else relation_registry.extension_registry_path(vault)
                )
                data = yaml.safe_load(path.read_text())
                if changed == "language_registry":
                    data.setdefault("categories", {})["maintenance_cost"] = {"description": "Maintenance costs"}
                else:
                    data.setdefault("extensions", {})["maintenance.costs"] = {
                        "parent": "supports", "description": "Records maintenance support",
                    }
                path.write_text(yaml.safe_dump(data))
        return result

    monkeypatch.setattr(lexstore.LexicalStore, "search_semantic_units_result", change_before_hydration)
    packet = _activate(vault)
    assert packet["generation"].get("carried_by") != "conversation"
    assert all(SUPPORT != unit["text"] for unit in packet["units"])


def test_terminal_disclosure_revokes_the_conversation_claim_when_its_witness_is_removed(vault, monkeypatch):
    _write(vault, SECRET, "Public fixture initially.", "Restricted dependency")
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} See [[Restricted dependency]]. ^support")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=6)
    _ready(vault, monkeypatch)
    guard = egress.guard_working_set

    def revoke_before_disclosure(root, packet, release, **kwargs):
        assert packet["generation"].get("carried_by") == "conversation"
        write_rule(root, ceiling=0)
        _reset_caches()
        return guard(root, packet, release, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", revoke_before_disclosure)
    packet = _activate(vault)
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["abstained"]
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["anchors"] == []
    assert packet["units"] == []


def test_new_authority_revokes_support_on_the_next_request(vault, monkeypatch):
    _write(vault, SECRET, "Public fixture initially.", "Restricted dependency")
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} See [[Restricted dependency]]. ^support")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=6)
    _ready(vault, monkeypatch)
    assert _activate(vault)["generation"].get("carried_by") == "conversation"
    write_rule(vault, ceiling=0)
    packet = _activate(vault)
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["units"] == []


def test_local_material_veto_and_explicit_subject_keep_their_precedence(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^support")
    _write(vault, OTHER, "- [fact] A separate route. ^route", "Prism routing")
    _ready(vault, monkeypatch)
    local = _activate(vault, 'Here is a quote: "replacement costs twenty tokens". What does it cost?')
    assert local["generation"].get("carried_by") != "conversation"
    explicit = _activate(vault, "Tell me about Prism routing")
    assert explicit["generation"].get("carried_by") != "conversation"
    assert explicit["anchors"][0]["title"] == "Prism routing"


def test_an_unselected_category_owner_cannot_be_bypassed_through_material(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [contact] {SUPPORT} ^support")
    _ready(vault, monkeypatch)
    packet = _activate(vault)
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet.get("abstention", {}).get("reason") != "unavailable"


def test_an_unknown_category_cannot_establish_a_witness_role(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [unregistered-fixture-category] {SUPPORT} ^support")
    _ready(vault, monkeypatch)
    packet = _activate(vault)
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet.get("abstention", {}).get("reason") != "unavailable"


def test_hidden_graph_neighbours_do_not_displace_the_public_supporting_unit(vault, monkeypatch):
    from exomem import epistemic_graph

    witness_path = "Knowledge Base/Notes/Insights/zinc-documentation.md"
    _write(vault, witness_path, f"- [fact] {SUPPORT} ^support", "Zinc documentation")
    hidden_paths = []
    for index in range(48):
        path = f"Knowledge Base/Notes/Restricted/dependency-{index:03}.md"
        _write(vault, path, "A private dependency.", f"Dependency {index:03}")
        hidden_paths.append(path)
    links = "\n".join(f"- supports [[{path.removesuffix('.md')}]]" for path in hidden_paths)
    _write(vault, SUBJECT, "A public introduction.\n\n## Relations\n" + links
           + f"\n- supports [[{witness_path.removesuffix('.md')}]]")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=0)
    _ready(vault, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    hidden = _activate(vault)
    for path in hidden_paths:
        (vault / path).unlink()
    _ready(vault, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    absent = _activate(vault)
    for packet in (hidden, absent):
        assert packet["generation"].get("carried_by") == "conversation"
        assert any(unit["text"] == SUPPORT and unit["provenance"]["path"] == witness_path
                   for unit in packet["units"])
    assert hidden["anchors"] == absent["anchors"]
    assert hidden["units"] == absent["units"]


def test_earlier_parent_witness_edit_during_a_later_graph_page_revokes_support(vault, monkeypatch):
    from exomem import epistemic_graph

    later = "Knowledge Base/Notes/Insights/zinc-documentation.md"
    _write(vault, SECRET, "Private fixture.", "Restricted dependency")
    _write(vault, later, "- [fact] Separate transport context. ^last", "Zinc documentation")
    hidden = "\n".join(f"- [fact] Entry {i} cites [[Restricted dependency]]. ^a-{i:03}" for i in range(1, 200))
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^a-000\n" + hidden
           + f"\n\n## Relations\n- supports [[{later.removesuffix('.md')}]]")
    write_scope(vault, paths="Notes/Restricted/**")
    write_rule(vault, ceiling=0)
    _ready(vault, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    search = lexstore.LexicalStore.search_semantic_units_result
    changed = False

    def edit_earlier_parent(store, *args, **kwargs):
        nonlocal changed
        result = search(store, *args, **kwargs)
        if kwargs.get("after_unit_ref") and result.value and all(hit.parent_path == later for hit in result.value):
            _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^a-000")
            changed = True
        return result

    monkeypatch.setattr(lexstore.LexicalStore, "search_semantic_units_result", edit_earlier_parent)
    packet = _activate(vault)
    assert changed
    assert packet["generation"].get("carried_by") != "conversation"
    assert all(SUPPORT != unit["text"] for unit in packet["units"])


@pytest.mark.parametrize("changed", ["source", "selected_definitions"])
def test_a_witness_that_changes_after_support_returns_cannot_license_a_carry(vault, monkeypatch, changed):
    import yaml

    from exomem import semantic_language_registry

    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^support")
    _write(vault, OTHER, f"- [fact] {SUPPORT} ^support", "Prism routing")
    semantic_language_registry.registry_path(vault).write_text(yaml.safe_dump(semantic_language_registry.empty_proposal()))
    _ready(vault, monkeypatch)
    support = working_set._conversation_support
    guard = egress.guard_working_set

    def edit_after_support(*args, **kwargs):
        result = support(*args, **kwargs)
        assert result[0] and result[1] is not None
        if changed == "source":
            _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^support")
        else:
            path = semantic_language_registry.registry_path(vault)
            data = yaml.safe_load(path.read_text())
            data.setdefault("categories", {})["maintenance_cost"] = {"description": "Maintenance costs"}
            path.write_text(yaml.safe_dump(data))
        return result

    def check_compiler_output(root, packet, release, **kwargs):
        assert packet["generation"].get("carried_by") != "conversation"
        assert all(SUPPORT != unit["text"] for unit in packet["units"])
        return guard(root, packet, release, **kwargs)

    monkeypatch.setattr(working_set, "_conversation_support", edit_after_support)
    monkeypatch.setattr(egress, "guard_working_set", check_compiler_output)
    packet = _activate(vault, conversation={"recent": [
        {"role": "user", "text": "Prism routing"},
        {"role": "user", "text": "Lattice transport"},
    ]})
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["anchors"] == []
    assert all(SUPPORT != unit["text"] for unit in packet["units"])


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("changed", ["source", "selected_definitions"])
def test_witness_change_after_terminal_projection_abstains_and_keeps_recent_context(vault, monkeypatch, owner, changed):
    import yaml

    from exomem import semantic_language_registry

    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^support")
    _write(vault, OTHER, "Unrelated recent work.", "Prism routing")
    _write(vault, SECRET, "Private recent work.", "Restricted dependency")
    if not owner:
        write_scope(vault, paths="Notes/Restricted/**")
        write_rule(vault, ceiling=0)
    semantic_language_registry.registry_path(vault).write_text(yaml.safe_dump(semantic_language_registry.empty_proposal()))
    _ready(vault, monkeypatch)
    recent = {"ref": OTHER, "title": "Prism routing", "statement": "Unrelated recent work."}
    private = {"ref": SECRET, "title": "Restricted dependency", "statement": "Private recent work."}
    monkeypatch.setattr(working_set, "_recent_context", lambda *args, **kwargs: (recent, private))
    release_packet = egress._packet_release
    projected = []

    def edit_after_projection(root, packet, *args, **kwargs):
        ctx = release_packet(root, packet, *args, **kwargs)
        if packet.get("generation", {}).get("carried_by") == "conversation":
            projected.append(ctx is None)
            if changed == "source":
                _write(root, SUBJECT, "- [fact] Maintenance is scheduled. ^support")
            else:
                path = semantic_language_registry.registry_path(root)
                data = yaml.safe_load(path.read_text())
                data.setdefault("categories", {})["maintenance_cost"] = {"description": "Maintenance costs"}
                path.write_text(yaml.safe_dump(data))
        return ctx

    monkeypatch.setattr(egress, "_packet_release", edit_after_projection)
    with request_scope(owner_principal(surface="library") if owner else _external()):
        packet = commands.op_activate_context(vault, turn="What does it cost?", conversation=CONVERSATION)
    assert projected == [owner]
    assert packet["abstained"]
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["generation"].get("carried_by") != "conversation"
    for section in ("anchors", "ambiguity", "units", "pointers", "current_state", "roles"):
        assert packet[section] == []
    assert packet["recent_context"] == ([recent, private] if owner else [recent])
    assert packet["budget"]["used_chars"] == sum(
        len(entry["title"]) + len(entry["statement"]) for entry in packet["recent_context"]
    )


def test_terminal_projection_revokes_ambiguity_when_one_subject_witness_expires(vault, monkeypatch):
    _write(vault, SUBJECT, f"- [fact] {SUPPORT} ^support")
    _write(vault, OTHER, f"- [fact] {SUPPORT} ^support", "Prism routing")
    _ready(vault, monkeypatch)
    release_packet = egress._packet_release
    changed = False

    def edit_after_projection(root, packet, *args, **kwargs):
        nonlocal changed
        ctx = release_packet(root, packet, *args, **kwargs)
        if packet.get("abstention", {}).get("reason") == "ambiguous":
            assert {entry["title"] for entry in packet["ambiguity"]} == {"Lattice transport", "Prism routing"}
            _write(root, SUBJECT, "- [fact] Maintenance is scheduled. ^support")
            changed = True
        return ctx

    monkeypatch.setattr(egress, "_packet_release", edit_after_projection)
    packet = _activate(vault, conversation={"recent": [{"role": "user", "text": "Lattice transport and Prism routing"}]})
    assert changed
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["ambiguity"] == []
    assert packet["anchors"] == []
    assert packet["units"] == []
    assert packet["budget"]["used_chars"] == sum(
        len(entry.get("title", "")) + len(entry.get("statement", ""))
        for entry in packet["recent_context"]
    )


def test_title_only_carry_has_no_unit_currency_dependency(vault, monkeypatch):
    _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^support")
    _ready(vault, monkeypatch)
    release_packet = egress._packet_release
    changed = False

    def edit_after_projection(root, packet, *args, **kwargs):
        nonlocal changed
        ctx = release_packet(root, packet, *args, **kwargs)
        if packet.get("generation", {}).get("carried_by") == "conversation":
            _write(root, SUBJECT, "- [fact] Maintenance is complete. ^support")
            changed = True
        return ctx

    monkeypatch.setattr(egress, "_packet_release", edit_after_projection)
    packet = _activate(vault, "What about the next one?")
    assert changed
    assert packet["generation"].get("carried_by") == "conversation"
    assert packet["anchors"][0]["title"] == "Lattice transport"


@pytest.mark.parametrize("phase,owner", [
    ("before_support", False), ("after_support", False), ("terminal", False), ("terminal", True),
])
def test_authored_title_currency_controls_a_title_only_carry(vault, monkeypatch, phase, owner):
    title = "Lattice transport cost"
    _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^support", title)
    for index in range(5):
        _write(vault, f"Knowledge Base/Notes/Insights/transport-cost-{index}.md",
               "- [fact] An unrelated topic. ^topic", f"Other transport topic {index} cost")
    if not owner:
        write_scope(vault, paths="Notes/Restricted/**")
        write_rule(vault, ceiling=0)
    conversation = {"recent": [{"role": "user", "text": "Tell me about " + title + "."}]}
    _ready(vault, monkeypatch)
    with request_scope(owner_principal(surface="library") if owner else _external()):
        unchanged = commands.op_activate_context(vault, turn="What does it cost?", conversation=conversation)
    assert unchanged["generation"].get("carried_by") == "conversation"
    assert unchanged["anchors"][0]["title"] == title
    _ready(vault, monkeypatch)
    target = vault / SUBJECT
    before = target.read_text()
    changed = False
    support = working_set._conversation_support
    release_packet = egress._packet_release

    def edit_title():
        nonlocal changed
        target.write_text(before.replace("# " + title, "# Lattice transport schedule", 1))
        changed = True

    def edit_at_support(*args, **kwargs):
        if phase == "before_support":
            edit_title()
        result = support(*args, **kwargs)
        if phase == "after_support":
            assert result[0] and result[1] is None
            edit_title()
        return result

    def edit_after_projection(root, packet, *args, **kwargs):
        ctx = release_packet(root, packet, *args, **kwargs)
        if phase == "terminal" and packet.get("generation", {}).get("carried_by") == "conversation":
            assert (ctx is None) == owner
            edit_title()
        return ctx

    monkeypatch.setattr(working_set, "_conversation_support", edit_at_support)
    monkeypatch.setattr(egress, "_packet_release", edit_after_projection)
    with request_scope(owner_principal(surface="library") if owner else _external()):
        packet = commands.op_activate_context(vault, turn="What does it cost?", conversation=conversation)
    assert changed
    assert target.read_text() == before.replace("# " + title, "# Lattice transport schedule", 1)
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["anchors"] == []
    assert packet["ambiguity"] == []
    assert packet["units"] == []


@pytest.mark.parametrize("phase", ["after_support", "terminal"])
def test_a_neighbour_unit_cannot_reuse_a_changed_subject_title(vault, monkeypatch, phase):
    from exomem import epistemic_graph

    neighbour = "Knowledge Base/Notes/Insights/zinc-documentation.md"
    title = "Lattice maintenance transport"
    _write(vault, SUBJECT, f"A public introduction.\n\n## Relations\n- supports [[{neighbour.removesuffix('.md')}]]", title)
    _write(vault, neighbour, "- [fact] Replacement costs twenty tokens. ^support", "Zinc documentation")
    for index in range(5):
        _write(vault, f"Knowledge Base/Notes/Insights/maintenance-topic-{index}.md",
               "A separate subject.", f"Other maintenance topic {index}")
    conversation = {"recent": [{"role": "user", "text": "Tell me about " + title + "."}]}
    _ready(vault, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    unchanged = _activate(vault, "What is its maintenance cost?", conversation=conversation)
    assert unchanged["generation"].get("carried_by") == "conversation"
    assert any(unit["provenance"]["path"] == neighbour for unit in unchanged["units"])
    _ready(vault, monkeypatch)
    target = vault / SUBJECT
    before = target.read_text()
    neighbour_before = (vault / neighbour).read_text()
    support = working_set._conversation_support
    release_packet = egress._packet_release
    changed = False

    def edit_title():
        nonlocal changed
        target.write_text(before.replace("# " + title, "# Lattice scheduled transport", 1))
        changed = True

    def edit_after_support(*args, **kwargs):
        result = support(*args, **kwargs)
        if phase == "after_support":
            assert result[0] and result[1].path == neighbour
            edit_title()
        return result

    def edit_after_projection(root, packet, *args, **kwargs):
        ctx = release_packet(root, packet, *args, **kwargs)
        if phase == "terminal" and packet.get("generation", {}).get("carried_by") == "conversation":
            edit_title()
        return ctx

    monkeypatch.setattr(working_set, "_conversation_support", edit_after_support)
    monkeypatch.setattr(egress, "_packet_release", edit_after_projection)
    packet = _activate(vault, "What is its maintenance cost?", conversation=conversation)
    assert changed
    assert target.read_text() == before.replace("# " + title, "# Lattice scheduled transport", 1)
    assert (vault / neighbour).read_text() == neighbour_before
    assert packet["generation"].get("carried_by") != "conversation"
    assert packet["anchors"] == []
    assert packet["units"] == []


def test_title_currency_uses_the_declared_purpose_of_an_identity_grant(vault, monkeypatch):
    from test_governance_egress import GRANT_ID, SCOPE_ID

    title = "Lattice transport cost"
    _write(vault, SUBJECT, "- [fact] Maintenance is scheduled. ^support", title)
    for index in range(5):
        _write(vault, f"Knowledge Base/Notes/Insights/transport-cost-{index}.md",
               "- [fact] An unrelated topic. ^topic", f"Other transport topic {index} cost")
    write_scope(vault, paths="Notes/Insights/lattice-transport.md", default_deny=True)
    write_rule(vault, ceiling=0, extra="kind: org_cap\npurpose: marketing\npurpose_condition: matches\n")
    grant = vault / "Knowledge Base/_Governance/grants/transport.yaml"
    grant.parent.mkdir(parents=True, exist_ok=True)
    grant.write_text(
        f"governance_version: 1\nid: {GRANT_ID}\nkind: standing\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 6\n'
    )
    conversation = {"recent": [{"role": "user", "text": "Tell me about " + title + "."}]}
    _ready(vault, monkeypatch)
    with request_scope(_external(purpose="marketing")):
        denied = commands.op_activate_context(vault, turn="What does it cost?", conversation=conversation)
        allowed = commands.op_activate_context(
            vault, turn="What does it cost?", conversation=conversation, purpose="audit",
        )
    assert denied["generation"].get("carried_by") != "conversation"
    assert denied["anchors"] == []
    assert allowed["generation"].get("carried_by") == "conversation"
    assert allowed["anchors"][0]["title"] == title


def test_terminal_projection_revokes_a_title_only_ambiguity(vault, monkeypatch):
    _write(vault, SUBJECT, "An ordinary introduction.", "Lattice transport cost")
    _write(vault, OTHER, "An ordinary introduction.", "Prism routing cost")
    for index in range(5):
        _write(vault, f"Knowledge Base/Notes/Insights/cost-topic-{index}.md",
               "An unrelated topic.", f"Other cost topic {index}")
    conversation = {"recent": [{"role": "user", "text": "Compare Lattice transport cost and Prism routing cost."}]}
    _ready(vault, monkeypatch)
    unchanged = _activate(vault, conversation=conversation)
    assert unchanged["abstention"]["reason"] == "ambiguous"
    assert {entry["title"] for entry in unchanged["ambiguity"]} == {"Lattice transport cost", "Prism routing cost"}
    release_packet = egress._packet_release
    changed = False

    def edit_after_projection(root, packet, *args, **kwargs):
        nonlocal changed
        ctx = release_packet(root, packet, *args, **kwargs)
        if packet.get("abstention", {}).get("reason") == "ambiguous":
            _write(root, SUBJECT, "An ordinary introduction.", "Lattice transport schedule")
            changed = True
        return ctx

    monkeypatch.setattr(egress, "_packet_release", edit_after_projection)
    packet = _activate(vault, conversation=conversation)
    assert changed
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["ambiguity"] == []
    assert packet["anchors"] == []
    assert packet["units"] == []
