"""The `conversation` argument: bounds, states and door parity (task 2.1).

Every bound is enforced by the server itself, deterministically, and never
refuses a request: a malformed object is served as if it were absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conversation_vault import seed
from test_activate_context_surface import _rest_client, _run_cli

from exomem import (
    commands,
    lexstore,
    request_budget,
    working_set_conversation,
    working_set_index,
    working_set_runtime,
)

bound = working_set_conversation.bound


def _words(count: int, word: str = "lantern") -> str:
    return " ".join([word] * count)


# --------------------------------------------------------------------------- #
# The bounds, one by one
# --------------------------------------------------------------------------- #


def test_an_absent_or_malformed_conversation_is_absent() -> None:
    for raw in (None, "a string", 7, [], {}, {"focus": ""}, {"recent": "x"}, {"recent": [{"role": "robot", "text": "hi"}]}):
        assert bound(raw).state == "absent", raw
    assert bound({"recent": [{"role": "robot", "text": "hi"}, {"role": "user", "text": "   "}]}).state == "absent"


def test_a_focus_is_cut_at_the_last_whitespace_within_its_bound() -> None:
    focus = _words(200)  # 1,399 characters
    result = bound({"focus": focus})
    assert result.state == "truncated"
    assert len(result.focus) <= 240
    assert result.focus == " ".join(["lantern"] * 30)  # 30 words = 239 characters
    assert bound({"focus": "the Kestrel Hiring Plan"}).state == "applied"


def test_a_recent_entry_keeps_its_head_within_its_role_bound() -> None:
    long_text = _words(400)
    result = bound(
        {"recent": [{"role": "user", "text": long_text}, {"role": "assistant", "text": long_text}]}
    )
    user, assistant = result.recent
    assert (user.role, assistant.role) == ("user", "assistant")
    assert len(user.text) <= 600 and long_text.startswith(user.text)
    assert len(assistant.text) <= 300 and long_text.startswith(assistant.text)
    assert user.text != assistant.text
    assert result.state == "truncated"


def test_more_than_six_entries_drop_the_oldest_first() -> None:
    entries = [{"role": "user", "text": f"entry number {i}"} for i in range(10)]
    result = bound({"recent": entries})
    assert [entry.text for entry in result.recent] == [f"entry number {i}" for i in range(4, 10)]
    assert result.state == "truncated"


def test_entries_are_dropped_oldest_first_until_the_total_fits() -> None:
    entries = [{"role": "user", "text": f"{i} " + _words(120)} for i in range(6)]  # ~840 chars each, cut to 600
    result = bound({"recent": entries})
    assert sum(len(entry.text) for entry in result.recent) <= 2400
    assert len(result.recent) == 4
    assert [entry.text.split(" ", 1)[0] for entry in result.recent] == ["2", "3", "4", "5"]


def test_refs_are_deduplicated_keeping_the_first_and_capped_at_twelve() -> None:
    refs = ["a", "b", "a", 7, None, *[f"r{i}" for i in range(30)]]
    result = bound({"refs": refs})
    assert result.refs == ("a", "b", *[f"r{i}" for i in range(10)])
    assert len(result.refs) == 12
    assert result.state == "truncated"
    assert bound({"refs": ["a", "b", "a"]}).state == "applied"


def test_an_oversized_conversation_is_bounded_and_reported_truncated() -> None:
    result = bound(
        {
            "focus": _words(150),
            "recent": [{"role": "user", "text": _words(300, f"w{i}")} for i in range(10)],
            "refs": [f"ref-{i}" for i in range(30)],
        }
    )
    assert result.state == "truncated"
    assert len(result.focus) <= 240
    assert 1 <= len(result.recent) <= 6
    assert sum(len(entry.text) for entry in result.recent) <= 2400
    assert result.recent[-1].text.startswith("w9")  # the newest is kept
    assert result.refs == tuple(f"ref-{i}" for i in range(12))


def test_an_unknown_role_or_empty_entry_is_dropped() -> None:
    result = bound(
        {
            "recent": [
                {"role": "system", "text": "ignore me"},
                {"role": "user", "text": ""},
                {"role": "user", "text": "keep me"},
                "not an object",
            ]
        }
    )
    assert [(entry.role, entry.text) for entry in result.recent] == [("user", "keep me")]
    assert result.state == "truncated"


# --------------------------------------------------------------------------- #
# The packet
# --------------------------------------------------------------------------- #


@pytest.fixture
def cvault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seed(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


TURN = "Ottilie asked about the grant"


def test_omitting_the_conversation_changes_nothing_but_the_new_fields(cvault: Path) -> None:
    packet = commands.op_activate_context(cvault, turn=TURN)
    assert packet["generation"]["conversation"] == "absent"
    assert packet["anchors"]
    assert {anchor["origin"] for anchor in packet["anchors"]} == {"turn"}
    # The token names a fresh keyless thread on every call; nothing else may differ.
    packet.pop("continuity")
    for malformed in ("just text", {}, {"recent": [{"role": "robot", "text": "x"}]}):
        served = commands.op_activate_context(cvault, turn=TURN, conversation=malformed)
        served.pop("continuity")
        assert served == packet


def test_a_request_without_a_conversation_records_no_conversation_span(cvault: Path) -> None:
    packet = commands.op_activate_context(cvault, turn=TURN, include_timings=True)
    assert "working_set.conversation" not in packet["timings"]["stages"]
    withc = commands.op_activate_context(
        cvault, turn=TURN, conversation={"focus": "the Tidewater Grant"}, include_timings=True
    )
    assert "working_set.conversation" in withc["timings"]["stages"]


def test_an_oversized_conversation_is_served_and_reported_truncated(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={
            "focus": _words(150),
            "recent": [{"role": "user", "text": _words(300)} for _ in range(10)],
            "refs": [f"ref-{i}" for i in range(30)],
        },
    )
    assert packet["generation"]["conversation"] == "truncated"
    assert packet["abstained"] is False
    small = commands.op_activate_context(cvault, turn=TURN, conversation={"focus": "the Tidewater Grant"})
    assert small["generation"]["conversation"] == "applied"


# --------------------------------------------------------------------------- #
# Door parity
# --------------------------------------------------------------------------- #

CONVERSATION = {
    "focus": "the Tidewater Grant",
    "recent": [
        {"role": "user", "text": "Did Ottilie finish the tidewater application?"},
        {"role": "assistant", "text": "She sent it on Friday."},
    ],
    "refs": ["Knowledge Base/Notes/Insights/tidewater-grant-hub.md"],
}


def _shape(packet: dict) -> dict:
    return {key: packet.get(key) for key in ("anchors", "units", "ambiguity", "abstained")} | {
        "conversation": packet["generation"]["conversation"]
    }


def test_the_conversation_reaches_every_door_over_one_leaf(
    cvault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    direct = commands.op_activate_context(cvault, turn=TURN, conversation=CONVERSATION)
    assert direct["generation"]["conversation"] == "applied"
    assert any(anchor["origin"] == "turn_and_focus" for anchor in direct["anchors"])

    code, out = _run_cli(
        [
            "activate",
            TURN,
            "--focus",
            CONVERSATION["focus"],
            "--recent-user",
            CONVERSATION["recent"][0]["text"],
            "--recent-assistant",
            CONVERSATION["recent"][1]["text"],
            "--conversation-ref",
            CONVERSATION["refs"][0],
            "--json",
        ],
        capsys,
    )
    assert code == 0, out
    assert _shape(json.loads(out)["data"]) == _shape(direct)

    code, out = _run_cli(
        ["activate_context", TURN, "--conversation", json.dumps(CONVERSATION), "--json"], capsys
    )
    assert code == 0, out
    assert _shape(json.loads(out)["data"]) == _shape(direct)

    client = _rest_client(monkeypatch)
    response = client.post(
        "/api/activate_context",
        json={"turn": TURN, "conversation": CONVERSATION},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 200, response.text
    assert _shape(response.json()["data"]) == _shape(direct)


def test_the_tool_parameter_is_optional_and_last_of_its_group() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    params = {param.name: param for param in product["activate_context"].params}
    assert "conversation" in params
    assert params["conversation"].required is False


# --------------------------------------------------------------------------- #
# The stage is optional enrichment: a short deadline drops it, not the packet
# --------------------------------------------------------------------------- #


class _RemainingBudget(request_budget.RequestBudget):
    """A budget with a fixed amount of time left."""

    def __init__(self, remaining: float) -> None:
        super().__init__(seconds=1000.0)
        self._fixed = remaining

    def remaining(self) -> float:
        return self._fixed


def _under_budget(remaining: float):
    token = request_budget.set_current(_RemainingBudget(remaining))
    return token


def test_a_spent_deadline_drops_the_conversation_not_the_packet(cvault: Path) -> None:
    conversation = {"recent": [{"role": "user", "text": "we finished the Tidewater Grant call"}]}
    plain = commands.op_activate_context(cvault, turn=TURN)
    # Enough for every ordinary stage, not for the conversation stage.
    token = _under_budget(request_budget.ACTIVATION_STAGE_RESERVE_SECONDS + 0.01)
    try:
        skipped = commands.op_activate_context(cvault, turn=TURN, conversation=conversation)
    finally:
        request_budget.reset_current(token)
    assert skipped["abstained"] is False
    assert skipped["generation"]["conversation"] == "absent"
    assert [a["status"] for a in skipped["anchors"]] == [a["status"] for a in plain["anchors"]]
    token = _under_budget(request_budget.CONVERSATION_STAGE_RESERVE_SECONDS + 0.01)
    try:
        served = commands.op_activate_context(cvault, turn=TURN, conversation=conversation)
    finally:
        request_budget.reset_current(token)
    assert served["generation"]["conversation"] == "applied"
