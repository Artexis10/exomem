"""Regression coverage for the keyword lane's watcher-live cold path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore
from exomem.vault import walk_vault_md


pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5/trigram"
)


def _write_page(
    root: Path,
    rel: str,
    body: str,
    *,
    updated: str = "2026-08-01",
) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.stem
    path.write_text(
        f"---\ntype: insight\ntitle: {title}\nupdated: {updated}\n---\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_live(root: Path) -> None:
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(kb)
        ),
    )


def _materialize_live_catalog(root: Path, query: str) -> None:
    _seed_live(root)
    checkpoint = freshness.recall_checkpoint(root, "kb")
    assert (
        lexstore.search_substring(
            root,
            query,
            scope="kb",
            freshness=checkpoint.triple,
            repair=True,
        )
        is not None
    )


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()


def test_one_write_reconciles_keyword_catalog_in_proportion_to_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "stablemarker oldkeywordpayload",
        updated="2026-08-20",
    )
    for index in range(79):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/filler-{index:03d}.md",
            f"stablemarker unrelated payload {index}",
        )
    _materialize_live_catalog(tmp_path, "oldkeywordpayload")

    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "oldkeywordpayload", "newkeywordpayload"
        ),
        encoding="utf-8",
    )
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    freshness.on_files_changed(tmp_path, changed=[target])
    find_module._CACHE.clear()

    walked = 0
    parsed = 0
    delta_calls = 0
    original_walk = find_module._walk_md
    original_get = find_module._CACHE.get
    original_delta = freshness.recall_delta_since

    def counted_walk(root: Path):
        nonlocal walked
        for path in original_walk(root):
            walked += 1
            yield path

    def counted_get(*args, **kwargs):
        nonlocal parsed
        parsed += 1
        return original_get(*args, **kwargs)

    def counted_delta(*args, **kwargs):
        nonlocal delta_calls
        delta_calls += 1
        return original_delta(*args, **kwargs)

    monkeypatch.setattr(find_module, "_walk_md", counted_walk)
    monkeypatch.setattr(find_module._CACHE, "get", counted_get)
    monkeypatch.setattr(freshness, "recall_delta_since", counted_delta)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    paths = find_module._keyword_match_paths(
        tmp_path,
        "newkeywordpayload",
        "kb",
        freshness=checkpoint.triple,
        repair=False,
    )

    assert paths == ["Knowledge Base/Notes/target.md"]
    assert walked == 0
    assert parsed <= 1
    assert delta_calls == 1


def test_declining_sidecar_still_returns_the_reference_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/newer.md",
        "sharedneedle payload",
        updated="2026-08-20",
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/older.md",
        "sharedneedle payload",
        updated="2026-08-01",
    )
    _materialize_live_catalog(tmp_path, "sharedneedle")
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    healthy = find_module._keyword_match_paths(
        tmp_path,
        "sharedneedle",
        "kb",
        freshness=checkpoint.triple,
        repair=False,
    )

    monkeypatch.setattr(lexstore, "search_substring", lambda *args, **kwargs: None)
    declined = find_module._keyword_match_paths(
        tmp_path,
        "sharedneedle",
        "kb",
        freshness=checkpoint.triple,
        repair=False,
    )

    assert declined == healthy == [
        "Knowledge Base/Notes/newer.md",
        "Knowledge Base/Notes/older.md",
    ]


def test_access_policy_change_is_not_served_from_a_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Private/secret.md",
        "policyneedle private",
        updated="2026-08-20",
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/public.md",
        "policyneedle public",
        updated="2026-08-01",
    )
    _materialize_live_catalog(tmp_path, "policyneedle")
    store = lexstore.get_store(tmp_path)

    (tmp_path / "Knowledge Base/_access.yaml").write_text(
        "excluded:\n  - Notes/Private\n", encoding="utf-8"
    )

    def forbidden_delta_apply(*args, **kwargs):
        raise AssertionError("an access-policy transition must take the full path")

    monkeypatch.setattr(store, "_apply_delta_rows", forbidden_delta_apply)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")

    paths = find_module._keyword_match_paths(
        tmp_path,
        "policyneedle",
        "kb",
        freshness=checkpoint.triple,
        repair=False,
    )

    assert paths == ["Knowledge Base/Notes/public.md"]


def test_incomplete_delta_takes_the_full_reference_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "stablemarker beforedelta",
    )
    for index in range(4):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/filler-{index}.md",
            f"stablemarker filler {index}",
        )
    _materialize_live_catalog(tmp_path, "beforedelta")

    target.write_text(
        target.read_text(encoding="utf-8").replace("beforedelta", "afterdelta"),
        encoding="utf-8",
    )
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    freshness.on_files_changed(tmp_path, changed=[target])

    original_delta = freshness.recall_delta_since
    original_walk = find_module._walk_md
    walked = 0

    def incomplete(*args, **kwargs):
        delta = original_delta(*args, **kwargs)
        return delta._replace(
            complete=False,
            changed=frozenset(),
            deleted=frozenset(),
            target_signatures=(),
        )

    def counted_walk(root: Path):
        nonlocal walked
        for path in original_walk(root):
            walked += 1
            yield path

    monkeypatch.setattr(freshness, "recall_delta_since", incomplete)
    monkeypatch.setattr(find_module, "_walk_md", counted_walk)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")

    paths = find_module._keyword_match_paths(
        tmp_path,
        "afterdelta",
        "kb",
        freshness=checkpoint.triple,
        repair=False,
    )

    assert paths == ["Knowledge Base/Notes/target.md"]
    assert walked == 5
