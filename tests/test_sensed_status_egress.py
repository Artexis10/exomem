"""The point-of-use status on read and activation, and its per-caller egress twins.

Under a governed policy, a restricted principal is served the sensed items it
may see, and a withheld page is indistinguishable from an absent one in every
sensed field. Each twin compares a vault where a page is withheld with a vault
where it never existed.
"""

from __future__ import annotations

import json
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf
from test_governance_egress import SCOPE_ID, _external, write_rule

from exomem import commands, dreamer, freshness, sensed_model, sensor_worker
from exomem.governance import egress, membership, policy
from exomem.governance.principal import owner_principal, request_scope
from exomem.writer_lease import invoke_command


def _reset_governance() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    sensed_model._VECTORS.clear()
    _reset_governance()
    yield
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    freshness.clear()
    sensed_model._VECTORS.clear()
    _reset_governance()


def _govern(vault: Path, withheld: str) -> None:
    target = vault / "Knowledge Base" / "_Governance" / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\n"
        f'paths: ["{withheld.removeprefix("Knowledge Base/")}"]\n',
        encoding="utf-8",
    )
    write_rule(vault, ceiling=0)
    _reset_governance()


def _read(vault: Path, path: str) -> dict:
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "read_memory")
    return invoke_command(command, vault, path=path)


def _status_as(vault: Path, path: str, principal) -> str:
    with request_scope(principal):
        status = sensed_model.status_for(vault, path)
    return json.dumps(status, sort_keys=True)


def _pair(root: Path, name: str, *, build, withheld: str, instrument) -> tuple[Path, Path]:
    """A vault with `withheld` withheld, and its twin where it never existed.

    Built one at a time: the freshness registry follows one vault per process.
    """
    vaults = []
    for label, with_page in (("real", True), ("twin", False)):
        freshness.clear()
        vault = build(root / name / label, with_page=with_page)
        sf.converge(vault, instrument)
        _govern(vault, withheld)
        vaults.append(vault)
    return vaults[0], vaults[1]


def _seal_vault(*, drop: str):
    def build(root: Path, *, with_page: bool) -> Path:
        return sf.build(
            root,
            with_denial=with_page or drop != "denial",
            with_winter=with_page or drop != "winter",
        )

    return build


def test_read_memory_carries_the_status_and_changes_nothing_else(
    tmp_path: Path, monkeypatch
) -> None:
    vault = sf.build(tmp_path)
    before = _read(vault, sf.SEAL)
    assert "epistemic_status" not in before
    sf.enable(monkeypatch)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    after = _read(vault, sf.SEAL)
    status = after.pop("epistemic_status")
    assert status["line"] == "refined by 1 later note; 1 open contradiction"
    assert after == before, "the body and every other field are unchanged"
    assert "epistemic_status" not in _read(vault, sf.INLET)


