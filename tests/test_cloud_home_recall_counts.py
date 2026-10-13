"""Home counts delivered subjects rather than the compiler's admission probes."""

import json

import pytest
import test_activate_context_surface as activation_surface
from conftest import _drain_background_threads
from test_cloud_home_facts import _call
from test_cloud_home_facts import cell as home_cell
from test_governance_egress import write_rule, write_scope

from exomem import call_ledger

activation_vault = activation_surface.activation_vault


@pytest.fixture
def cell(activation_vault, monkeypatch, tmp_path):
    _drain_background_threads()
    yield from home_cell.__wrapped__(activation_vault, monkeypatch, tmp_path)


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
