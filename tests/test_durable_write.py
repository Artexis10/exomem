"""move-cloud-cells-to-local-storage D9: a cell's block snapshot is the state a
power cut would leave, so its small state files are synced before they
replace the old ones, and a truncated config never resets a tenant's settings.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import dreamer, durable_write, envelope, mode, prominence, writer_lease
from exomem.governance import transaction


def test_new_content_is_synced_before_it_replaces_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        events.append(("fsync", Path(os.readlink(f"/proc/self/fd/{fd}")).read_text(encoding="utf-8")))
        real_fsync(fd)

    def replace(source, destination) -> None:
        events.append(("replace", Path(destination).name))
        real_replace(source, destination)

    monkeypatch.setattr(durable_write.os, "fsync", fsync)
    monkeypatch.setattr(durable_write.os, "replace", replace)

    durable_write.replace_text(target, "new")

    assert events == [("fsync", "new"), ("replace", "config.json")]
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "write",
    [
        lambda: mode.write_mode("quiet"),
        lambda: prominence.write_prominence(prominence.CANON[0]),
        lambda: dreamer.write_setting(dreamer.policy.SETTINGS[0]),
        lambda: envelope._write_envelope({}),
        lambda: writer_lease._bump_commit_generation(Path(os.environ["EXOMEM_TEST_STATE_DIR"]), "/vault"),
        lambda: transaction.durable_json(Path(os.environ["EXOMEM_TEST_STATE_DIR"]) / "marker.json", {}),
    ],
    ids=["mode", "prominence", "dreamer", "envelope", "commit-counter", "governance"],
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
