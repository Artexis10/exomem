"""`logs/activations.jsonl`: one host-local row per activation, never the turn.

The row answers "which client activated, from which door, and what did it
get" -- the per-client evidence the acceptance script and the operator need --
and nothing else. It never carries the turn or any hash of it, never the raw
session value, never unit or title text, and it feeds nothing: ACT-R, the
recent-context block and the hot profile read queries, reads and writes only.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from exomem import command_surface, commands, query_log, usage
from exomem.governance.principal import OWNER_AUDIENCE, RequestPrincipal, request_scope

TURN = "Where did we leave the Harbor Lamp order for Project Alpha?"
SESSION = "ep-" + "b2" * 16


@pytest.fixture
def activation_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Logging switched on for this test only, into its own directory.

    The suite runs with the embedding kill switch, which is also the query
    log's off switch; the gate itself is exercised separately below.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    return log_dir / "activations.jsonl"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_one_row_per_call_and_the_turn_is_never_in_it(vault: Path, activation_log: Path) -> None:
    commands.op_activate_context(vault, turn=TURN, client="claude-code", session=SESSION)

    [row] = _rows(activation_log)
    raw = activation_log.read_text(encoding="utf-8")
    assert TURN not in raw
    assert hashlib.sha256(TURN.encode()).hexdigest() not in raw
    assert hashlib.sha256(TURN.encode()).hexdigest()[:16] not in raw
    assert SESSION not in raw
    assert row["client_declared"] == "claude-code"
    assert row["client"] == "claude-code"
    assert len(row["session_hash"]) == 16
    assert row["door"]
    assert row["outcome"] in {"served", "abstained"}
    assert "duration_ms" in row
    assert isinstance(row["recent"], dict)


@pytest.mark.parametrize(
    "principal, kind, principal_hash",
    [
        (RequestPrincipal(audience_id=OWNER_AUDIENCE, surface="mcp"), "owner", "owner"),
        (
            RequestPrincipal(audience_id=OWNER_AUDIENCE, surface="mcp", remote_owner=True),
            "owner-oauth",
            "owner",
        ),
        (RequestPrincipal(audience_id="client-a", surface="mcp"), "principal", None),
    ],
    ids=["owner", "remote-owner", "principal"],
)
def test_the_row_says_which_kind_of_principal_activated(
    vault: Path, activation_log: Path, principal, kind: str, principal_hash: str | None
) -> None:
    """The remote-owner hand-over: an owner reached through the host's remote
    binding shares the owner's hash, so only the kind tells the two apart."""
    with request_scope(principal):
        query_log.log_activation_call(vault, packet={}, client="chatgpt")

    [row] = _rows(activation_log)
    assert row["principal_kind"] == kind
    if principal_hash is not None:
        assert row["principal_hash"] == principal_hash
    else:
        assert row["principal_hash"] not in (None, "owner")


def test_abstentions_are_logged_with_their_reason(vault: Path, activation_log: Path) -> None:
    commands.op_activate_context(vault, turn="   ")

    [row] = _rows(activation_log)
    assert row["outcome"] == "abstained"
    assert row["abstention_reason"] == "unresolved"
    assert row["session_hash"] is None
    assert row["client_declared"] is None


def test_the_session_hash_is_stable_per_vault_and_differs_across_vaults(
    vault: Path, activation_log: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-vault"
    shutil.copytree(vault, other)
    commands.op_activate_context(vault, turn="   ", session=SESSION)
    commands.op_activate_context(vault, turn="   ", session=SESSION)
    commands.op_activate_context(other, turn="   ", session=SESSION)

    first, again, elsewhere = (row["session_hash"] for row in _rows(activation_log))
    assert first == again
    assert first != elsewhere


def test_an_invalid_label_is_recorded_as_invalid_and_never_echoed(
    vault: Path, activation_log: Path
) -> None:
    commands.op_activate_context(vault, turn="   ", client="Evil Label <script>")

    [row] = _rows(activation_log)
    assert row["client_declared"] == "invalid"
    assert row["client"] is None
    assert "Evil" not in activation_log.read_text(encoding="utf-8")


def test_observed_mcp_identity_wins_over_a_declared_label(
    vault: Path, activation_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {
            "client_name": "chatgpt",
            "client_version": "1.0",
            "transport": "http",
            "session_id": "raw-mcp-session",
        },
    )
    commands.op_activate_context(vault, turn="   ", client="claude-code")

    [row] = _rows(activation_log)
    assert row["client"] == "chatgpt"
    assert row["client_observed"] == "chatgpt"
    assert row["client_declared"] == "claude-code"
    assert row["transport"] == "http"
    assert "raw-mcp-session" not in activation_log.read_text(encoding="utf-8")


def test_the_disable_gates_hold(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_dir = tmp_path / "gated-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_QUERY_LOG", "1")

    query_log.log_activation_call(
        vault, packet={"abstained": True, "abstention": {"reason": "unresolved"}}
    )

    assert not (log_dir / "activations.jsonl").exists()


def test_content_private_mode_omits_the_anchor_refs(
    vault: Path, activation_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = {
        "abstained": False,
        "anchors": [{"ref": "Knowledge Base/Notes/private.md", "status": "resolved"}],
        "recent_context": [{"why": "episode"}, {"why": "edited"}, {"why": "edited"}],
        "generation": {"continuity": "absent"},
    }
    query_log.log_activation_call(vault, packet=packet)
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    query_log.log_activation_call(vault, packet=packet)

    open_row, private_row = _rows(activation_log)
    assert open_row["anchors"] == ["Knowledge Base/Notes/private.md"]
    assert open_row["recent"] == {"episode": 1, "edited": 2}
    assert "anchors" not in private_row
    assert "private.md" not in json.dumps(private_row)


def test_activation_rows_never_feed_the_usage_signal(
    vault: Path, activation_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    weights = {"w_surfaced": 1.0, "w_read": 1.0, "w_cited": 1.0}
    query_log.log_get_call(read_path="Knowledge Base/Notes/read.md")
    usage.reset_usage_cache()
    before = usage.access_events(activation_log.parent, **weights)
    assert before, "the signal must be live, or an unchanged None proves nothing"
    for _ in range(3):
        query_log.log_activation_call(
            vault,
            packet={
                "abstained": False,
                "anchors": [{"ref": "Knowledge Base/Notes/hot.md", "status": "resolved"}],
                "generation": {},
            },
        )
    usage.reset_usage_cache()

    assert usage.access_events(activation_log.parent, **weights) == before
    assert "Knowledge Base/Notes/hot.md" not in json.dumps(before)
