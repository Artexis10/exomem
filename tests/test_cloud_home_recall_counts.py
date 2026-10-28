"""Home counts delivered subjects rather than the compiler's admission probes."""

import json

import pytest
import test_activate_context_surface as activation_surface
import test_working_set_planning_item_ref as planning_surface
from conftest import _drain_background_threads
from test_cloud_home_facts import _call
from test_cloud_home_facts import cell as home_cell
from test_governance_egress import write_rule, write_scope

from exomem import call_ledger, working_set_index, working_set_runtime

# These tests count the `anchors` packet's delivered pages, the rollback mode; `maps` is the shipped default.
pytestmark = pytest.mark.usefixtures("anchors_packet")

activation_vault = activation_surface.activation_vault
planned = planning_surface.planned


@pytest.fixture
def cell(activation_vault, monkeypatch, tmp_path):
    _drain_background_threads()
    yield from home_cell.__wrapped__(activation_vault, monkeypatch, tmp_path)


@pytest.fixture
def planning_cell(planned, monkeypatch, tmp_path):
    _drain_background_threads()
    yield from home_cell.__wrapped__(planned, monkeypatch, tmp_path)


@pytest.mark.parametrize("unrelated_policy", [False, True])
def test_home_counts_the_same_delivered_activation_items_under_either_policy(
    activation_vault, cell, unrelated_policy,
):
    """Cold directory admission and unit aliases must not inflate Home activity."""
    if unrelated_policy:
        write_scope(activation_vault, paths="Knowledge Base/Unrelated/**", name="Unrelated")
        write_rule(activation_vault, ceiling=0)

    async def scenario(client):
        for _warmth in ("cold", "warm"):
            packet = await _call(client, "activate_context", {
                "turn": activation_surface.TURN,
                "anchor": "Knowledge Base/Products/Cargo Sled.md",
            }, oauth_client="claude.ai")
            assert packet["abstained"] is False
        return await _call(client, "bootstrap", {"section": "activity"})

    activity = cell(scenario)
    rows = [json.loads(line) for line in call_ledger.ledger_path().read_text().splitlines()]
    activations = [row for row in rows if row["tool"] == "activate_context"]
    # The maintained fixture delivers the product page and one authored plan.
    assert [row["item_count"] for row in activations] == [2, 2]
    assert [(event["action"], event["items"]) for event in activity["events"]] == [
        ("recalled", 4),
    ]


@pytest.mark.parametrize("missing_item_pages", [False, True])
def test_home_counts_planning_items_without_counting_their_filing_home(
    planned, planning_cell, monkeypatch, missing_item_pages,
):
    """Two plans count twice; their shared fallback manifest cannot identify them."""
    if missing_item_pages:
        monkeypatch.setattr(working_set_index, "_planning_item_pages", lambda *_a, **_k: {})
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(planned)
    try:
        index.rebuild()
    finally:
        index.close()

    async def scenario(client):
        for _warmth in ("cold", "warm"):
            packet = await _call(client, "activate_context", {
                "turn": "First resurface the harbour slipway, then replace the lighthouse lantern.",
            }, oauth_client="claude.ai")
            assert packet["abstained"] is False
        return await _call(client, "bootstrap", {"section": "activity"})

    activity = planning_cell(scenario)
    rows = [json.loads(line) for line in call_ledger.ledger_path().read_text().splitlines()]
    activations = [row for row in rows if row["tool"] == "activate_context"]
    # The maintained fixture authors two distinct plans under one collection.
    expected = None if missing_item_pages else 2
    assert [row["item_count"] for row in activations] == [expected, expected]
    assert [(event["action"], event["items"]) for event in activity["events"]] == [
        ("recalled", None if missing_item_pages else 4),
    ]


def test_home_counts_a_map_packet_as_unknown_not_zero(activation_vault, cell, monkeypatch):
    """A map lists catalogue entries without a per-page basis, so Home cannot say how many pages it released."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")

    async def scenario(client):
        packet = await _call(client, "activate_context", {"turn": activation_surface.TURN}, oauth_client="claude.ai")
        assert packet["abstained"] is False
        assert "map" in packet
        return await _call(client, "bootstrap", {"section": "activity"})

    activity = cell(scenario)
    rows = [json.loads(line) for line in call_ledger.ledger_path().read_text().splitlines()]
    assert [row["item_count"] for row in rows if row["tool"] == "activate_context"] == [None]
    assert [(event["action"], event["items"]) for event in activity["events"]] == [("recalled", None)]
