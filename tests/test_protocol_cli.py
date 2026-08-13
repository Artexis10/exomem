from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_selftest_fixtures_loads_the_selected_packaged_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--fixtures must execute fixture data, not merely accept an unused flag."""

    from protocol import cli

    fixture = tmp_path / "selftest.json"
    fixture.write_text(json.dumps({"gold": {"case_id": "fixture", "answer": "violet cedar lantern", "answer_session_ids": ["answer_fixture"], "question_type": "knowledge-update", "question": "Which lantern?"}, "content_fields": ["plain source"], "authored_literals": {"title": "case {case}"}, "harness_fields": {"title": "case 1", "tags": ["longmemeval"]}, "canary_hits": {"presence": True, "cross_case": False, "never_ingested": False}, "update_hits": ["current"]}), encoding="utf-8")
    monkeypatch.setattr(cli, "_FIXTURE_PATH", fixture)
    assert cli.main(["selftest", "--fixtures"]) == 0
