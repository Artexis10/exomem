"""Semantic-unit records in the rebuildable lexical sidecar."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import lexstore, vault

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)

_PAGE_ID = "11111111-1111-4111-8111-111111111111"
_PARENT_REF = f"exomem://memory/{_PAGE_ID}"


def _write_page(root: Path, body: str) -> Path:
    path = root / "Knowledge Base" / "Notes" / "units.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Unit index fixture\n"
        f"exomem_id: {_PAGE_ID}\n"
        "updated: 2026-07-15\n"
        "---\n"
        "# Unit index fixture\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )
    return path


def _rows(root: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(lexstore.lexical_path(root))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM semantic_units ORDER BY source_order").fetchall()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    monkeypatch.delenv("EXOMEM_LEXICAL_BACKEND", raising=False)
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()


def test_unit_rows_share_parent_generation_and_filter_exact_metadata(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        """\
## Observations
- [config] Same payload mentions decision ^cfg
- [rule] Same payload mentions decision ^rule

## Decision
- category: config
- id: rich-config

Use SQLite for the index.
""",
    )

    category_hits = lexstore.search_semantic_units(
        tmp_path, "", k=10, categories=["config"], scope="kb"
    )
    assert category_hits is not None
    assert [(hit.form, hit.category, hit.kind) for hit in category_hits] == [
        ("compact", "config", "observation"),
        ("rich", "config", "decision"),
    ]

    # Kind is governed semantic kind, not an open category or a content mention.
    kind_hits = lexstore.search_semantic_units(tmp_path, "", k=10, kinds=["decision"], scope="kb")
    assert kind_hits is not None
    assert [(hit.form, hit.category, hit.kind) for hit in kind_hits] == [
        ("rich", "config", "decision")
    ]

    same_text = lexstore.search_semantic_units(tmp_path, "same payload", k=10, scope="kb")
    assert same_text is not None
    assert {hit.category for hit in same_text} == {"config", "rule"}
    assert len({hit.unit_ref for hit in same_text}) == 2

    rows = _rows(tmp_path)
    assert len(rows) == 3
    assert {row["record_type"] for row in rows} == {"semantic_unit"}
    assert {row["parent_path"] for row in rows} == {"Knowledge Base/Notes/units.md"}
    assert {row["parent_ref"] for row in rows} == {_PARENT_REF}
    assert {row["parent_source_hash"] for row in rows} == {
        vault.content_hash(page.read_text(encoding="utf-8"))
    }
    assert len({row["parent_generation"] for row in rows}) == 1
    assert len(rows[0]["parent_generation"]) == 64
    assert len({row["parser_version"] for row in rows}) == 1
    assert rows[0]["parser_version"] >= 1


def test_parent_update_replaces_all_unit_rows_in_one_transaction(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        """\
- [config] oldtoken ^old
- [rule] survivor ^survivor
""",
    )
    assert lexstore.search_semantic_units(tmp_path, "oldtoken", k=10, scope="kb")
    before = _rows(tmp_path)
    before_generation = before[0]["parent_generation"]

    page = _write_page(tmp_path, "- [rule] newtoken ^new\n")
    lexstore.upsert_after_write(tmp_path, [page])

    assert lexstore.search_semantic_units(tmp_path, "oldtoken", k=10, scope="kb") == []
    assert (
        lexstore.search_semantic_units(tmp_path, "", k=10, categories=["config"], scope="kb") == []
    )
    current = lexstore.search_semantic_units(tmp_path, "newtoken", k=10, scope="kb")
    assert current is not None and [hit.category for hit in current] == ["rule"]
    after = _rows(tmp_path)
    assert len(after) == 1
    assert after[0]["parent_generation"] != before_generation

    # Force the replacement insert to abort after the parent delete. SQLite
    # must roll the transaction back, retaining the last complete generation.
    conn = sqlite3.connect(lexstore.lexical_path(tmp_path))
    try:
        conn.execute(
            "CREATE TRIGGER reject_broken_generation BEFORE INSERT ON semantic_units "
            "WHEN NEW.content = 'broken generation' "
            "BEGIN SELECT RAISE(ABORT, 'forced unit insert failure'); END"
        )
        conn.commit()
    finally:
        conn.close()
    page = _write_page(tmp_path, "- [rule] broken generation ^broken\n")
    find_module.clear_cache()
    with pytest.raises(sqlite3.IntegrityError, match="forced unit insert failure"):
        lexstore.get_store(tmp_path).upsert_paths([page])
    assert [row["content"] for row in _rows(tmp_path)] == ["newtoken"]


def test_parent_delete_removes_every_lexical_unit_row(tmp_path: Path) -> None:
    page = _write_page(tmp_path, "- [config] remove me ^gone\n")
    assert lexstore.search_semantic_units(tmp_path, "remove", k=10, scope="kb")

    page.unlink()
    lexstore.delete_after_remove(tmp_path, ["Knowledge Base/Notes/units.md"])

    assert _rows(tmp_path) == []
    assert (
        lexstore.search_semantic_units(tmp_path, "", k=10, categories=["config"], scope="kb") == []
    )