def test_a_withheld_contradicting_page_is_absent(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    stub = sf.StubInstrument(sf.default_table())
    real, twin = _pair(tmp_path, "denial", build=_seal_vault(drop="denial"),
                       withheld=sf.DENIAL, instrument=stub)
    withheld = _status_as(real, sf.SEAL, _external())
    assert withheld == _status_as(twin, sf.SEAL, _external())
    assert json.loads(withheld)["line"] == "refined by 1 later note"
    # Withheld, not absent: the owner still sees it.
    assert json.loads(_status_as(real, sf.SEAL, owner_principal()))["open_contradictions"] == 1
    with request_scope(_external()):
        real_read = _read(real, sf.SEAL)
        twin_read = _read(twin, sf.SEAL)
    assert real_read.get("epistemic_status") == twin_read.get("epistemic_status")
    assert set(real_read) == set(twin_read)


def test_a_withheld_refining_page_is_absent(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    stub = sf.StubInstrument(sf.default_table())
    real, twin = _pair(tmp_path, "winter", build=_seal_vault(drop="winter"),
                       withheld=sf.WINTER, instrument=stub)
    withheld = _status_as(real, sf.SEAL, _external())
    assert withheld == _status_as(twin, sf.SEAL, _external())
    status = json.loads(withheld)
    assert status["line"] == "1 open contradiction" and "chain" not in status
    assert json.loads(_status_as(real, sf.SEAL, owner_principal()))["refined_by_later"] == 1


def _bridge_vault(root: Path, *, with_page: bool) -> Path:
    """A contradicts B, B contradicts W (withheld), W contradicts C; all linked in a line."""
    vault = root / "vault"
    pages = [("a", "2026-01-01", "The valve is open."), ("b", "2026-02-01", "The valve is shut.")]
    if with_page:
        pages.append(("w", "2026-03-01", "The valve is wide open."))
    pages.append(("c", "2026-04-01", "The valve is sealed shut."))
    names = [name for name, _d, _t in pages]
    for index, (name, date, text) in enumerate(pages):
        links = " ".join(
            f"[[Notes/Insights/{other}]]" for other in names[max(0, index - 1): index + 2] if other != name
        )
        fx.write(vault, f"{sf.KB}/Notes/Insights/{name}.md", sf.note(name.upper(), date, text, links=links))
    fx.seed(vault)
    fx.publish_graph(vault)
    return vault


def test_a_withheld_page_breaks_a_contradiction_component(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    table = {
        ("The valve is open.", "The valve is shut."): "contradicts",
        ("The valve is shut.", "The valve is wide open."): "contradicts",
        ("The valve is wide open.", "The valve is sealed shut."): "contradicts",
    }
    stub = sf.StubInstrument(table)
    withheld_path = f"{sf.KB}/Notes/Insights/w.md"
    real, twin = _pair(tmp_path, "bridge", build=_bridge_vault, withheld=withheld_path,
                       instrument=stub)
    a = f"{sf.KB}/Notes/Insights/a.md"
    withheld = _status_as(real, a, _external())
    assert withheld == _status_as(twin, a, _external())
    assert json.loads(withheld)["contradiction_component"] == 2
    assert json.loads(_status_as(real, a, owner_principal()))["contradiction_component"] == 4


def test_activation_attaches_the_status_to_activated_anchors_only(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    packet = {
        "anchors": [
            {"ref": sf.SEAL, "path": sf.SEAL, "status": "resolved", "kind": "page"},
            {"ref": sf.DENIAL, "path": sf.DENIAL, "status": "partial", "kind": "page"},
        ],
        "budget": {"limit_chars": 4000, "used_chars": 10},
        "abstained": False,
    }
    sensed_model.for_packet(vault, packet)
    status = packet["anchors"][0]["epistemic_status"]
    assert status == {
        "line": "refined by 1 later note; 1 open contradiction",
        "refined_by_later": 1,
        "open_contradictions": 1,
        "evidence_complete": True,
    }
    assert "epistemic_status" not in packet["anchors"][1]
    assert packet["budget"]["used_chars"] == 10 + len(status["line"])
    assert [anchor["path"] for anchor in packet["anchors"]] == [sf.SEAL, sf.DENIAL]


def test_activation_is_unchanged_with_sensing_off(tmp_path: Path, monkeypatch) -> None:
    vault = sf.build(tmp_path)
    packet = {
        "anchors": [{"ref": sf.SEAL, "path": sf.SEAL, "status": "resolved"}],
        "budget": {"used_chars": 0},
        "abstained": False,
    }
    before = json.dumps(packet, sort_keys=True)
    sensed_model.for_packet(vault, packet)
    assert json.dumps(packet, sort_keys=True) == before
    live = commands.op_activate_context(vault, turn="what about seal wear")
    assert all("epistemic_status" not in anchor for anchor in live.get("anchors") or [])


def test_a_withheld_page_cannot_decide_a_visible_pages_selection(
    tmp_path: Path, monkeypatch
) -> None:
    """The slice-1 HIGH 1 twin, served per caller. A withheld aside linking a
    visible hub must not cost a restricted caller the hub's refinement: under
    the per-page cap it pushed the hub to 132 candidates and the refining pair
    out of selection, so the real vault served nothing where the twin served it."""
    sf.enable(monkeypatch)
    real, twin = _pair(tmp_path, "hub", build=sf.hub_vault, withheld=sf.ASIDE,
                       instrument=sf.StubInstrument(sf.HUB_REFINES))
    conn = sensed_model.open_readonly(real)
    hub_pairs = conn.execute(
        "SELECT count(*) FROM pairs WHERE path_a=? OR path_b=?", (sf.HUB, sf.HUB)
    ).fetchone()[0]
    conn.close()
    assert hub_pairs == 132, "the aside takes the hub past the old per-page cap"
    withheld = _status_as(real, sf.HUB, _external())
    assert withheld == _status_as(twin, sf.HUB, _external())
    assert json.loads(withheld)["line"] == "refined by 1 later note"


def test_a_read_of_another_snapshot_carries_no_status(tmp_path: Path, monkeypatch) -> None:
    """MEDIUM 2: the status describes the snapshot the projection modelled, only."""
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    assert "epistemic_status" in _read(vault, sf.SEAL)
    # The file moves on without the watcher, the graph or a tick seeing it.
    path = vault / sf.SEAL
    path.write_text(path.read_text(encoding="utf-8") + "\nA new paragraph.\n", encoding="utf-8")
    fx.find_module.clear_cache()
    assert "epistemic_status" not in _read(vault, sf.SEAL)
