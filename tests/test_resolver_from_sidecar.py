"""Recall resolver construction from the lexical sidecar."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import call_spans, freshness, lexstore, recall_policy
from exomem import find as find_module
from exomem.vault import walk_vault_md


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    freshness.clear()
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    yield
    freshness.clear()
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


def _expected_entries(root: Path) -> set[tuple[str, str | None]]:
    entries: set[tuple[str, str | None]] = set()
    for path in walk_vault_md(root):
        if not recall_policy.is_recall_candidate(root, path):
            continue
        page = find_module._CACHE.get(path, root)
        if page is not None:
            entries.add((page.rel_path, page.title))
    return entries


def _seed_sidecar(root: Path) -> None:
    _seed_live_freshness(root)
    lexstore.ensure_fresh(root)
    find_module.clear_cache()
    find_module._RECALL_RESOLVER_CACHE.clear()


def _resolver_entries(resolver) -> set[tuple[str, str | None]]:
    return {
        (rel + ".md", resolver.title_key_for_path(rel))
        for rel in resolver.full_paths
    }


def test_sidecar_entries_match_walk_and_preserve_exact_title_bytes(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(
        root,
        "Knowledge Base/Notes/Mixed.md",
        "---\ntitle: \"MiXeD, punctuation: café ☃\"\n---\n# ignored heading\n",
    )
    _write(root, "Knowledge Base/Notes/Plain.md", "# Plain title\n")
    _seed_sidecar(root)

    store = lexstore.get_store(root)
    entries = store.recall_resolver_entries(
        "vault", freshness.recall_checkpoint(root, "vault")
    )

    assert entries is not None
    assert set(entries) == _expected_entries(root)
    assert ("Knowledge Base/Notes/Mixed.md", "MiXeD, punctuation: café ☃") in entries
    assert _resolver_entries(find_module.recall_resolver_snapshot(root)) == {
        (path, title.lower() if title else None) for path, title in entries
    }


def test_resolver_build_from_current_sidecar_does_not_read_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Notes/One.md", "# One\n")
    _write(root, "Knowledge Base/Notes/Two.md", "# Two\n")
    _seed_sidecar(root)

    reads = 0
    real_get = find_module._CACHE.get

    def counting_get(path: Path, vault_root: Path):
        nonlocal reads
        reads += 1
        return real_get(path, vault_root)

    monkeypatch.setattr(find_module._CACHE, "get", counting_get)
    resolver = find_module.recall_resolver_snapshot(root)

    assert resolver is not None
    assert reads == 0


def test_resolver_timing_separates_build_and_warm_copy_work(tmp_path: Path) -> None:
    """A warm copy must not look like another full resolver build in diagnostics."""
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Notes/One.md", "# One\n")
    _write(root, "Knowledge Base/Notes/Two.md", "# Two\n")
    _seed_sidecar(root)
    token = "resolver-build-and-hit"
    handle = call_spans.MCP_CALL_TOKEN.set(token)
    try:
        built = find_module.recall_resolver_snapshot(root)
        cached = find_module.recall_resolver_snapshot(root)
        rows = {row["name"]: row for row in call_spans.pop_call_spans(token)}
    finally:
        call_spans.pop_call_spans(token)
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert _resolver_entries(cached) == _resolver_entries(built)
    assert rows["resolver.acquire"]["count"] == 2
    copied = rows["resolver.fork"]
    assert copied["count"] == 2
    fields = {
        key: value for key, value in copied["fields"].items()
        if key not in call_spans.CPU_FIELDS
    }
    assert fields == {"build_returns": 1, "cache_hits": 1, "paths": 4}
    assert 0 <= copied["ms"] <= rows["resolver.acquire"]["ms"]


def test_sidecar_title_null_round_trips_and_exact_title_survives(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = _write(
        root,
        "Knowledge Base/Notes/Exact.md",
        "---\ntitle: MiXeD -- café / 東京\n---\n# ignored\n",
    )
    _seed_sidecar(root)
    store = lexstore.get_store(root)
    rel = page.relative_to(root).as_posix()

    assert store.recall_resolver_entries(
        "vault", freshness.recall_checkpoint(root, "vault")
    ) == [
        (rel, "MiXeD -- café / 東京")
    ]
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            conn.execute("UPDATE pages SET title = NULL WHERE path = ?", (rel,))
    finally:
        conn.close()

    assert store.recall_resolver_entries(
        "vault", freshness.recall_checkpoint(root, "vault")
    ) == [
        (rel, None)
    ]


@pytest.mark.parametrize("state", ["absent", "stale", "wrong-version"])
def test_unusable_sidecar_falls_back_to_complete_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    _seed_sidecar(root)
    store = lexstore.get_store(root)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        lexstore,
        "_schedule_repair",
        lambda vault_root, **_kwargs: scheduled.append(vault_root),
    )

    if state == "absent":
        store.path.unlink()
    else:
        conn = sqlite3.connect(store.path)
        try:
            with conn:
                if state == "stale":
                    conn.execute(
                        "UPDATE meta SET value = ? WHERE key = 'recall_checkpoint:vault'",
                        ("not a checkpoint",),
                    )
                else:
                    conn.execute(
                        "UPDATE meta SET value = ? WHERE key = 'schema_version'", ("0",)
                    )
        finally:
            conn.close()

    reads = 0
    real_get = find_module._CACHE.get

    def counting_get(path: Path, vault_root: Path):
        nonlocal reads
        reads += 1
        return real_get(path, vault_root)

    monkeypatch.setattr(find_module._CACHE, "get", counting_get)
    resolver = find_module.recall_resolver_snapshot(root)

    assert reads > 0
    assert scheduled == []
    assert _resolver_entries(resolver) == {
        (path, title.lower() if title else None) for path, title in _expected_entries(root)
    }


def test_access_policy_change_is_not_served_from_stale_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    _seed_sidecar(root)

    access = root / "Knowledge Base" / "_access.yaml"
    access.write_text("excluded:\n  - Notes\n", encoding="utf-8")
    find_module._RECALL_RESOLVER_CACHE.clear()

    resolver = find_module.recall_resolver_snapshot(root)

    assert "Knowledge Base/Notes/Visible" not in resolver.full_paths


def test_sidecar_unknown_page_falls_back_and_still_resolves(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Notes/Known.md", "# Known\n")
    _seed_sidecar(root)
    unknown = _write(root, "Knowledge Base/Notes/Unknown.md", "# Unknown\n")
    freshness.on_files_changed(root, changed=[unknown])
    find_module._RECALL_RESOLVER_CACHE.clear()

    resolver = find_module.recall_resolver_snapshot(root)

    assert "Knowledge Base/Notes/Unknown" in resolver.full_paths
