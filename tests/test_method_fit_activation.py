"""Method activation from ordinary turns: a method the judgment selects is served first, whole, with its contract.

The judgment is a bounded model call (`context_fit.select_methods`); these cases plant its reply at the one seam that
leaves the process (`context_fit._complete`) and check what the product does with it. Whether a real model selects
well is measured on the no-magic-word fixtures, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_context_catalogue import _built, _write
from test_working_set_index import _seed_structure

from exomem import context_fit, context_task, lexstore

SKILL = "Knowledge Base/Skills/kiln-firing/SKILL.md"
SKILL_BODY = (
    "# Kiln firing\n\n"
    "1. Check the shelf layout against the firing log.\n"
    "2. Ramp to 600 degrees over four hours.\n"
    "3. Write the cone result into [[Firing Log]].\n"
)
#: The Records collection `_seed_structure` lays down, with its authored contract line.
COLLECTION = "Knowledge Base/Records/Depot Stock/_collection.md"
COLLECTION_BODY = "Depot stock observations."
#: A turn that names no skill, no collection and no product word.
TURN = "The glaze pots for Saturday are drying on the rack and I want them done before the market."


def _seed(vault: Path, body: str = SKILL_BODY) -> None:
    _write(
        vault, SKILL,
        "---\nname: kiln-firing\ndescription: Fire bisque and glaze loads safely and log the result.\n---\n\n" + body,
    )
    _seed_structure(vault)
    _built(vault).close()
    lexstore.ensure_fresh(vault)


@pytest.fixture(autouse=True)
def _placement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    config = tmp_path / "method-fit.json"
    config.write_text(json.dumps({
        "placement": "local", "runtime": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "planted",
        "model_revision": "r1", "runtime_version": "v1", "timeout_seconds": 5, "max_input_chars": 32000,
        "max_output_tokens": 256, "resource_allowance_gib": 1,
    }), encoding="utf-8")
    monkeypatch.setenv(context_fit.METHOD_FIT_ENV, str(config))


def _judge(monkeypatch: pytest.MonkeyPatch, reply: object) -> list[dict]:
    """The model's reply for every selection; returns what it was offered."""
    offered: list[dict] = []

    async def complete(_config, _template, data, _schema, _name, _timeout, _transport=None):
        offered.extend(json.loads(data)["methods"])
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    monkeypatch.setattr(context_fit, "_complete", complete)
    return offered


def _selected(packet: dict) -> list[dict]:
    return [
        item for item in packet["task_context"]["methods"]["items"] if item.get("selection") == context_task.METHOD_FIT
    ]


