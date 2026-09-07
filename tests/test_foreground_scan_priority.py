"""Foreground-priority checkpoint integration seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import find_corpus, foreground_activity, recall_policy


def test_admission_and_parse_checkpoints_are_inert_without_background_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    page = vault / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    calls: list[Path] = []
    monkeypatch.setattr(foreground_activity, "checkpoint", lambda root: calls.append(Path(root)))

    recall_policy.is_recall_candidate(vault, page)
    find_corpus.parse_page(page, 0.0, vault)

    assert calls == [vault, vault]
