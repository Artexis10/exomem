"""`bootstrap(section="activity")`: what Exomem Cloud Home shows as recent activity.

Rows here are written by the ledger's own row builder and appender, exactly as a
cell writes them, with chosen timestamps; the activity reader then reads that
file. Each case names what an owner would see wrongly if it broke.
"""

from __future__ import annotations

import datetime as dt
import json
import os
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
    call_ledger.reset_chain_cache()  # an earlier test's failed append is not this ledger's

    def append(tool, at, *, access, outcome="ok", released=None, arguments=None,
               client="claude.ai", app_id=None):
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
            app_id=app_id,
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
        {
            "app_id": None,
            "client": "claude-ai",
            "action": "recalled",
            "items": 4,
            "at": _iso(_at(1.7)),
        },
        {
            "app_id": None,
            "client": "claude-ai",
            "action": "recalled",
            "items": 5,
            "at": _iso(_at(0.5)),
        },
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
    """`items` is shown as a number of notes. A write whose arguments name no page
    and whose commit reported none has an unknown size, and folding it into a
    counted save would show "saved 1" for two saves."""
    ledger("edit_memory", _at(0), access="write", arguments={"path": "Notes/Insights/a.md"})
    ledger("triage_memory", _at(0.5), access="write", arguments={"ref": "item"})

    assert _activity(vault)["events"] == [
        {
            "app_id": None,
            "client": "claude-ai",
            "action": "saved",
            "items": None,
            "at": _iso(_at(0.5)),
        },
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
        {
            "app_id": None,
            "client": "claude-ai",
            "action": "recalled",
            "items": 2,
            "at": _iso(_at(5)),
        },
    ]
    assert payload["since"] == _iso(_at(5))


def test_a_fresh_ledger_reports_no_events_since_the_writer_started(vault, ledger):
    """After a pod restart the ledger is empty: Home says "no activity since" the
    restart, not since some earlier time it cannot vouch for."""
    payload = _activity(vault)

    assert payload["events"] == []
    assert payload["since"] == _iso(call_ledger.WRITER_STARTED_AT)
    assert payload["since"] <= payload["as_of"]


def test_a_full_list_never_claims_to_cover_a_call_it_omits(vault, ledger):
    """With twenty events listed, an older save has no room. `since` must stay newer
    than that save, or Home would claim the list covers a call it does not show."""
    ledger("remember", _at(0), access="write", arguments={"path": "Notes/a.md"}, client="ChatGPT")
    for minute in range(20):  # 20 recalls, two minutes apart, from 30 s after the save
        ledger("ask_memory", _at(0.5 + 2 * minute), access="read", released=1)

    payload = _activity(vault)

    assert len(payload["events"]) == 20
    assert all(event["action"] == "recalled" for event in payload["events"])
    assert payload["since"] == _iso(_at(0.5)) > _iso(_at(0))


def _damage(path: Path, index: int, damage: str) -> None:
    """Damage one row the way a torn write, a deleted row or a changed byte would."""
    lines = path.read_text("utf-8").splitlines()
    if damage == "unreadable row":
        lines[index] = lines[index][:40]
    elif damage == "removed row":
        del lines[index]
    else:  # a changed count that still parses as JSON
        assert '"item_count":2' in lines[index]
        lines[index] = lines[index].replace('"item_count":2', '"item_count":3')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("damage", ["unreadable row", "removed row", "altered row"])
def test_damaged_history_is_never_inside_the_interval_home_claims(vault, ledger, tmp_path, damage):
    """Home's list says what happened "since ...". A recall whose row cannot be
    read, is gone or was changed must not fall inside that interval, nor add to
    a count, even when a readable row shares its millisecond."""
    ledger("bootstrap", _at(0), access="read", released=0)
    ledger("ask_memory", _at(1), access="read", released=2)
    ledger("bootstrap", _at(1), access="read", released=0)
    ledger("ask_memory", _at(2), access="read", released=3)
    _damage(tmp_path / "logs" / "ledger.jsonl", 1, damage)

    payload = _activity(vault)

    assert [event["items"] for event in payload["events"]] == [3]
    assert payload["since"] == _iso(_at(2))


@pytest.mark.parametrize("damage", ["unreadable row", "altered row"])
def test_a_damaged_newest_row_makes_history_unavailable(vault, ledger, tmp_path, damage):
    """With no intact row after the damage, no interval is known to be complete."""
    ledger("ask_memory", _at(0), access="read", released=3)
    ledger("ask_memory", _at(1), access="read", released=2)
    _damage(tmp_path / "logs" / "ledger.jsonl", 1, damage)

    with pytest.raises(OpError, match="ACTIVITY_UNAVAILABLE"):
        _activity(vault)


def test_after_rotation_the_oldest_live_millisecond_is_outside_the_interval(
    vault, ledger, tmp_path
):
    """Rotation moves older rows to the archive, and one of them can share the
    oldest live row's millisecond. The list must not claim to cover it."""
    ledger("ask_memory", _at(1), access="read", released=1)
    ledger("ask_memory", _at(1), access="read", released=2)
    ledger("ask_memory", _at(3), access="read", released=3)
    path = tmp_path / "logs" / "ledger.jsonl"
    path.write_text("".join(path.read_text("utf-8").splitlines(keepends=True)[1:]), "utf-8")

    payload = _activity(vault)

    assert [event["items"] for event in payload["events"]] == [3]
    assert payload["since"] == _iso(_at(3))


