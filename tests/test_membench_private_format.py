"""Founder-regression format: valid records, safe defaults, git exclusion."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from membench.private_regressions import (
    FounderRegression,
    fixtures_path,
    load_regressions,
    replay_activation_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _record(**overrides) -> FounderRegression:
    payload = dict(
        id="fr-001",
        recorded_on="2026-07-31",
        natural_prompt="what did we decide about the ingress migration?",
        should_activate=True,
        observed_result="answered from memory without prompting",
        privacy_class="P2",
    )
    payload.update(overrides)
    return FounderRegression(**payload)


def test_record_validates_and_rejects_extras() -> None:
    record = _record()
    assert record.synthetic_convertible is False
    with pytest.raises(ValidationError):
        FounderRegression(**{**record.model_dump(), "surprise": 1})
    with pytest.raises(ValidationError):
        _record(privacy_class="P9")


def test_private_dir_is_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "benchmarks/private/" in gitignore.splitlines()


def test_load_and_replay_respect_privacy(tmp_path: Path) -> None:
    assert load_regressions(tmp_path) == []  # absent file is a normal state
    target = fixtures_path(tmp_path)
    target.parent.mkdir(parents=True)
    lines = [
        _record().model_dump_json(),
        _record(id="fr-002", privacy_class="P0").model_dump_json(),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = load_regressions(tmp_path)
    assert [r.id for r in records] == ["fr-001", "fr-002"]
    replayable = replay_activation_cases(tmp_path)
    assert [item["id"] for item in replayable] == ["fr-001"]  # P0 never leaves