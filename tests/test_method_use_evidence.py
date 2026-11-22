"""The owner readout says whether a served method was then used, from the served rows and the local logs.

Before, every readout said `unknown` for every method, so nobody could tell an activated method from an ignored one.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem import method_use, served_context

SKILL = "Knowledge Base/Skills/signal-triage/SKILL.md"
MANIFEST = "Knowledge Base/Records/Engineering/Signals/_collection.md"
ITEM_NOTE = "Knowledge Base/Notes/Insights/unrelated.md"


def _vault(root: Path, *, links: bool = True) -> None:
    for path, text in {
        SKILL: "---\nname: signal-triage\ndescription: Triage a new tool for a project.\n---\n\n"
        + ("1. Record it in [[Knowledge Base/Records/Engineering/Signals/_collection]].\n" if links else "1. Think.\n"),
        MANIFEST: "---\ntype: collection\ntitle: Signals\n---\n\nOne item per signal.\n",
        ITEM_NOTE: "---\ntype: insight\ntitle: Unrelated\n---\n\nNothing.\n",
    }.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")


def _served(root: Path, at: float) -> Path:
    """One retained row that served the skill at `at`."""
    with served_context._connect(root) as connection:  # noqa: SLF001 - the table's own writer
        connection.execute(
            "INSERT INTO served_context (id, timestamp, principal, view, map_unchanged, stance_refs, slice_refs, upkeep,"
            " task_context) VALUES (?, ?, 'owner', 'maps', 0, '[]', '[]', '[]', ?)",
            ("row-1", at, json.dumps({"methods": {"items": [{"ref": SKILL, "source_ref": SKILL, "read_ref": SKILL}]}})),
        )
    return served_context.sidecar_path(root)


def _log(directory: Path, name: str, field: str, events: list[tuple[float, str]]) -> None:
    lines = [
        json.dumps({"ts_utc": datetime.fromtimestamp(when, UTC).isoformat(), field: path, "outcome": "success"})
        for when, path in events
    ]
    (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(directory))
    return directory


def _evidence(root: Path, sidecar: Path, *, disabled: bool = False) -> dict:
    (entry,) = method_use.report(root, sidecar, resolver_for=lambda: None, disabled=disabled)["methods"]
    return entry


def test_a_write_into_the_linked_collection_after_serving_reads_followed(vault: Path, logs: Path) -> None:
    _vault(vault)
    now = time.time()
    # Served three hours ago, so its two-hour window has closed.
    sidecar = _served(vault, now - 3 * 3600)
    _log(logs, "writes.jsonl", "written_path", [(now - 4 * 3600, ITEM_NOTE), (now - 3 * 3600 + 600, MANIFEST)])
    _log(logs, "reads.jsonl", "read_path", [(now - 4 * 3600, ITEM_NOTE)])

    entry = _evidence(vault, sidecar)

    assert entry["served"] == 1
    assert entry["followed"]["observed"] == 1
    assert entry["opened"]["not_observed"] == 1


def test_no_later_read_or_write_reads_not_observed_never_unknown(vault: Path, logs: Path) -> None:
    _vault(vault)
    now = time.time()
    sidecar = _served(vault, now - 3 * 3600)
    # Both logs began before the serving and the window has closed, so a missing event is a real absence.
    _log(logs, "writes.jsonl", "written_path", [(now - 4 * 3600, ITEM_NOTE), (now - 3 * 3600 + 600, ITEM_NOTE)])
    _log(logs, "reads.jsonl", "read_path", [(now - 4 * 3600, ITEM_NOTE)])

    entry = _evidence(vault, sidecar)

    assert entry["followed"]["not_observed"] == 1
    assert entry["opened"]["not_observed"] == 1


def test_disabled_logs_and_a_method_with_no_destination_read_unknown(vault: Path, logs: Path) -> None:
    _vault(vault, links=False)
    now = time.time()
    sidecar = _served(vault, now - 3600)
    _log(logs, "writes.jsonl", "written_path", [(now - 7200, ITEM_NOTE)])
    _log(logs, "reads.jsonl", "read_path", [(now - 7200, ITEM_NOTE)])

    assert _evidence(vault, sidecar)["followed"]["unknown"] == 1
    disabled = _evidence(vault, sidecar, disabled=True)
    assert disabled["opened"]["unknown"] == 1 and disabled["followed"]["unknown"] == 1


def test_a_write_into_a_served_collection_itself_reads_followed_and_an_open_window_reads_unknown(
    vault: Path, logs: Path
) -> None:
    """A selected collection's own item writes were never counted, and a window still open read as not observed."""
    _vault(vault)
    now = time.time()
    with served_context._connect(vault) as connection:  # noqa: SLF001 - the table's own writer
        for row_id, at in (("closed", now - 3600 * 3), ("open", now - 600)):
            connection.execute(
                "INSERT INTO served_context (id, timestamp, principal, view, map_unchanged, stance_refs, slice_refs, upkeep,"
                " task_context) VALUES (?, ?, 'owner', 'maps', 0, '[]', '[]', '[]', ?)",
                (row_id, at, json.dumps({"methods": {"items": [{"ref": MANIFEST, "source_ref": MANIFEST, "read_ref": MANIFEST}]}})),
            )
    _log(logs, "writes.jsonl", "written_path", [(now - 3600 * 4, ITEM_NOTE), (now - 3600 * 3 + 60, MANIFEST)])
    _log(logs, "reads.jsonl", "read_path", [(now - 3600 * 4, ITEM_NOTE)])

    (entry,) = method_use.report(vault, served_context.sidecar_path(vault), resolver_for=lambda: None, disabled=False)["methods"]

    assert entry["followed"]["observed"] == 1  # the closed window saw a write into the collection
    assert entry["followed"]["unknown"] == 1  # the open window has seen nothing yet
