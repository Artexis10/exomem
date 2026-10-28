"""Maps activation uses the caller's view, its budget, and the shared view-item guard."""

from __future__ import annotations

import base64
import copy
import json
import shutil
from pathlib import Path

import pytest
from test_context_catalogue import _built, _page, _write
from test_context_views import (
    OWNER,
    SECRET,
    THREAD_W,
    TIDE,
    _ask,
    _cold,
    _second_vault,
    _world,
)
from test_governance_egress import _external
from test_working_set_index import _seed_structure
from test_working_set_withheld_twins import _withhold

from exomem import (
    commands,
    context_catalogue,
    context_views,
    lexstore,
    project_keys,
    working_set_runtime,
)
from exomem.governance import egress, principal
from exomem.writer_lease import invoke_command


def _activate(root: Path, caller=OWNER, **kwargs):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    with principal.request_scope(caller):
        return invoke_command(command, root, **kwargs)


def test_the_default_mode_is_maps_and_anchors_is_the_rollback(vault: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """An absent or invalid setting serves maps; `anchors` restores the anchors packet and its token bytes."""
    _seed_structure(vault)
    _built(vault).close()
    # Freeze only caller-local token fields, so the complete packets can be compared.
    monkeypatch.setattr(working_set_runtime, "new_thread", lambda: ("fixture-thread", 1_800_000_000_000_000_000))
    encode = working_set_runtime.encode_continuity
    monkeypatch.setattr(working_set_runtime, "encode_continuity", lambda **kw: encode(**{**kw, "minted_ns": 1_800_000_000_000_000_000}))
    options = {"turn": "What are the Cargo Sled constraints?"}
    _activate(vault, **options)  # warm the published recall inputs before comparing modes
    monkeypatch.delenv("EXOMEM_ACTIVATION_PACKET", raising=False)
    absent = _activate(vault, **options)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    explicit = _activate(vault, **options)
    monkeypatch.setattr(working_set_runtime, "_PACKET_WARNING_EMITTED", False)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "invalid")
    invalid = _activate(vault, **options)
    assert absent == explicit == invalid
    assert _activate(vault, **options) == invalid
    assert sum("invalid EXOMEM_ACTIVATION_PACKET" in record.message for record in caplog.records) == 1
    assert absent["map"]["items"] and absent["anchors"] == []
    # The rollback: the one setting restores the anchors packet.
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "anchors")
    rollback = _activate(vault, **options)
    assert "map" not in rollback
    assert [anchor["ref"] for anchor in rollback["anchors"]] == ["Knowledge Base/Products/Cargo Sled.md"]
    # The old encoder's exact wire shape remains unchanged when map_generation is omitted.
    expected = '{"conventions_hash":"c","generation":2,"identity":"i","refs":["p"],"roles":["r"],"roles_hash":"h","v":1}'
    assert encode(identity="i", roles_hash="h", conventions_hash="c", generation=2, refs=["p"], roles=["r"]) == base64.urlsafe_b64encode(expected.encode()).decode().rstrip("=")


