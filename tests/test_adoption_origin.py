"""A Source filed from a stage that states an origin says where the file came from.

OpenSpec file-imports-on-the-cell, group 2. Each test names the failure it catches.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_series import _window

from exomem import import_stage, page_fields
from exomem.vault import parse_frontmatter
from exomem.vocabulary import registry

MODIFIED = 1_700_000_000  # 2023-11-14T22:13:20Z
ORIGIN = {"label": "Phone photos", "device_family": "phone", "browser_family": "safari"}
FILES = {"a.md": b"# A\n", "paper.pdf": consume.PDF}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _stage(vault: Path, *, origin: dict | None) -> str:
    """A staged window of two files whose modification time the sender set, with an origin in its series file or none."""
    import_id = _window(vault, FILES, series=list(FILES))
    stage = vault / "_Imports" / import_id
    for name in FILES:
        os.utime(stage / name, (MODIFIED, MODIFIED))
    if origin is not None:
        (stage / import_stage.SERIES_FILE).write_text(
            json.dumps({"label": "Field Notes", "paths": list(FILES), "filing": "cell", "origin": origin}))
    return import_id


def _filed_pages(vault: Path, import_id: str) -> list[dict]:
    """The frontmatter of each Source that filing wrote from the stage."""
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    reply = consume._apply(vault, run_id, plan_id)
    pages = [vault / outcome["target_path"] for outcome in reply["outcomes"].values() if outcome["status"] == "applied"]
    assert len(pages) == len(FILES)
    return [parse_frontmatter(page.read_text(encoding="utf-8"))[0] for page in pages]


def test_each_source_of_a_stage_with_an_origin_carries_the_four_provenance_fields(vault: Path) -> None:
    """The text Source and the artifact Source are rendered by two writers, so each needs the fields."""
    frontmatters = _filed_pages(vault, _stage(vault, origin=ORIGIN))

    for frontmatter in frontmatters:
        assert frontmatter["origin_label"] == "Phone photos"
        assert frontmatter["upload_device"] == "phone"
        assert frontmatter["upload_browser"] == "safari"
        assert frontmatter["file_modified"] == "2023-11-14T22:13:20Z"


def test_a_retired_provenance_entry_is_not_written(vault: Path) -> None:
    """An owner who retires the device entry must stop it reaching a Source, with no code change."""
    overlay = page_fields.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries:\n  upload-device:\n    status: deprecated\n", encoding="utf-8")
    registry.invalidate(vault)

    frontmatters = _filed_pages(vault, _stage(vault, origin=ORIGIN))

    for frontmatter in frontmatters:
        assert "upload_device" not in frontmatter
        assert frontmatter["upload_browser"] == "safari"


def test_a_stage_without_an_origin_gets_no_provenance_field(vault: Path) -> None:
    """An older stage's mtime was never set by its sender: stamping it would show a wrong date as the file time."""
    frontmatters = _filed_pages(vault, _stage(vault, origin=None))

    for frontmatter in frontmatters:
        assert not frontmatter.keys() & {"origin_label", "upload_device", "upload_browser", "file_modified"}
