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


def _seed_log(vault: Path, n_entries: int) -> tuple[Path, str]:
    log_file = vault / "Knowledge Base" / "log.md"
    entries = "".join(_entry(i) for i in range(n_entries))
    text = HEADER + entries
    log_file.write_text(text, encoding="utf-8")
    return log_file, text


def test_noop_under_threshold(vault: Path) -> None:
    log_file, original = _seed_log(vault, 10)
    assert rotate_log_if_needed(vault) is None
    assert log_file.read_text(encoding="utf-8") == original


def test_rotation_keeps_newest_and_archives_tail_byte_exact(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    n = LOG_ROTATE_KEEP_ENTRIES + 37
    log_file, original = _seed_log(vault, n)

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
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    _seed_log(vault, LOG_ROTATE_KEEP_ENTRIES + 5)
    assert rotate_log_if_needed(vault) is not None
    # Still over the byte threshold, but at the entry-count floor: no-op.
    assert rotate_log_if_needed(vault) is None


def test_write_log_entry_triggers_rotation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1000")
    _seed_log(vault, LOG_ROTATE_KEEP_ENTRIES + 5)
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
