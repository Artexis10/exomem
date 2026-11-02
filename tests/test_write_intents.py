"""The intent journal keeps an accepted write whole across a crash, within bounds, under one identity."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from exomem import write_intents
from exomem.write_intents import (
    Bounds,
    InputFile,
    JournalFormatTooNew,
    JournalFull,
    JournalUnavailable,
    WriteIntentJournal,
)

_ACCEPT = {
    "audience": "owner",
    "command": "remember",
    "arguments": {"title": "A", "body": "hello"},
    "surface": "mcp",
    "principal": {"audience": "owner", "surface": "mcp", "session_id": "s1"},
    "digest": "digest-1",
}


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _journal(vault: Path, **kwargs) -> WriteIntentJournal:
    kwargs.setdefault("bounds", Bounds(free_space_floor=0))
    return WriteIntentJournal(vault, vault_id="main", replica_id="desktop", **kwargs)


def test_an_intent_accepted_by_a_process_that_is_then_killed_reads_back_complete(
    vault: Path, tmp_path: Path
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"x" * 4096)
    script = tmp_path / "accept_then_die.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, signal, sys
            from pathlib import Path
            from exomem.write_intents import Bounds, InputFile, WriteIntentJournal

            journal = WriteIntentJournal(
                Path(sys.argv[1]), vault_id="main", replica_id="desktop", bounds=Bounds(free_space_floor=0)
            )
            intent, _ = journal.accept(
                audience="owner", command="remember", arguments={"title": "A"}, surface="mcp",
                principal={"audience": "owner"}, digest="d", retry_key="implicit:owner:k",
                inputs=[InputFile("file-0", Path(sys.argv[2]))],
            )
            print(intent.intent_id, flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
            """
        )
    )
    run = subprocess.run(
        [sys.executable, str(script), str(vault), str(source)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    assert run.returncode == -signal.SIGKILL
    intent_id = run.stdout.split()[0]

    intent = _journal(vault).get(intent_id)

    assert intent is not None
    assert intent.state == "accepted"
    assert intent.arguments == {"title": "A"}
    staged = _journal(vault).input_path(intent_id, "file-0")
    assert staged.read_bytes() == b"x" * 4096
    assert intent.inputs[0]["size"] == 4096


def test_a_write_at_the_count_bound_is_refused_as_journal_full_and_stages_nothing(vault: Path, tmp_path: Path) -> None:
    journal = _journal(vault, bounds=Bounds(max_intents=2, audience_share=1.0, free_space_floor=0))
    for index in range(2):
        journal.accept(**_ACCEPT, retry_key=f"k{index}")
    source = tmp_path / "late.bin"
    source.write_bytes(b"late")

    with pytest.raises(JournalFull) as refusal:
        journal.accept(**_ACCEPT, retry_key="k-late", inputs=[InputFile("f", source)])

    assert refusal.value.cause == "journal_full"
    assert not any(journal.inputs_root.iterdir())


def test_one_audience_cannot_take_more_than_its_share_of_the_journal(vault: Path) -> None:
    journal = _journal(vault, bounds=Bounds(max_intents=4, free_space_floor=0))
    journal.accept(**{**_ACCEPT, "audience": "agent"}, retry_key="a1")
    journal.accept(**{**_ACCEPT, "audience": "agent"}, retry_key="a2")

    with pytest.raises(JournalFull):
        journal.accept(**{**_ACCEPT, "audience": "agent"}, retry_key="a3")

    journal.accept(**{**_ACCEPT, "audience": "owner"}, retry_key="o1")


def test_the_byte_bound_counts_arguments_and_staged_inputs(vault: Path, tmp_path: Path) -> None:
    journal = _journal(vault, bounds=Bounds(max_bytes=1000, audience_share=1.0, free_space_floor=0))
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 2000)

    with pytest.raises(JournalFull):
        journal.accept(**_ACCEPT, retry_key="k", inputs=[InputFile("f", source)])


def test_accept_leaves_the_free_space_floor_free_on_the_state_volume(vault: Path) -> None:
    journal = _journal(
        vault,
        bounds=Bounds(free_space_floor=1000),
        free_bytes=lambda _path: 999,
    )

    with pytest.raises(JournalFull):
        journal.accept(**_ACCEPT, retry_key="k")


def test_a_stored_row_has_no_credential_column(vault: Path) -> None:
    journal = _journal(vault)
    journal.accept(**_ACCEPT, retry_key="k")

    with sqlite3.connect(journal.path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(intents)")]

    assert columns  # the schema was read
    for column in columns:
        for forbidden in ("bearer", "password", "secret", "credential", "authorization_header"):
            assert forbidden not in column


def test_a_journal_from_a_newer_release_refuses_to_open(vault: Path) -> None:
    journal = _journal(vault)
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'format_version'", (str(write_intents.FORMAT_VERSION + 1),))

    with pytest.raises(JournalFormatTooNew) as refusal:
        _journal(vault)

    assert isinstance(refusal.value, JournalUnavailable)
    assert refusal.value.cause == "journal_unavailable"


def test_a_corrupt_journal_file_is_unavailable_not_a_crash(vault: Path) -> None:
    journal = _journal(vault)
    journal.path.write_bytes(b"this is not a sqlite database" * 100)
    for suffix in ("-wal", "-shm"):
        journal.path.with_name(journal.path.name + suffix).unlink(missing_ok=True)

    with pytest.raises(JournalUnavailable):
        _journal(vault)


def test_a_byte_bound_below_twice_the_largest_synchronous_input_warns_at_start(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_UPLOAD_SESSION_MAX_BYTES", str(1000))

    assert _journal(vault, bounds=Bounds(max_bytes=1999, free_space_floor=0)).byte_bound_warning
    assert _journal(vault, bounds=Bounds(max_bytes=2000, free_space_floor=0)).byte_bound_warning is None


def test_the_default_byte_bound_holds_two_of_the_largest_synchronous_input(vault: Path) -> None:
    assert _journal(vault, bounds=Bounds(free_space_floor=0)).byte_bound_warning is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_journal_and_staged_inputs_are_owner_only(vault: Path, tmp_path: Path) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"data")
    journal = _journal(vault)
    intent, _ = journal.accept(**_ACCEPT, retry_key="k", inputs=[InputFile("f", source)])

    for path in (journal.path, journal.inputs_root, journal.input_path(intent.intent_id, "f")):
        assert path.stat().st_mode & 0o077 == 0


def test_two_accepts_under_one_key_return_the_same_intent_and_stage_once(vault: Path, tmp_path: Path) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"data")
    journal = _journal(vault)

    first, first_created = journal.accept(**_ACCEPT, retry_key="implicit:owner:k", inputs=[InputFile("f", source)])
    second, second_created = journal.accept(**_ACCEPT, retry_key="implicit:owner:k", inputs=[InputFile("f", source)])

    assert (first_created, second_created) == (True, False)
    assert second.intent_id == first.intent_id
    assert len(journal.non_terminal()) == 1
    assert [entry.name for entry in journal.inputs_root.iterdir()] == [first.intent_id]


def test_two_processes_racing_on_one_key_record_one_intent(vault: Path, tmp_path: Path) -> None:
    script = tmp_path / "race.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from exomem.write_intents import Bounds, WriteIntentJournal

            journal = WriteIntentJournal(
                Path(sys.argv[1]), vault_id="main", replica_id="desktop", bounds=Bounds(free_space_floor=0)
            )
            for _ in range(20):
                intent, _created = journal.accept(
                    audience="owner", command="remember", arguments={}, surface="mcp",
                    principal={}, digest="d", retry_key="implicit:owner:race",
                )
            print(intent.intent_id)
            """
        )
    )
    runs = [
        subprocess.Popen([sys.executable, str(script), str(vault)], stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    ids = {run.communicate()[0].strip() for run in runs}

    assert len(ids) == 1
    assert len(_journal(vault).non_terminal()) == 1


def test_a_write_without_retry_identity_gets_its_own_key_and_the_24_hour_window(vault: Path) -> None:
    journal = _journal(vault)

    first, _ = journal.accept(**_ACCEPT)
    second, _ = journal.accept(**_ACCEPT)

    assert first.intent_id != second.intent_id
    assert first.retry_key == f"intent:{first.intent_id}"
    assert first.window_seconds == write_intents.INTENT_RETENTION_SECONDS
    assert first.request_id != second.request_id


def test_a_resend_after_the_intent_ended_finds_its_terminal(vault: Path) -> None:
    journal = _journal(vault)
    intent, _ = journal.accept(**_ACCEPT, retry_key="implicit:owner:k")
    journal.finish(intent.intent_id, "committed", terminal={"ok": True})

    again = journal.find_by_key("implicit:owner:k")

    assert again is not None
    assert again.state == "committed"
    assert again.terminal == {"ok": True}
    assert again.arguments is None


def test_a_committed_intent_drops_its_inputs_and_a_rejected_one_keeps_its_arguments(
    vault: Path, tmp_path: Path
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"data")
    journal = _journal(vault)
    done, _ = journal.accept(**_ACCEPT, retry_key="k1", inputs=[InputFile("f", source)])
    rejected, _ = journal.accept(**_ACCEPT, retry_key="k2", inputs=[InputFile("f", source)])

    journal.finish(done.intent_id, "committed", terminal={})
    journal.finish(rejected.intent_id, "rejected", terminal={"error": "bad"})

    assert not journal.input_path(done.intent_id, "f").exists()
    assert not journal.input_path(rejected.intent_id, "f").exists()
    assert journal.get(rejected.intent_id).arguments == _ACCEPT["arguments"]


def test_terminal_rows_are_purged_after_their_retention(vault: Path) -> None:
    now = [1_000_000.0]
    journal = _journal(vault, clock=lambda: now[0])
    intent, _ = journal.accept(**_ACCEPT, retry_key="k")
    journal.finish(intent.intent_id, "committed", terminal={})

    now[0] += write_intents.INTENT_RETENTION_SECONDS - 1
    assert journal.purge_expired() == 0
    now[0] += 2
    assert journal.purge_expired() == 1
    assert journal.get(intent.intent_id) is None


def test_a_resend_dedupes_and_the_row_is_purged_for_the_keys_own_window_not_a_day(vault: Path) -> None:
    """One retention rule: a contended write dedupes for its key's 600 s, the same as an inline one, and then leaves."""
    now = [1_000_000.0]
    journal = _journal(vault, clock=lambda: now[0])
    intent, _ = journal.accept(**_ACCEPT, retry_key="implicit:owner:w", window_seconds=600.0)
    journal.finish(intent.intent_id, "committed", terminal={"ok": True})

    now[0] += 599
    assert journal.find_by_key("implicit:owner:w") is not None
    assert journal.purge_expired() == 0
    now[0] += 2
    assert journal.find_by_key("implicit:owner:w") is None
    assert journal.purge_expired() == 1


def test_the_orphan_sweep_removes_inputs_without_a_row_and_keeps_those_of_an_accepted_intent(
    vault: Path, tmp_path: Path
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"data")
    journal = _journal(vault)
    kept, _ = journal.accept(**_ACCEPT, retry_key="k", inputs=[InputFile("f", source)])
    orphan = journal.inputs_root / ("0" * 32)
    orphan.mkdir()
    (orphan / "f").write_bytes(b"stranded")
    old = journal.clock() - 2 * write_intents.ORPHAN_GRACE_SECONDS
    for directory in (orphan, journal.inputs_root / kept.intent_id):
        os.utime(directory, (old, old))

    removed = journal.sweep_orphans()

    assert removed == [orphan.name]
    assert journal.input_path(kept.intent_id, "f").read_bytes() == b"data"


def test_the_orphan_sweep_spares_staging_that_an_accept_may_still_commit(vault: Path) -> None:
    journal = _journal(vault)
    fresh = journal.inputs_root / ("1" * 32)
    fresh.mkdir()

    assert journal.sweep_orphans() == []
    assert fresh.exists()
