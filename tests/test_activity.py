"""`bootstrap(section="activity")`: what Exomem Cloud Home shows as recent activity.

Rows here are written by the ledger's own row builder and appender, exactly as a
cell writes them, with chosen timestamps; the activity reader then reads that
file. Each case names what an owner would see wrongly if it broke.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from test_call_ledger import version_1_row

from exomem import call_ledger, commands
from exomem.cli_ops import OpError
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

T0 = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(directory))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    directory.mkdir()
    path = directory / "ledger.jsonl"

    def append(tool, at, *, access, outcome="ok", released=None, arguments=None,
               client="claude.ai"):
        sequence, prev_hash = _chain_head(path)
        row = call_ledger.build_row(
            sequence=sequence + 1,
            prev_hash=prev_hash,
            request_id="request",
            tool=tool,
            outcome=outcome,
            duration_ms=1.0,
            arguments=arguments or {},
            client_name=client,
            timestamp=at.isoformat(timespec="milliseconds"),
            access=access,
            released_items=released,
        )
        call_ledger.append_row(row, path=path)
        return row

    return append


def _chain_head(path: Path) -> tuple[int, str]:
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    if not lines:
        return 0, call_ledger.GENESIS_HASH
    last = json.loads(lines[-1])
    return last["sequence"], last["row_hash"]


def _activity(vault: Path) -> dict:
    with request_scope(owner_principal(surface="mcp")):
        return commands.op_bootstrap(vault, section="activity")


def _at(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def test_calls_of_one_client_and_action_within_a_minute_are_one_event(vault, ledger):
    """Without the merge window one recall burst fills the list with near-duplicates;
    with an unbounded one, recalls minutes apart collapse into one line."""
    ledger("ask_memory", _at(0), access="read", released=2)
    ledger("read_memory", _at(0.5), access="read", released=3)
    ledger("activate_context", _at(1.7), access="read", released=4)

    events = _activity(vault)["events"]

    assert events == [
        {"client": "claude-ai", "action": "recalled", "items": 4, "at": _iso(_at(1.7))},
        {"client": "claude-ai", "action": "recalled", "items": 5, "at": _iso(_at(0.5))},
    ]


def test_only_successful_memory_calls_are_activity(vault, ledger):
    """A refused save or a failed recall did not happen for the owner, and a
    settings read is not an assistant using memory."""
    ledger("remember", _at(0), access="write", outcome="refused")
    ledger("ask_memory", _at(1), access="read", outcome="error")
    ledger("configure_memory", _at(2), access="read", released=0)
    ledger("bootstrap", _at(3), access="read", released=0)

    assert _activity(vault)["events"] == []


def test_events_are_newest_first_and_at_most_twenty(vault, ledger):
    """Home renders the list as given: an oldest-first or unbounded list shows
    stale activity first or grows without limit."""
    for minute in range(0, 50, 2):  # 25 recalls, two minutes apart
        ledger("ask_memory", _at(minute), access="read", released=1)

    payload = _activity(vault)

    assert [event["at"] for event in payload["events"]] == [
        _iso(_at(minute)) for minute in range(48, 8, -2)
    ]
    # The list accounts for every call from `since`; older calls are not claimed.
    assert payload["since"] == payload["events"][-1]["at"]


def test_a_save_of_unreported_size_makes_its_event_count_unknown(vault, ledger):
    """`items` is shown as a number of notes. A write whose destination the server
    derived reports no target, and folding it into a counted save would show
    "saved 1" for two saves."""
    ledger("edit_memory", _at(0), access="write", arguments={"path": "Notes/Insights/a.md"})
    ledger("remember", _at(0.5), access="write", arguments={"title": "derived path"})

    assert _activity(vault)["events"] == [
        {"client": "claude-ai", "action": "saved", "items": None, "at": _iso(_at(0.5))},
    ]


def test_a_write_names_its_addressed_targets_as_items(vault, ledger):
    ledger("edit_memory", _at(0), access="write", arguments={"path": "Notes/Insights/a.md"})

    assert _activity(vault)["events"][0]["items"] == 1


def test_version_1_rows_are_not_activity_and_since_starts_after_them(vault, ledger, ledger_v1):
    """After an upgrade on a kept ledger, the older rows cannot say whether they read
    or wrote. Claiming "no activity since" their time would hide what they did."""
    ledger_v1("ask_memory", _at(0))
    ledger_v1("remember", _at(1))
    ledger("ask_memory", _at(5), access="read", released=2)

    payload = _activity(vault)

    assert payload["events"] == [
        {"client": "claude-ai", "action": "recalled", "items": 2, "at": _iso(_at(5))},
    ]
    assert payload["since"] == _iso(_at(5))


def test_a_fresh_ledger_reports_no_events_since_the_writer_started(vault, ledger):
    """After a pod restart the ledger is empty: Home says "no activity since" the
    restart, not since some earlier time it cannot vouch for."""
    payload = _activity(vault)

    assert payload["events"] == []
    assert payload["since"] == _iso(call_ledger.WRITER_STARTED_AT)
    assert payload["since"] <= payload["as_of"]


def test_activity_is_refused_to_anyone_but_the_owner(vault, ledger):
    """A delegated principal must not learn when and how much the owner's other
    assistants recalled and saved."""
    ledger("ask_memory", _at(0), access="read", released=2)

    with request_scope(RequestPrincipal("principal:delegate", surface="mcp")):
        with pytest.raises(OpError, match="ACTIVITY_OWNER_REQUIRED"):
            commands.op_bootstrap(vault, section="activity")


def test_a_fresh_vault_counts_zero_notes(tmp_path, ledger, monkeypatch):
    """A new cell must say zero ("waiting for the first note"), not unknown."""
    from exomem.init import init_vault

    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    fresh = tmp_path / "fresh"
    init_vault(fresh)

    assert _activity(fresh)["note_count"] == 0


def test_an_unreadable_notes_folder_counts_as_unknown_not_zero(vault, ledger):
    """A count that failed to read is not an empty vault."""
    from exomem.vault import kb_root

    notes = kb_root(vault) / "Notes"
    for path in sorted(notes.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    notes.rmdir()
    notes.write_text("not a folder", encoding="utf-8")

    assert _activity(vault)["note_count"] is None


@pytest.fixture
def ledger_v1(tmp_path, ledger):
    """Append rows exactly as the version 1 builder wrote them, on the same chain."""
    path = tmp_path / "logs" / "ledger.jsonl"

    def append(tool, at):
        sequence, prev_hash = _chain_head(path)
        row = version_1_row(
            sequence=sequence + 1, prev_hash=prev_hash, tool=tool,
            ts_utc=at.isoformat(timespec="milliseconds"),
        )
        call_ledger.append_row(row, path=path)

    return append