@pytest.mark.parametrize("mode", ["anchors", "maps"])
def test_the_kill_switch_disables_both_modes(vault: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", mode)
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")
    previous = working_set_runtime.encode_continuity(
        identity="earlier", roles_hash="h", conventions_hash="c", generation=1,
        refs=[TIDE], roles=[], thread="earlier-thread",
    )
    packet = _activate(vault, turn="Show the map", include_timings=True, continuity=previous)
    assert packet["abstention"] == {"reason": "disabled"}
    assert "map" not in packet
    if mode == "maps":
        assert "continuity" not in packet


@pytest.mark.parametrize("restricted", [False, True])
def test_an_empty_vault_returns_an_empty_map(vault: Path, monkeypatch: pytest.MonkeyPatch, restricted: bool) -> None:
    """An empty caller view is a ready empty map; a cold catalogue remains visibly warming."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # A configured empty registry has no synthetic project anchors.
    monkeypatch.setattr(project_keys, "load_project_registry", lambda _root: project_keys.ProjectRegistry({}, {}))
    if restricted:
        _world(vault, with_w=True)
        _built(vault).close()
        caller = context_views.CallerView(lambda _path: False, None, "empty-view", False, False)
        monkeypatch.setattr(context_views.CallerView, "current", lambda *_a, **_kw: caller)
    else:
        shutil.rmtree(vault / "Knowledge Base")
        _write(vault, "Knowledge Base/_Schema/project-keys.yaml", "projects: {}\n")
    packet = _activate(vault, turn="")
    assert packet["abstained"] is False
    assert packet["map"]["items"] == []
    assert packet["map"]["reason"] == "empty"
    monkeypatch.setattr(context_catalogue, "ensure_unlisted", lambda *_a, **_kw: ("index_warming", None, False))
    previous = working_set_runtime.encode_continuity(
        identity="earlier", roles_hash="h", conventions_hash="c", generation=1,
        refs=[TIDE], roles=[], thread="earlier-thread",
    )
    waiting = _activate(vault, turn="", continuity=previous)
    assert waiting["abstention"] == {"reason": "index_warming"}
    assert "map" not in waiting
    assert "continuity" not in waiting


def test_the_map_fits_the_budget(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whole items, including state, and the overflow pointer must share the request's prose cap."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _seed_structure(vault)
    _built(vault).close()
    before = {str(path.relative_to(vault)): path.read_bytes() for path in vault.rglob("*") if path.is_file()}

    def forbidden(*_a, **_kw):
        raise AssertionError("maps activation reached anchor resolution")

    monkeypatch.setattr(working_set_runtime, "lexical_evidence", forbidden)
    monkeypatch.setattr(working_set_runtime, "serve", forbidden)
    for budget in (500, 1000, 8000):
        packet = _activate(vault, turn="", max_chars=budget, include_timings=True)
        assert packet["abstained"] is False, packet.get("abstention")
        block = packet["map"]
        rendered = [block["description"], *(context_views.map_item_line(item) for item in block["items"]), block["overflow"]]
        assert packet["budget"] == {"limit_chars": budget, "used_chars": len("\n".join(rendered))}
        assert packet["budget"]["used_chars"] <= budget
        assert "Domain maps:" in block["overflow"]
        decoded = working_set_runtime.decode_continuity(packet["continuity"])
        assert decoded["map_generation"] == block["generation"] == packet["generation"]["map_generation"]
        assert decoded["refs"] == []
        assert {"working_set.catalogue", "working_set.map", "working_set.map_state"} <= packet["timings"]["stages"].keys()
    assert before == {str(path.relative_to(vault)): path.read_bytes() for path in vault.rglob("*") if path.is_file()}


@pytest.mark.parametrize("restricted", [False, True])
def test_a_pathless_map_anchor_opens_its_profile(
    vault: Path, monkeypatch: pytest.MonkeyPatch, restricted: bool,
) -> None:
    """A project in either map must lead to readable context instead of a dead-end identity ref."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=False)
    _page(vault, "tide-work", "project: harbour-ops\n", body="- [decision] Work at slack tide ^slack\n")
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    caller = _external() if restricted else OWNER
    packet = _activate(vault, caller, max_chars=8000)
    anchor = next(item for item in packet["map"]["items"] if item["ref"] == "project:harbour-ops")
    assert not anchor.get("path")
    assert anchor["profile"] == context_views.anchor_profile_ref(anchor["ref"])
    for ref in (context_views.MAP_REF, context_views.topic_ref("harbour")):
        items = _ask(vault, ref, caller)["items"]
        assert next(item for item in items if item["ref"] == anchor["ref"])["profile"] == anchor["profile"]
        assert all(item["profile"] == context_views.anchor_profile_ref(item["ref"])
                   for item in items if item["kind"] == "anchor")
    profile = _ask(vault, anchor["profile"], caller)
    assert profile["state"] == "ready"
    assert any("Work at slack tide" in item.get("text", "") for item in profile["items"])


def test_conversation_does_not_change_the_map(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _built(vault).close()
    first = _activate(vault, turn="")
    second = _activate(vault, turn="A different question", conversation={"focus": "Something else", "refs": [TIDE], "recent": [{"role": "user", "text": "Earlier work"}]}, trigger="task_refresh")
    assert first["map"] == second["map"]
    assert first["continuity"] == second["continuity"]
    assert first["generation"]["conversation"] == second["generation"]["conversation"] == "absent"


def test_a_withheld_entry_changes_no_byte_of_the_maps_packet(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P3's twin includes a hidden newest thread revision, topic member, and relation target."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    twin = _second_vault(tmp_path)
    for root, with_w in ((vault, True), (twin, False)):
        _world(root, with_w=with_w)
        _withhold(root, THREAD_W, ceiling=0)
        _built(root).close()
    owners, guests = [], []
    for root in (vault, twin):
        _cold()
        owners.append(_activate(root, max_chars=1000))  # fill the owner cache before a restricted read
        guests.append(_activate(root, _external(), max_chars=1000))
    assert json.dumps(guests[0], sort_keys=True) == json.dumps(guests[1], sort_keys=True)
    assert guests[0]["generation"] == guests[1]["generation"]
    assert working_set_runtime.decode_continuity(guests[0]["continuity"]) == working_set_runtime.decode_continuity(guests[1]["continuity"])
    assert owners[0]["map"] != owners[1]["map"]
    assert THREAD_W in json.dumps(owners[0])
    assert THREAD_W not in json.dumps(guests[0])


@pytest.mark.parametrize("forgery", [
    "path", "description", "state", "anchor", "activity", "lifecycle_class", "state.source",
    "topics", "superseded_by", "extra.path", "links[].path", "evidence", "profile",
])
def test_the_guard_drops_a_forged_map_item(vault: Path, monkeypatch: pytest.MonkeyPatch, forgery: str) -> None:
    """The map restores its caller's pinned items before the shared strict path and state decisions."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# Restricted fixture\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    _built(vault).close()
    with principal.request_scope(_external()), principal.invocation_scope():
        packet = _activate(vault, _external())
        original = copy.deepcopy(packet["map"])
        if forgery == "path":
            forged = {"kind": "note", "path": SECRET, "ref": SECRET, "title": "Restricted fixture", "description": "Hidden."}
            packet["map"]["items"].append(forged)
        else:
            forged = next(item for item in packet["map"]["items"] if item.get("profile" if forgery == "profile" else "path"))
            if forgery == "description":
                forged["description"] = "Forged [[secret-cluster]] prose."
            elif forgery == "state":
                forged["state"] = {"statement": "Forged", "path": SECRET, "as_of": "2026-10-01"}
            elif forgery == "state.source":
                forged["state"] = {**(forged.get("state") or {}), "source": SECRET}
            elif forgery == "profile":
                forged["profile"] = context_views.anchor_profile_ref(SECRET)
            elif forgery == "extra.path":
                forged["extra"] = {"path": SECRET}
            elif forgery == "links[].path":
                forged["links"] = [{"path": SECRET}]
            elif forgery in ("topics", "evidence"):
                forged[forgery] = [SECRET]
            else:
                forged[forgery] = SECRET
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert SECRET not in json.dumps(guarded)
    assert "Forged" not in json.dumps(guarded)
    assert guarded["map"]["items"]
    assert guarded["map"] == original


@pytest.mark.parametrize("field", ["overflow", "description", "ref", "reason"])
def test_the_guard_restores_the_compiled_map_block(vault: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Forging block prose or its ref must not change the compiled map sent to the caller."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _built(vault).close()
    with principal.request_scope(_external()), principal.invocation_scope():
        packet = _activate(vault, _external())
        original = copy.deepcopy(packet["map"])
        packet["map"][field] = "Forged block text."
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert guarded["map"] == original


@pytest.mark.parametrize("pointed_at", ["the block", "the block and the packet"])
def test_a_map_compiled_for_a_larger_packet_is_never_served(vault: Path, monkeypatch: pytest.MonkeyPatch, pointed_at: str) -> None:
    """A 500-character packet carries the map that the same request compiled for 8000 characters.

    The guard takes the block that the packet's own `map_generation` names, for the packet's own limit,
    so the larger map never reaches the wire: the packet abstains, as a map that cannot be read does.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _built(vault).close()
    with principal.request_scope(_external()), principal.invocation_scope():
        small = _activate(vault, _external(), max_chars=500)
        large = _activate(vault, _external(), max_chars=8000)
        assert len(large["map"]["items"]) > len(small["map"]["items"])  # the control: two different maps
        forged = copy.deepcopy(small)
        forged["map"] = copy.deepcopy(large["map"])
        if pointed_at == "the block and the packet":
            forged["generation"]["map_generation"] = large["map"]["generation"]
        guarded = egress.guard_working_set(vault, forged, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert (guarded["abstained"], guarded["abstention"], guarded["generation"]["map_generation"]) == (True, {"reason": "unavailable"}, None)
    assert "map" not in guarded


def test_a_map_that_another_request_compiled_is_never_served(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A packet replayed into a request that compiled no map abstains instead of serving an empty, ready map."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _built(vault).close()
    packet = _activate(vault, _external())
    assert packet["map"]["items"]
    with principal.request_scope(_external()), principal.invocation_scope():
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert (guarded["abstained"], guarded["abstention"], guarded["generation"]["map_generation"]) == (True, {"reason": "unavailable"}, None)
    assert "map" not in guarded


def test_notes_without_map_rows_still_point_to_the_unfiled_map(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A visible ordinary note must not be reported as an empty catalogue."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setattr(project_keys, "load_project_registry", lambda _root: project_keys.ProjectRegistry({}, {}))
    shutil.rmtree(vault / "Knowledge Base")
    _page(vault, "unfiled-note")
    _built(vault).close()
    packet = _activate(vault)
    assert packet["map"]["items"] == []
    assert "reason" not in packet["map"]
    assert context_views.UNFILED_REF in packet["map"]["overflow"]
    with principal.request_scope(OWNER):
        unfiled = context_views.read_view(vault, context_views.UNFILED_REF)
    assert len(unfiled["items"]) == 1


def test_equal_rendered_lines_do_not_select_an_extra_entry(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Copied pages can retain a ref and display; fitting one line must serve only that row."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setattr(project_keys, "load_project_registry", lambda _root: project_keys.ProjectRegistry({}, {}))
    shutil.rmtree(vault / "Knowledge Base")
    for name in ("copy-one", "copy-two"):
        _write(
            vault, f"Knowledge Base/Entities/People/{name}.md",
            "---\ntype: entity\nentity_type: person\nstatus: active\n"
            "exomem_id: 2fb2961f-0e12-4f5d-8372-a013bd7c921a\n"
            f"title: {'Copied profile ' * 12}\nsummary: {'Shared description. ' * 12}\n---\n\nBody.\n",
        )
    _built(vault).close()
    with principal.request_scope(OWNER):
        view = context_views.compile_view(vault, context_views.MAP_REF)
    lines = [context_views.map_item_line(context_views._map_item(view, row)) for row in view.rows]
    assert len(lines) == 2 and lines[0] == lines[1]
    _, overflow = context_views.map_within(view, 100_000)
    budget = len("Index of what exists.") + len(lines[0]) + len(overflow) + 2
    assert budget >= 500
    packet = _activate(vault, max_chars=budget)
    assert len(packet["map"]["items"]) == 1
    assert packet["map"]["overflow"].startswith("Not shown: 1.")
    assert packet["budget"]["used_chars"] <= budget


def test_a_topic_with_only_a_withheld_member_cannot_be_injected(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypassing the compiler cannot introduce an owner's topic into a guest's served map."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _withhold(vault, THREAD_W, ceiling=0)
    _built(vault).close()
    owner = _activate(vault)
    topic = next(item for item in owner["map"]["items"] if item["ref"] == context_views.topic_ref("fleet"))
    caller = _external("marketing")
    with principal.request_scope(caller), principal.invocation_scope():
        packet = _activate(vault, caller, purpose="marketing")
        original = copy.deepcopy(packet["map"])
        packet["map"]["items"].append(topic)
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]), purpose="marketing")
    assert guarded is not None
    assert guarded["map"] == original


def test_a_withheld_project_registry_edited_after_readiness_leaves_a_restricted_map_unchanged(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who may not read the project registry never loads it, so no edit of it reaches that caller.

    The edit lands after the catalogue is ready and is one that no loader can parse.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # Freeze only caller-local token fields, so the complete packets can be compared.
    monkeypatch.setattr(working_set_runtime, "new_thread", lambda: ("fixture-thread", 1_800_000_000_000_000_000))
    encode = working_set_runtime.encode_continuity
    monkeypatch.setattr(working_set_runtime, "encode_continuity", lambda **kw: encode(**{**kw, "minted_ns": 1_800_000_000_000_000_000}))
    _world(vault, with_w=False)
    registry = vault / project_keys.registry_relpath()
    _withhold(vault, project_keys.registry_relpath(), ceiling=0)
    _built(vault).close()
    before = _activate(vault, _external(), max_chars=1000)
    assert before["map"]["state"] == "ready" and before["map"]["items"]
    prepare = context_views._prepare

    def prepare_then_edit(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        assert prepared.state == context_views.READY and not prepared.caller.projects_admitted
        registry.write_text(registry.read_text(encoding="utf-8") + "\nunused: " + "[" * 1200 + "0" + "]" * 1200 + "\n")
        return prepared

    monkeypatch.setattr(context_views, "_prepare", prepare_then_edit)
    assert _activate(vault, _external(), max_chars=1000) == before


def test_each_activation_in_a_library_scope_keeps_its_record_only_while_it_runs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library scope spans many invocations. Each one is served from its own record, which ends with it."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=False)
    _built(vault).close()
    with principal.library_scope():
        smaller = _activate(vault, max_chars=7900)
        larger = _activate(vault, max_chars=8000)
        assert principal.invocation_served() is None
        for _ in range(20):
            assert _activate(vault, max_chars=8000)["map"]["state"] == "ready"
            assert principal.invocation_served() is None
    # The control: both packets select the same rows, so their maps share one generation.
    assert smaller["generation"]["map_generation"] == larger["generation"]["map_generation"]
    assert smaller["map"]["state"] == larger["map"]["state"] == "ready"
    assert smaller["map"]["items"] == larger["map"]["items"]


def test_two_maps_budgeted_in_one_invocation_are_both_served(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One invocation that compiles the same rows for two packet sizes keeps a record for each size."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=False)
    _built(vault).close()
    with principal.request_scope(_external()), principal.invocation_scope():
        smaller = _activate(vault, _external(), max_chars=7900)
        larger = _activate(vault, _external(), max_chars=8000)
        guarded = [egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[])) for packet in (smaller, larger)]
    assert smaller["generation"]["map_generation"] == larger["generation"]["map_generation"]
    assert smaller["map"]["state"] == larger["map"]["state"] == "ready"
    assert [packet["map"] for packet in guarded] == [smaller["map"], larger["map"]]
