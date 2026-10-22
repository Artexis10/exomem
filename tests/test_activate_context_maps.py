"""Maps activation uses the caller's view, its budget, and the shared view-item guard."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
from test_context_catalogue import _built, _write
from test_context_views import (
    OWNER,
    SECRET,
    THREAD_W,
    TIDE,
    _cold,
    _second_vault,
    _world,
)
from test_governance_egress import _external
from test_working_set_index import _seed_structure
from test_working_set_withheld_twins import _withhold

from exomem import commands, context_catalogue, context_views, project_keys, working_set_runtime
from exomem.governance import egress, principal
from exomem.writer_lease import invoke_command


def _activate(root: Path, caller=OWNER, **kwargs):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    with principal.request_scope(caller):
        return invoke_command(command, root, **kwargs)


def test_the_default_mode_is_unchanged(vault: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """An absent or invalid setting must preserve the explicit anchors packet and its token bytes."""
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
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "anchors")
    explicit = _activate(vault, **options)
    monkeypatch.setattr(working_set_runtime, "_PACKET_WARNING_EMITTED", False)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "invalid")
    invalid = _activate(vault, **options)
    assert absent == explicit == invalid
    assert _activate(vault, **options) == invalid
    assert sum("invalid EXOMEM_ACTIVATION_PACKET" in record.message for record in caplog.records) == 1
    assert "map" not in absent
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
        caller = context_views.CallerView(lambda _path: False, None, "empty-view")
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


def test_conversation_does_not_change_the_map(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _built(vault).close()
    first = _activate(vault, turn="")
    second = _activate(vault, turn="A different question", conversation={"focus": "Something else", "refs": [TIDE], "recent": [{"role": "user", "text": "Earlier work"}]}, trigger="task_refresh")
    assert first["map"] == second["map"]
    assert first["continuity"] == second["continuity"]


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


@pytest.mark.parametrize("forgery", ["path", "description", "state"])
def test_the_guard_drops_a_forged_map_item(vault: Path, monkeypatch: pytest.MonkeyPatch, forgery: str) -> None:
    """The map uses the read-view display replacement and the same strict path and state decisions."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _world(vault, with_w=True)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# Restricted fixture\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    _built(vault).close()
    packet = _activate(vault)
    if forgery == "path":
        forged = {"kind": "note", "path": SECRET, "ref": SECRET, "title": "Restricted fixture", "description": "Hidden."}
        packet["map"]["items"].append(forged)
    else:
        forged = next(item for item in packet["map"]["items"] if item.get("path"))
        if forgery == "description":
            forged["description"] = "Forged [[secret-cluster]] prose."
        else:
            forged["state"] = {"statement": "Forged", "path": SECRET, "as_of": "2026-10-01"}
    with principal.request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert SECRET not in json.dumps(guarded)
    assert "Forged" not in json.dumps(guarded)
    assert guarded["map"]["items"]