def test_a_row_still_being_appended_is_not_yet_history(vault, ledger, tmp_path):
    """Home polls while calls are being recorded. A half-written last row must not
    turn the panel into "unavailable"."""
    ledger("ask_memory", _at(0), access="read", released=2)
    with (tmp_path / "logs" / "ledger.jsonl").open("ab") as stream:
        stream.write(b'{"schema_version":2,"sequence":2,"tool":"ask_')

    assert [event["items"] for event in _activity(vault)["events"]] == [2]


def test_version_1_rows_written_after_this_writer_started_make_history_unavailable(
    vault, ledger, ledger_v1
):
    """An older process still appending version 1 rows leaves calls that no one can
    classify inside the interval Home would claim."""
    ledger("ask_memory", _at(0), access="read", released=2)
    ledger_v1("remember", dt.datetime.now(dt.UTC))

    with pytest.raises(OpError, match="ACTIVITY_UNAVAILABLE"):
        _activity(vault)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller",
)
def test_a_call_another_process_could_not_record_starts_the_interval_after_it(
    vault, ledger, tmp_path
):
    """A failed append leaves no row and no gap in the sequence. The writer marks
    the failure beside the ledger, so a reader in another process must not claim
    the time of that call."""
    ledger("ask_memory", _at(0), access="read", released=2)
    path = tmp_path / "logs" / "ledger.jsonl"
    path.chmod(0o400)
    try:
        lost = call_ledger.record_call(
            request_id="request", tool="ask_memory", outcome="ok", duration_ms=1.0
        )
    finally:
        path.chmod(0o600)
    failed_at = dt.datetime.now(dt.UTC)
    call_ledger.reset_chain_cache()  # the reader is another process: nothing in memory
    assert lost is None, "sanity: the append failed"

    with pytest.raises(OpError, match="ACTIVITY_UNAVAILABLE"):
        _activity(vault)
    ledger("ask_memory", failed_at + dt.timedelta(seconds=1), access="read", released=3)
    payload = _activity(vault)

    assert [event["items"] for event in payload["events"]] == [3]
    assert payload["since"] > _iso(failed_at)


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


def test_the_note_count_includes_every_saved_page(tmp_path, ledger, monkeypatch):
    """The owner counts everything saved, not compiled notes only: an owner who has
    captured only sources or records must not read "Waiting for the first note".
    A collection's manifest is structure: creating a collection saves nothing."""
    from exomem.init import init_vault
    from exomem.vault import kb_root

    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    fresh = tmp_path / "fresh"
    init_vault(fresh)
    for rel in (
        "Sources/Articles/2026-03-01-upload-log.md",
        "Records/vehicle/Events/2026-03-01-oil.md",
        "Notes/Insights/flaky-upload-fixed.md",
        "Records/vehicle/Events/_collection.md",
    ):
        page = kb_root(fresh) / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("# A saved page\n", encoding="utf-8")

    assert _activity(fresh)["note_count"] == 3


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller",
)
def test_a_folder_that_can_be_listed_but_not_entered_counts_as_unknown(vault, ledger):
    """A count that failed to read part of the vault is not a smaller vault."""
    from exomem.vault import kb_root

    notes = kb_root(vault) / "Notes"
    mode = notes.stat().st_mode
    notes.chmod(0o400)
    try:
        count = _activity(vault)["note_count"]
    finally:
        notes.chmod(mode)

    assert count is None


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


def test_older_schema_2_rows_keep_their_count_without_matching_a_home_app(vault, ledger, tmp_path):
    """An upgrade must preserve older counts without inventing an app attribution."""
    old = ledger("ask_memory", _at(0), access="read", released=2)
    del old["app_id"]
    old["row_hash"] = call_ledger.row_hash(old)
    path = tmp_path / "logs" / "ledger.jsonl"
    path.write_bytes(call_ledger.canonical_json(old) + b"\n")
    actor = "12345678-1234-4234-8234-123456789abc"
    ledger("ask_memory", _at(0.5), access="read", released=3, app_id=actor)

    assert _activity(vault)["events"] == [
        {
            "app_id": actor,
            "client": "claude-ai",
            "action": "recalled",
            "items": 3,
            "at": _iso(_at(0.5)),
        },
        {
            "app_id": None,
            "client": "claude-ai",
            "action": "recalled",
            "items": 2,
            "at": _iso(_at(0)),
        },
    ]
    assert call_ledger.verify(path) == []


def test_an_intact_row_with_a_malformed_actor_stays_unattributed(vault, ledger, tmp_path):
    """Older or incompatible producers must not put arbitrary actor text into Home."""
    row = ledger("ask_memory", _at(0), access="read", released=2)
    row["app_id"] = "claimed-client"
    row["row_hash"] = call_ledger.row_hash(row)
    path = tmp_path / "logs" / "ledger.jsonl"
    path.write_bytes(call_ledger.canonical_json(row) + b"\n")

    assert _activity(vault)["events"] == [
        {
            "app_id": None,
            "client": "claude-ai",
            "action": "recalled",
            "items": 2,
            "at": _iso(_at(0)),
        },
    ]
