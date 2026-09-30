"""Conversation content is request-scoped and never crosses callers (tasks 2.2, 2.3).

Nothing about a conversation is written anywhere the activation path owns, the
packet cache is bypassed, refs are not hot-profile events, the token never
encodes a conversation-only anchor, and a ref to a withheld page answers as an
absent one, `generation` included.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from conversation_vault import seed
from test_governance_egress import _external, write_rule, write_scope

from exomem import (
    call_ledger,
    commands,
    lexstore,
    query_log,
    working_set,
    working_set_heat,
    working_set_index,
    working_set_runtime,
)
from exomem.governance.principal import request_scope

SENTINEL = "quillfeather-lantern-sentinel-phrase"
TURN = "Ottilie asked about the grant"
HUB_PATH = "Knowledge Base/Notes/Insights/tidewater-grant-hub.md"
ENTITY_PATH = "Knowledge Base/Entities/People/Ottilie Marsh.md"


@pytest.fixture
def cvault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seed(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _all_files(*roots: Path) -> list[Path]:
    return [path for root in roots if root.exists() for path in root.rglob("*") if path.is_file()]


def _call_through_the_server(vault: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict) -> dict:
    """One `activate_context` call through the built MCP server WITH its
    middleware, so the call ledger records it exactly as in production."""
    from exomem import server as server_module

    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    for name in ("EXOMEM_DISABLE_EMBEDDINGS", "EXOMEM_DISABLE_RELEVANCE_CHECK", "EXOMEM_DISABLE_FILE_WATCHER"):
        monkeypatch.setenv(name, "1")
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    mcp = server_module.build_server(require_auth=False)
    result = asyncio.run(mcp.call_tool("activate_context", arguments))
    if isinstance(result.structured_content, dict):
        payload = result.structured_content
        return payload.get("result", payload) if "abstained" not in payload else payload
    return json.loads(result.content[0].text)


def test_no_state_file_holds_the_conversation_or_its_hash(
    cvault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "logs"
    ledger = tmp_path / "ledger"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(ledger))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    call_ledger.reset_chain_cache()
    text = f"we agreed the {SENTINEL} wording before the Tidewater Grant call"
    conversation = {
        "focus": f"{SENTINEL} focus",
        "recent": [{"role": "user", "text": text}],
        "refs": [HUB_PATH],
    }
    packet = _call_through_the_server(cvault, monkeypatch, {"turn": TURN, "conversation": conversation})
    assert packet["generation"]["conversation"] == "applied"
    # Every digest a ledger or log could plausibly hold: each text, and the
    # serialised argument itself in the ledger's own canonical form and in
    # ordinary JSON (the forms a per-argument hash is taken over).
    serialised = (
        call_ledger.canonical_json({"v": conversation}),
        call_ledger.canonical_json(conversation),
        json.dumps(conversation).encode("utf-8"),
    )
    digests = {
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in (text, f"{SENTINEL} focus", SENTINEL, HUB_PATH)
    } | {hashlib.sha256(blob).hexdigest() for blob in serialised}
    state_root = tmp_path / "xdg-state"
    files = _all_files(state_root, logs, ledger, cvault)
    assert (ledger / "ledger.jsonl") in files, "the call went through the ledgered middleware"
    for path in files:
        blob = path.read_bytes()
        assert SENTINEL.encode() not in blob, path
        for digest in digests:
            assert digest.encode() not in blob, path
    (row,) = [json.loads(line) for line in (ledger / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert row["args"]["conversation"] == {"len": len(serialised[0])}
    assert SENTINEL not in json.dumps(packet)
    call_ledger.reset_chain_cache()


def test_the_activation_log_holds_only_presence_counts_and_the_state(
    cvault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    commands.op_activate_context(cvault, turn=TURN)
    commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={
            "focus": SENTINEL,
            "recent": [{"role": "user", "text": SENTINEL}, {"role": "assistant", "text": SENTINEL}],
            "refs": [HUB_PATH, ENTITY_PATH, "not-a-page"],
        },
    )
    without, with_conversation = [
        json.loads(line) for line in (logs / "activations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert without["conversation"] == {"state": "absent", "recent": 0, "refs": 0}
    assert with_conversation["conversation"] == {"state": "applied", "recent": 2, "refs": 3}
    assert SENTINEL not in (logs / "activations.jsonl").read_text(encoding="utf-8")


def test_a_request_with_a_conversation_bypasses_the_packet_cache(
    cvault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiles: list[int] = []
    real = working_set.compile_packet

    def counting(*args, **kwargs):
        compiles.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(working_set, "compile_packet", counting)
    commands.op_activate_context(cvault, turn=TURN)
    commands.op_activate_context(cvault, turn=TURN)
    assert len(compiles) == 1, "the plain request is still served from the cache"

    conversation = {"recent": [{"role": "user", "text": "the Tidewater Grant call went well"}]}
    commands.op_activate_context(cvault, turn=TURN, conversation=conversation)
    commands.op_activate_context(cvault, turn=TURN, conversation=conversation)
    assert len(compiles) == 3, "each conversation request is compiled afresh"
    commands.op_activate_context(cvault, turn=TURN)
    assert len(compiles) == 3, "a conversation packet is never stored for a plain request"


def test_conversation_refs_are_not_hot_profile_events(
    cvault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list = []
    real = working_set_heat.append

    def spy(root, batch, *args, **kwargs):
        events.extend(batch)
        return real(root, batch, *args, **kwargs)

    monkeypatch.setattr(working_set_heat, "append", spy)
    commands.op_activate_context(cvault, turn=TURN)
    baseline = len(events)
    commands.op_activate_context(cvault, turn=TURN, conversation={"refs": [HUB_PATH, ENTITY_PATH]})
    assert not any(getattr(event, "path", "") in {HUB_PATH} for event in events[baseline:])


def test_the_token_never_encodes_a_conversation_only_anchor(cvault: Path) -> None:
    conversation = {"recent": [{"role": "user", "text": "the Tidewater Grant call went well"}]}
    packet = commands.op_activate_context(cvault, turn=TURN, conversation=conversation)
    by_title = {anchor["title"]: anchor for anchor in packet["anchors"]}
    grant = by_title["Tidewater Grant"]
    assert grant["status"] == "resolved" and "conversation" in grant["evidence"]
    payload = working_set_runtime.decode_continuity(packet["continuity"])
    refs = set(payload["refs"])
    assert by_title["Ottilie Marsh"]["ref"] in refs
    assert grant["ref"] not in refs and grant["path"] not in refs


# --------------------------------------------------------------------------- #
# Withheld equals absent (task 2.3)
# --------------------------------------------------------------------------- #


def _withhold_the_grant_hub(vault: Path) -> None:
    write_scope(vault, paths="Notes/Insights/tidewater-grant-hub.md")
    write_rule(vault, ceiling=0, audience="external")


def _served(vault: Path, **arguments) -> dict:
    with request_scope(_external()):
        packet = commands.op_activate_context(vault, turn=TURN, **arguments)
    packet.pop("continuity", None)
    packet.pop("timings", None)
    return packet


def test_a_withheld_ref_answers_as_an_absent_one(cvault: Path) -> None:
    _withhold_the_grant_hub(cvault)
    visible = "Knowledge Base/Entities/People/Ottilie Marsh.md"
    with_withheld = _served(cvault, conversation={"refs": [HUB_PATH, visible]})
    without = _served(cvault, conversation={"refs": [visible]})
    assert with_withheld == without
    assert with_withheld["generation"]["conversation"] == without["generation"]["conversation"] == "applied"
    assert "Tidewater" not in json.dumps(with_withheld)


def test_a_withheld_name_in_an_earlier_turn_reaches_nothing(cvault: Path) -> None:
    _withhold_the_grant_hub(cvault)
    naming = _served(
        cvault, conversation={"recent": [{"role": "user", "text": "the Tidewater Grant call went well"}]}
    )
    plain = _served(
        cvault, conversation={"recent": [{"role": "user", "text": "the harbour call went well"}]}
    )
    assert naming == plain
    assert "Tidewater" not in json.dumps(naming)


def test_a_ref_only_conversation_naming_only_a_withheld_page_equals_no_conversation_but_the_state(
    cvault: Path,
) -> None:
    _withhold_the_grant_hub(cvault)
    only_withheld = _served(cvault, conversation={"refs": [HUB_PATH]})
    unknown = _served(cvault, conversation={"refs": ["Knowledge Base/Notes/never-existed.md"]})
    assert only_withheld == unknown
