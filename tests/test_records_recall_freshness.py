from __future__ import annotations

from pathlib import Path

from exomem import freshness, recall_policy


def test_live_recall_checkpoint_reuses_projected_map_without_rewalking(
    tmp_path: Path, monkeypatch
) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    freshness.seed(tmp_path, "kb", [(str(page), freshness.stat_signature(page))])
    first = freshness.recall_checkpoint(tmp_path, "kb")

    monkeypatch.setattr(
        recall_policy,
        "is_recall_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not walk or read")),
    )

    assert freshness.recall_checkpoint(tmp_path, "kb") == first
