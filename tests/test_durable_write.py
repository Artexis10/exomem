"""move-cloud-cells-to-local-storage D9: a cell's block snapshot is the state a
power cut would leave, so its small state files are synced before they
replace the old ones, and a truncated config never resets a tenant's settings.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import durable_write, mode, writer_lease
from exomem.governance import transaction


def test_new_content_is_synced_before_it_replaces_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    events: list[tuple[str, object]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        # The synced file already holds the whole new content.
        events.append(("fsync", os.fstat(fd).st_size))
        real_fsync(fd)

    def replace(source, destination) -> None:
        events.append(("replace", Path(destination).name))
        real_replace(source, destination)

    monkeypatch.setattr(durable_write.os, "fsync", fsync)
    monkeypatch.setattr(durable_write.os, "replace", replace)

    durable_write.replace_text(target, "new")

    assert events == [("fsync", len("new")), ("replace", "config.json")]
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name != "nt", reason="POSIX renames over an open file; there is nothing to wait out")
def test_a_reader_holding_the_file_open_on_windows_delays_the_write_instead_of_losing_it(tmp_path: Path) -> None:
    # writer_lease swallows a failed bump, which would leave a stale commit
    # generation; the rename must wait out a brief reader instead.
    import threading

    target = tmp_path / "commit-generation"
    target.write_text("1", encoding="utf-8")
    reader = open(target, encoding="utf-8")  # noqa: SIM115 - held open across the write on purpose
    threading.Timer(0.05, reader.close).start()

    durable_write.replace_text(target, "2")

    assert target.read_text(encoding="utf-8") == "2"


@pytest.mark.parametrize(
    "write",
    [
        # mode, prominence, dreamer and envelope all write the config through this one writer.
        lambda: mode.write_mode("quiet"),
        lambda: writer_lease._bump_commit_generation(Path(os.environ["EXOMEM_TEST_STATE_DIR"]), "/vault"),
        lambda: transaction.durable_json(Path(os.environ["EXOMEM_TEST_STATE_DIR"]) / "marker.json", {}),
    ],
    ids=["config", "commit-counter", "governance"],
)
def test_every_json_state_writer_goes_through_the_durable_path(
    write, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("EXOMEM_TEST_STATE_DIR", str(tmp_path / "state"))
    written: list[Path] = []
    real = durable_write.replace_text
    monkeypatch.setattr(durable_write, "replace_text", lambda path, text, **kw: (written.append(path), real(path, text, **kw)))

    write()

    assert len(written) == 1
