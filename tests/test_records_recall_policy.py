from __future__ import annotations

from pathlib import Path

import pytest

from exomem import bm25, freshness, recall_policy
from exomem.structured_collections import CollectionError, load_manifest


def _manifest() -> str:
    return """---
type: collection
exomem_id: 12345678-1234-4abc-8def-123456789abc
title: Measurements
semantic_profile: records
collection_version: 1
lifecycle: active
schema_version: 1
storage:
  strategy: markdown-items
  format_version: 1
  source: items
item_schema:
  natural_key: [observed]
  fields:
    observed:
      type: string
---
"""


def test_only_valid_exact_records_manifest_is_an_ordinary_recall_candidate(tmp_path: Path) -> None:
    records = tmp_path / "Knowledge Base" / "Records" / "Health"
    records.mkdir(parents=True)
    manifest = records / "_collection.md"
    manifest.write_text(_manifest(), encoding="utf-8")
    (records / "items").mkdir()
    raw_item = records / "items" / "raw.md"
    raw_item.write_text("raw confidential measurement", encoding="utf-8")
    summary = records / "_summary.md"
    summary.write_text("derived summary", encoding="utf-8")

    assert recall_policy.is_recall_candidate(tmp_path, manifest)
    assert not recall_policy.is_recall_candidate(tmp_path, raw_item)
    assert not recall_policy.is_recall_candidate(tmp_path, summary)


def test_records_manifest_outside_exact_records_layer_refuses(tmp_path: Path) -> None:
    collection = tmp_path / "Knowledge Base" / "Elsewhere"
    collection.mkdir(parents=True)
    manifest = collection / "_collection.md"
    manifest.write_text(_manifest(), encoding="utf-8")
    (collection / "items").mkdir()

    with pytest.raises(CollectionError, match="INVALID_COLLECTION_PATH"):
        load_manifest(tmp_path, manifest)


def test_bm25_uses_manifest_only_records_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    records = tmp_path / "Knowledge Base" / "Records" / "Health"
    records.mkdir(parents=True)
    (records / "_collection.md").write_text(_manifest() + "\nneedle manifest", encoding="utf-8")
    (records / "items").mkdir()
    for index in range(1_000):
        (records / "items" / f"{index}.md").write_text("needle raw item", encoding="utf-8")
    (records / "Training Log.md").write_text("needle log\n" * 100_000, encoding="utf-8")
    (records / "_summary.md").write_text("needle derived view", encoding="utf-8")
    other = tmp_path / "Knowledge Base" / "Notes"
    other.mkdir()
    (other / "other.md").write_text("unrelated page", encoding="utf-8")
    (other / "another.md").write_text("also unrelated", encoding="utf-8")

    bm25.clear_cache()
    assert [path for path, _score in bm25.search(tmp_path, "needle", k=5)] == [
        "Knowledge Base/Records/Health/_collection.md"
    ]


def test_raw_records_edits_do_not_move_recall_freshness(tmp_path: Path) -> None:
    records = tmp_path / "Knowledge Base" / "Records" / "Health"
    records.mkdir(parents=True)
    (records / "_collection.md").write_text(_manifest(), encoding="utf-8")
    (records / "items").mkdir()
    raw_item = records / "items" / "raw.md"
    raw_item.write_text("first", encoding="utf-8")

    before = freshness.recall_triple(tmp_path, "kb")
    raw_item.write_text("manual edit", encoding="utf-8")

    assert freshness.recall_triple(tmp_path, "kb") == before


@pytest.mark.parametrize(
    "relative",
    (
        "knowledge base/records/Health/_collection.md",
        "Knowledge Base/records/Health/_collection.md",
        "Knowledge Base/Records/Health/_COLLECTION.md",
    ),
)
def test_records_casefold_aliases_are_not_ordinary_candidates(
    tmp_path: Path, relative: str
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("ordinary-looking alias", encoding="utf-8")

    assert not recall_policy.is_recall_candidate(tmp_path, path)


def test_symlink_alias_cannot_admit_records_bytes_as_an_ordinary_page(tmp_path: Path) -> None:
    records = tmp_path / "Knowledge Base" / "Records" / "Health"
    records.mkdir(parents=True)
    raw_item = records / "raw.md"
    raw_item.write_text("raw record", encoding="utf-8")
    alias = tmp_path / "Knowledge Base" / "Elsewhere"
    try:
        alias.symlink_to(records, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks/reparse points are unavailable")

    assert not recall_policy.is_recall_candidate(tmp_path, alias / raw_item.name)


def test_canonical_alias_seam_suppresses_a_windows_short_name(tmp_path: Path, monkeypatch) -> None:
    note = tmp_path / "Knowledge Base" / "Elsewhere" / "SHORT~1.MD"
    note.parent.mkdir(parents=True)
    note.write_text("ordinary looking alias", encoding="utf-8")
    monkeypatch.setattr(recall_policy, "_needs_canonical_alias_check", lambda _parts: True)
    monkeypatch.setattr(
        recall_policy,
        "_canonical_parts_after_safe_validation",
        lambda _root, _parts: ["Knowledge Base", "Records", "Health", "raw.md"],
    )

    assert not recall_policy.is_recall_candidate(tmp_path, note)
