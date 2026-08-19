"""log.md size-triggered rotation: capped live log, byte-exact archives.

Every write op reads + rewrites log.md whole, so an unbounded activity log
makes every write O(log size). Rotation moves the tail beyond the newest
`LOG_ROTATE_KEEP_ENTRIES` entries into `Knowledge Base/_archive/logs/`
(excluded from find/index walks AND the incremental index paths) — append-only
history is preserved byte-exact, just relocated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import vault as vault_module
from exomem.vault import LOG_ROTATE_KEEP_ENTRIES, rotate_log_if_needed

HEADER = "# Knowledge Base activity log\n\nNewest first.\n\n---\n"


def _entry(i: int) -> str:
    return f"## [2026-01-01] note | Notes/probe-{i:04d}\n\nEntry body {i}.\n\n"


@pytest.fixture(params=["\n", "\r\n"], ids=["lf", "crlf"])
def log_newline(request: pytest.FixtureRequest) -> str:
    """Exercise both line endings a real vault carries.

    `log.md` is an ordinary markdown file in the user's vault, and any
    Windows editor that rewrites it leaves CRLF behind. `read_guarded_text`
    decodes raw bytes without newline translation, so rotation sees exactly
    what is on disk. Seeding through `write_text` hid that asymmetry: it
    emitted CRLF only on Windows, and `read_text` normalized it away again on
    the assertion side.
    """
    return str(request.param)


def _seed_log(vault: Path, n_entries: int, newline: str) -> tuple[Path, str]:
    log_file = vault / "Knowledge Base" / "log.md"
    entries = "".join(_entry(i) for i in range(n_entries))
    text = HEADER + entries
    log_file.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return log_file, text


def test_noop_under_threshold(vault: Path, log_newline: str) -> None:
    log_file, original = _seed_log(vault, 10, log_newline)
    assert rotate_log_if_needed(vault) is None
    assert log_file.read_text(encoding="utf-8") == original


def test_rotation_keeps_newest_and_archives_tail_byte_exact(
    vault: Path, monkeypatch: pytest.MonkeyPatch, log_newline: str
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    n = LOG_ROTATE_KEEP_ENTRIES + 37
    log_file, original = _seed_log(vault, n, log_newline)

    note = rotate_log_if_needed(vault)
    assert note and "_archive/logs/" in note

    live = log_file.read_text(encoding="utf-8")
    assert live.startswith(HEADER)
    assert live.count("## [") == LOG_ROTATE_KEEP_ENTRIES
    assert "Notes/probe-0000" in live  # newest-first: entry 0 is at the top

    archives = sorted((vault / "Knowledge Base" / "_archive" / "logs").glob("log-*.md"))
    assert len(archives) == 1
    archived = archives[0].read_text(encoding="utf-8")
    assert archived.count("## [") == 37
    # Byte-exact preservation: live entries + archived tail == original entries.
    live_entries = live[len(HEADER):]
    tail_start = archived.find("## [")
    assert HEADER + live_entries + archived[tail_start:] == original


def test_second_rotation_is_noop_at_entry_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch, log_newline: str
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    _seed_log(vault, LOG_ROTATE_KEEP_ENTRIES + 5, log_newline)
    assert rotate_log_if_needed(vault) is not None
    # Still over the byte threshold, but at the entry-count floor: no-op.
    assert rotate_log_if_needed(vault) is None


def test_write_log_entry_triggers_rotation(
    vault: Path, monkeypatch: pytest.MonkeyPatch, log_newline: str
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    _seed_log(vault, LOG_ROTATE_KEEP_ENTRIES + 5, log_newline)
    warning = vault_module.write_log_entry(
        vault,
        date_iso="2026-07-04",
        op="edit",
        rel_path_no_ext="Knowledge Base/Notes/probe-0001",
        body="rotation trigger probe",
    )
    assert warning is None
    archives = list((vault / "Knowledge Base" / "_archive" / "logs").glob("log-*.md"))
    assert archives, "write_log_entry should have triggered a rotation"
    live = (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")
    assert "rotation trigger probe" in live  # the new entry stays live


def test_log_rotation_plan_is_pure_deterministic_and_replay_stable(
    vault: Path, monkeypatch: pytest.MonkeyPatch, log_newline: str
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    _seed_log(vault, LOG_ROTATE_KEEP_ENTRIES + 5, log_newline)
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }

    kwargs = {
        "date_iso": "2026-07-14",
        "op": "note",
        "rel_path_no_ext": "Knowledge Base/Notes/Insights/replayable",
        "body": "Stable log operation.",
        "operation_token": "semantic-create:00000000-0000-4000-8000-000000000001",
    }
    first = vault_module.plan_log_writes(vault, **kwargs)
    second = vault_module.plan_log_writes(vault, **kwargs)

    assert first == second
    assert len(first.writes) == 2
    assert first.writes[0].path.parent.name == "logs"
    assert first.writes[1].path.name == "log.md"
    assert before == {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }

    vault_module.batch_atomic_write(first.writes, vault_root=vault)
    replay = vault_module.plan_log_writes(vault, **kwargs)
    assert tuple(write.path for write in replay.writes) == tuple(
        write.path for write in first.writes
    )
    assert tuple(write.content for write in replay.writes) == tuple(
        write.content for write in first.writes
    )


def test_log_entry_insertion_matches_the_logs_own_line_endings(log_newline: str) -> None:
    """A CRLF log must still insert newest-first, once, without mixing endings.

    `prepend_log_entry` proves idempotency by asking whether the rendered entry
    is already present. Rendering with LF against a CRLF log never matches, so a
    replayed write appended a duplicate; the separator lookup missed for the same
    reason and put the entry at the bottom, silently inverting newest-first.
    """
    log = (HEADER + _entry(0)).replace("\n", log_newline)
    kwargs = {
        "date_iso": "2026-08-19",
        "op": "edit",
        "rel_path_no_ext": "Knowledge Base/Notes/probe",
        "body": "probe body",
    }

    once = vault_module.prepend_log_entry(log, **kwargs)
    twice = vault_module.prepend_log_entry(once, **kwargs)

    assert once == twice, "replaying the same entry must not duplicate it"
    body = once[len(HEADER.replace("\n", log_newline)) :]
    assert body.lstrip(log_newline).startswith("## [2026-08-19]"), "newest entry goes first"
    naked_lf = once.replace("\r\n", "")
    assert ("\n" in naked_lf) is (log_newline == "\n"), "no mixed line endings"