def test_a_selected_skill_is_served_first_with_its_whole_contract(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that never names the skill used to get, at best, an unlabelled lead and a pointer."""
    _seed(vault)
    offered = _judge(monkeypatch, {"methods": ["kiln-firing"]})
    packet = _activate(vault, turn=TURN, max_chars=6000)
    (method,) = _selected(packet)
    assert method["read_ref"] == SKILL and method["kind"] == "skill"
    assert method["contract"] == {"text": SKILL_BODY.strip(), "complete": True}
    assert packet["task_context"]["methods"]["items"][0] is not None and packet["task_context"]["methods"]["items"][0]["read_ref"] == SKILL
    assert packet["task_context"]["method_fit"]["state"] == "ready"
    # The response's next move tells the agent to follow the contract, not merely to read a lead.
    assert packet["next_action"]["state"] == "method_selected"
    # The judgment read the author's own trigger text, not the body.
    assert {"label": "kiln-firing", "description": "Fire bisque and glaze loads safely and log the result."} in offered
    # Its method item routes to it, so it is not served a second time as a plain lead.
    assert SKILL not in [item["source_ref"] for item in packet["task_context"]["candidates"]]


def test_a_selected_collection_is_served_with_its_contract(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    _judge(monkeypatch, {"methods": ["Depot stock"]})
    (method,) = _selected(_activate(vault, turn=TURN, max_chars=6000))
    assert method["kind"] == "collection" and method["source_ref"] == COLLECTION
    assert method["contract"]["text"] == COLLECTION_BODY


def test_a_judgment_of_none_serves_no_selected_method(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting the closest method whatever the turn would fire a method on every turn."""
    _seed(vault)
    _judge(monkeypatch, {"methods": []})
    packet = _activate(vault, turn=TURN, max_chars=6000)
    assert _selected(packet) == []
    assert packet["task_context"]["method_fit"]["state"] == "ready"


@pytest.mark.parametrize("reply", [{"methods": ["rm -rf"]}, TimeoutError("slow")])
def test_an_unsupplied_label_or_a_failed_call_selects_nothing_and_keeps_the_turn(
    vault: Path, monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    _seed(vault)
    _judge(monkeypatch, reply)
    packet = _activate(vault, turn=TURN, max_chars=6000)
    assert _selected(packet) == []
    assert packet["task_context"]["method_fit"]["state"] == "unavailable"
    assert packet["task_context"]["state"] in {"ready", "partial", "empty"}


def test_without_a_placement_the_judgment_is_off(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    monkeypatch.delenv(context_fit.METHOD_FIT_ENV)
    offered = _judge(monkeypatch, {"methods": ["kiln-firing"]})
    packet = _activate(vault, turn=TURN, max_chars=6000)
    assert _selected(packet) == [] and offered == []
    assert packet["task_context"]["method_fit"]["state"] == "disabled"


def test_a_long_skill_arrives_cut_at_a_line_with_a_read_handle(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_body = SKILL_BODY + "".join(f"{n}. Step {n} of the long firing schedule, held for an hour.\n" for n in range(4, 200))
    _seed(vault, long_body)
    _judge(monkeypatch, {"methods": ["kiln-firing"]})
    (method,) = _selected(_activate(vault, turn=TURN, max_chars=8000))
    contract = method["contract"]
    assert contract["complete"] is False
    assert 0 < len(contract["text"]) <= 2400
    # A whole line, never half a step.
    assert long_body.strip().startswith(contract["text"])
    assert long_body.strip()[len(contract["text"])] == "\n"


def test_a_tight_allowance_cuts_the_contract_before_it_drops_the_skill(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_body = SKILL_BODY + "".join(f"{n}. Step {n} of the long firing schedule, held for an hour.\n" for n in range(4, 60))
    _seed(vault, long_body)
    _judge(monkeypatch, {"methods": ["kiln-firing"]})
    (method,) = _selected(_activate(vault, turn=TURN, max_chars=1200))
    assert method.get("contract", {}).get("complete") is not True


@pytest.mark.parametrize("placement_file", ["missing.json", "broken.json"])
def test_an_unusable_placement_file_costs_the_judgment_not_the_turn(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement_file: str
) -> None:
    """A bad placement file once emptied the whole task context while saying the judgment was disabled."""
    _seed(vault)
    if placement_file == "broken.json":
        (tmp_path / placement_file).write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(context_fit.METHOD_FIT_ENV, str(tmp_path / placement_file))
    _judge(monkeypatch, {"methods": ["kiln-firing"]})
    packet = _activate(vault, turn=TURN, max_chars=6000)
    assert packet["task_context"]["method_fit"]["state"] == "unavailable"
    assert packet["task_context"]["state"] != "unavailable"


def test_two_selected_methods_both_keep_their_place_under_a_tight_allowance(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_body = SKILL_BODY + "".join(f"{n}. Step {n} of the long firing schedule, held for an hour.\n" for n in range(4, 90))
    _seed(vault, long_body)
    _judge(monkeypatch, {"methods": ["kiln-firing", "Depot stock"]})
    selected = _selected(_activate(vault, turn=TURN, max_chars=1600))
    assert [item["kind"] for item in selected] == ["skill", "collection"]
