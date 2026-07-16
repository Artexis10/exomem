"""Lexical sidecar (.lexical.sqlite): schema/sync/migration, drift heal, lockstep,
and the two search primitives (FTS5 bm25, trigram substring).

Everything here is LEAN-SAFE: FTS5 and the trigram tokenizer ship inside CPython's
bundled SQLite — no extras, no model, no extension loading. Tests that exercise the
FTS5 path skip cleanly if this interpreter's SQLite genuinely lacks FTS5 (rare,
custom builds only); the fallback/kill-switch tests run everywhere.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import lexstore

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5/trigram"
)


# ---------------------------------------------------------------- helpers


def _write_page(
    root: Path,
    rel: str,
    body: str,
    *,
    title: str | None = None,
    updated: str = "2026-01-01",
    mtime: float | None = None,
) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    t = title or Path(rel).stem
    p.write_text(
        f"---\ntype: insight\ntitle: {t}\nupdated: {updated}\n---\n# {t}\n\n{body}\n",
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _fresh_lex_state(monkeypatch: pytest.MonkeyPatch):
    """Clean process-global memos, per-process store cache, and default env."""
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    monkeypatch.delenv("EXOMEM_LEXICAL_BACKEND", raising=False)
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()


# ---------------------------------------------------------------- env reader


def test_backend_env_reader_defaults(monkeypatch):
    assert lexstore.backend() == "auto"
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    assert lexstore.backend() == "python"
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    assert lexstore.backend() == "fts5"
    # A typo must not silently disable the kill switch someone reached for,
    # nor hard-fail search — unrecognized values mean `auto` (vecstore idiom).
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "bogus")
    assert lexstore.backend() == "auto"


# ---------------------------------------------------------------- schema + sync


def test_first_use_populates_sidecar_from_markdown(tmp_path):
    """A vault that predates the sidecar is indexed on first search — the
    migration path — and the search answers from the fresh index."""
    _write_page(tmp_path, "Knowledge Base/Notes/alpha.md", "the regulator files reports")
    _write_page(tmp_path, "Knowledge Base/Notes/beta.md", "unrelated body text")
    hits = lexstore.search_bm25(tmp_path, "regulator", k=5, scope="kb")
    assert hits is not None  # FTS5 served (not a fallback signal)
    assert [p for p, _ in hits] == ["Knowledge Base/Notes/alpha.md"]
    side = lexstore.lexical_path(tmp_path)
    assert side.exists()
    assert _count(side, "pages") == 2


def test_schema_creation_is_idempotent(tmp_path):
    _write_page(tmp_path, "Knowledge Base/a.md", "alpha body")
    assert lexstore.search_bm25(tmp_path, "alpha", k=3, scope="kb") is not None
    lexstore.clear_stores()  # fresh store object, same sidecar file
    assert lexstore.search_bm25(tmp_path, "alpha", k=3, scope="kb") is not None
    assert _count(lexstore.lexical_path(tmp_path), "pages") == 1


def test_out_of_band_edit_self_heals(tmp_path):
    """Markdown changed while no lexstore was watching (server down): the next
    use detects the count/mtime mismatch against the walk and rebuilds."""
    _write_page(tmp_path, "Knowledge Base/a.md", "original wording", mtime=1_000)
    assert lexstore.search_bm25(tmp_path, "original", k=3, scope="kb")
    lexstore.clear_stores()  # simulate a fresh process
    _write_page(tmp_path, "Knowledge Base/a.md", "replacement phrasing", mtime=2_000)
    _write_page(tmp_path, "Knowledge Base/b.md", "brand new page", mtime=2_000)
    hits = lexstore.search_bm25(tmp_path, "replacement", k=3, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/a.md"
    assert lexstore.search_bm25(tmp_path, "original", k=3, scope="kb") == []
    hits = lexstore.search_bm25(tmp_path, "brand", k=3, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/b.md"


def test_out_of_band_rename_self_heals(tmp_path):
    """A pure rename (os.replace preserves mtime; count unchanged) slips past
    count/max-mtime alone — the digest-strength bar the python rungs meet
    (test_bm25_sees_rename). Unwitnessed + unknown triple → exact verify →
    rebuild."""
    _write_page(tmp_path, "Knowledge Base/rename-old.md", "zanzibar quixotic marker")
    _write_page(tmp_path, "Knowledge Base/other.md", "unrelated filler")
    hits = lexstore.search_bm25(tmp_path, "zanzibar", k=5, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/rename-old.md"

    lexstore.clear_stores()  # fresh process; no hook witnessed anything
    os.replace(
        tmp_path / "Knowledge Base/rename-old.md",
        tmp_path / "Knowledge Base/rename-new.md",
    )
    hits = lexstore.search_bm25(tmp_path, "zanzibar", k=5, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/rename-new.md"
    assert lexstore.search_substring(tmp_path, "zanzibar", scope="kb") == [
        "Knowledge Base/rename-new.md"
    ]


def test_out_of_band_content_replacement_with_preserved_mtime_self_heals(tmp_path):
    page = _write_page(tmp_path, "Knowledge Base/a.md", "originaluniquetoken")
    before = page.stat()
    assert lexstore.search_bm25(tmp_path, "originaluniquetoken", k=3, scope="kb")
    lexstore.clear_stores()

    replacement = page.with_suffix(".replacement")
    replacement.write_text(
        "---\ntype: insight\n---\n# A\n\nreplacementuniquetoken with different bytes\n",
        encoding="utf-8",
    )
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, page)

    hits = lexstore.search_bm25(tmp_path, "replacementuniquetoken", k=3, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/a.md"
    assert lexstore.search_bm25(tmp_path, "originaluniquetoken", k=3, scope="kb") == []


def test_live_witness_does_not_bless_later_preserved_mtime_replacement(tmp_path):
    """A past dual-write is proof for exactly its own corpus triple, not future drift."""
    from exomem import freshness
    from exomem import vault as vault_module

    freshness.clear()
    try:
        page = _write_page(tmp_path, "Knowledge Base/a.md", "initialuniquetoken")
        assert lexstore.search_bm25(tmp_path, "initialuniquetoken", k=3, scope="kb")

        kb_dir = tmp_path / "Knowledge Base"
        freshness.seed(
            tmp_path,
            "kb",
            ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(kb_dir)),
        )
        freshness.seed(
            tmp_path,
            "vault",
            ((str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(tmp_path)),
        )

        page = _write_page(tmp_path, "Knowledge Base/a.md", "witnesseduniquetoken")
        freshness.on_files_changed(tmp_path, changed=[page])
        lexstore.upsert_after_write(tmp_path, [page])
        assert lexstore.search_bm25(tmp_path, "witnesseduniquetoken", k=3, scope="kb")

        before = page.stat()
        replacement = page.with_suffix(".replacement")
        replacement.write_text(
            "---\ntype: insight\n---\n# A\n\nlaterexternaluniquetoken with different bytes\n",
            encoding="utf-8",
        )
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, page)
        freshness.on_files_changed(tmp_path, changed=[page])

        hits = lexstore.search_bm25(tmp_path, "laterexternaluniquetoken", k=3, scope="kb")
        assert hits and hits[0][0] == "Knowledge Base/a.md"
        assert lexstore.search_bm25(tmp_path, "witnesseduniquetoken", k=3, scope="kb") == []
    finally:
        freshness.clear()


def test_restart_with_no_changes_skips_the_walk_verify(tmp_path):
    """Steady state across restarts: the meta-blessed triple lets a fresh
    store trust the sidecar without an exact verify (no rebuild)."""
    _write_page(tmp_path, "Knowledge Base/a.md", "steady content")
    assert lexstore.search_bm25(tmp_path, "steady", k=3, scope="kb")
    lexstore.clear_stores()

    calls = {"n": 0}
    real = lexstore.LexicalStore._rebuild

    def _counting(self, conn):
        calls["n"] += 1
        return real(self, conn)

    lexstore.LexicalStore._rebuild = _counting
    try:
        assert lexstore.search_bm25(tmp_path, "steady", k=3, scope="kb")
    finally:
        lexstore.LexicalStore._rebuild = real
    assert calls["n"] == 0


def test_manual_row_drift_self_heals(tmp_path, monkeypatch):
    """Highest page rowids deleted while their index rows remain are restored
    incrementally by the next fresh store."""
    for i in range(4):
        _write_page(
            tmp_path,
            f"Knowledge Base/n{i}.md",
            f"- [config] payload token{i} ^unit{i}",
        )
    assert lexstore.search_bm25(tmp_path, "payload", k=10, scope="kb")
    side = lexstore.lexical_path(tmp_path)
    assert _count(side, "semantic_units") == 4
    conn = sqlite3.connect(side)
    try:
        # Delete the highest rowids so SQLite will reuse them while the
        # contentless FTS tables still contain the orphaned rowids. The heal
        # must remove those orphans before reinserting the missing pages.
        conn.execute(
            "DELETE FROM pages WHERE rowid IN "
            "(SELECT rowid FROM pages ORDER BY rowid DESC LIMIT 2)"
        )
        conn.commit()
    finally:
        conn.close()
    assert _count(side, "pages") == 2
    assert _count(side, "fts") == 4
    assert _count(side, "tri") == 4
    lexstore.clear_stores()

    rebuilds = 0
    real_rebuild = lexstore.LexicalStore._rebuild

    def _counting_rebuild(self, conn):
        nonlocal rebuilds
        rebuilds += 1
        return real_rebuild(self, conn)

    monkeypatch.setattr(lexstore.LexicalStore, "_rebuild", _counting_rebuild)
    hits = lexstore.search_bm25(tmp_path, "payload", k=10, scope="kb")
    assert hits is not None and len(hits) == 4
    assert _count(side, "pages") == 4
    assert _count(side, "semantic_units") == 4
    assert _count(side, "fts") == 4
    assert _count(side, "tri") == 4
    assert rebuilds == 0


def test_out_of_band_single_edit_heals_incrementally_not_full_rebuild(tmp_path):
    """A single out-of-band file edit (count unchanged, one mtime bumped) must
    heal by patching ONLY that file's rows — not by wiping and repopulating the
    whole corpus. The full-rebuild heal is O(corpus); on a large real vault
    behind a flaky watcher it fires on every missed event (the 10-17s keyword
    lane / 3-6s cold find() observed 2026-07-05)."""
    for i in range(6):
        _write_page(tmp_path, f"Knowledge Base/n{i}.md", f"payload token{i}", mtime=1_000 + i)
    assert lexstore.search_bm25(tmp_path, "payload", k=10, scope="kb")
    lexstore.clear_stores()  # fresh process; no in-process hook witnessed the edit

    # One file rewritten out-of-band: same file count, a newer max mtime.
    _write_page(tmp_path, "Knowledge Base/n3.md", "payload zzzreplacement", mtime=9_000)

    calls = {"n": 0}
    real = lexstore.LexicalStore._rebuild

    def _counting(self, conn):
        calls["n"] += 1
        return real(self, conn)

    lexstore.LexicalStore._rebuild = _counting
    try:
        hits = lexstore.search_bm25(tmp_path, "zzzreplacement", k=5, scope="kb")
    finally:
        lexstore.LexicalStore._rebuild = real

    assert hits and hits[0][0] == "Knowledge Base/n3.md"  # new content indexed
    assert lexstore.search_bm25(tmp_path, "token3", k=5, scope="kb") == []  # old tokens gone
    assert lexstore.search_bm25(tmp_path, "token5", k=5, scope="kb")  # untouched intact
    assert _count(lexstore.lexical_path(tmp_path), "pages") == 6
    assert calls["n"] == 0  # healed incrementally, NOT via full rebuild


def test_out_of_band_add_and_delete_heal_incrementally(tmp_path):
    """A file removed from disk and another added out-of-band heal by patching
    just those two rows (delete + insert branches), never a full rebuild."""
    for i in range(5):
        _write_page(tmp_path, f"Knowledge Base/n{i}.md", f"corpus token{i}", mtime=1_000 + i)
    assert lexstore.search_bm25(tmp_path, "corpus", k=10, scope="kb")
    lexstore.clear_stores()

    (tmp_path / "Knowledge Base/n0.md").unlink()  # removed
    _write_page(tmp_path, "Knowledge Base/added.md", "corpus freshadd", mtime=8_000)  # added

    calls = {"n": 0}
    real = lexstore.LexicalStore._rebuild

    def _counting(self, conn):
        calls["n"] += 1
        return real(self, conn)

    lexstore.LexicalStore._rebuild = _counting
    try:
        hits = lexstore.search_bm25(tmp_path, "freshadd", k=5, scope="kb")
    finally:
        lexstore.LexicalStore._rebuild = real

    assert hits and hits[0][0] == "Knowledge Base/added.md"
    assert lexstore.search_bm25(tmp_path, "token0", k=5, scope="kb") == []  # removed file gone
    assert lexstore.search_bm25(tmp_path, "token4", k=5, scope="kb")  # survivor intact
    assert _count(lexstore.lexical_path(tmp_path), "pages") == 5  # 5 - 1 + 1
    assert calls["n"] == 0  # incremental delete+insert, not a full rebuild


def test_heal_reads_registry_map_not_filesystem_walk_when_live(tmp_path):
    """When the freshness registry is live (watcher-maintained), the incremental
    heal must diff against the registry's in-memory map, NOT re-stat the whole
    corpus — the ~2.7s drift-walk observed on the real D: vault. The registry is
    already current whenever a heal fires (that's why the triple drifted from the
    sidecar), so the filesystem walk is redundant."""
    from exomem import find as find_module
    from exomem import freshness
    from exomem import vault as vault_module

    freshness.clear()
    try:
        for i in range(5):
            _write_page(tmp_path, f"Knowledge Base/n{i}.md", f"payload token{i}", mtime=1_000 + i)
        assert lexstore.search_bm25(tmp_path, "payload", k=10, scope="kb")
        lexstore.clear_stores()  # fresh lexstore process; sidecar left on disk

        # The watcher seeds the registry live for both scopes at the current state.
        kb_dir = tmp_path / "Knowledge Base"
        freshness.seed(
            tmp_path,
            "kb",
            ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(kb_dir)),
        )
        freshness.seed(
            tmp_path,
            "vault",
            ((str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(tmp_path)),
        )

        # One file edited out-of-band; the watcher catches it and patches the
        # registry (but not the lexstore sidecar) — the exact drift the heal fixes.
        p = _write_page(tmp_path, "Knowledge Base/n2.md", "payload zzzreplaced", mtime=9_000)
        freshness.on_files_changed(tmp_path, changed=[p])

        walks = {"n": 0}
        real = lexstore.LexicalStore._walk_entries

        def _counting(self):
            walks["n"] += 1
            return real(self)

        lexstore.LexicalStore._walk_entries = _counting
        try:
            hits = lexstore.search_bm25(tmp_path, "zzzreplaced", k=5, scope="kb")
        finally:
            lexstore.LexicalStore._walk_entries = real

        assert hits and hits[0][0] == "Knowledge Base/n2.md"  # healed
        assert lexstore.search_bm25(tmp_path, "token2", k=5, scope="kb") == []  # old gone
        assert lexstore.search_bm25(tmp_path, "token4", k=5, scope="kb")  # survivor
        assert walks["n"] == 0  # read the registry map, did NOT walk the filesystem
    finally:
        freshness.clear()


def test_mtime_preserving_drift_heals_from_registry_no_walk(tmp_path):
    """Obsidian Sync preserves mtimes, so an out-of-band edit can land as
    count/max-match + digest-differ — the `_walk_matches_rows` verify path, NOT
    `_heal_delta`. With the registry live, that path must ALSO read the registry
    instead of re-statting the corpus (this is the real-vault drift shape that
    the earlier `_heal_delta`-only fix missed)."""
    from exomem import find as find_module
    from exomem import freshness
    from exomem import vault as vault_module

    freshness.clear()
    try:
        _write_page(
            tmp_path, "Knowledge Base/rename-old.md", "zanzibar quixotic marker", mtime=5_000
        )
        _write_page(tmp_path, "Knowledge Base/other.md", "unrelated filler", mtime=4_000)
        assert lexstore.search_bm25(tmp_path, "zanzibar", k=5, scope="kb")
        lexstore.clear_stores()  # fresh process; no in-process hook witnessed anything

        # Watcher seeds the registry live for both scopes at the current state.
        kb_dir = tmp_path / "Knowledge Base"
        freshness.seed(
            tmp_path,
            "kb",
            ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(kb_dir)),
        )
        freshness.seed(
            tmp_path,
            "vault",
            ((str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(tmp_path)),
        )

        # Pure rename: os.replace preserves mtime, so count AND max_mtime are
        # unchanged — only the digest differs (the `_walk_matches_rows` trigger).
        old = tmp_path / "Knowledge Base/rename-old.md"
        new = tmp_path / "Knowledge Base/rename-new.md"
        os.replace(old, new)
        freshness.on_files_changed(tmp_path, changed=[new], deleted=[old])

        walks = {"n": 0}
        real = lexstore.LexicalStore._walk_entries

        def _counting(self):
            walks["n"] += 1
            return real(self)

        lexstore.LexicalStore._walk_entries = _counting
        try:
            hits = lexstore.search_bm25(tmp_path, "zanzibar", k=5, scope="kb")
        finally:
            lexstore.LexicalStore._walk_entries = real

        assert hits and hits[0][0] == "Knowledge Base/rename-new.md"  # healed via registry
        assert walks["n"] == 0  # the verify path read the registry, not the filesystem
    finally:
        freshness.clear()


def test_writer_hooks_keep_index_in_lockstep(tmp_path):
    """upsert_after_write / delete_after_remove maintain pages+fts+tri without a
    rebuild, and a subsequent query observes the change."""
    _write_page(tmp_path, "Knowledge Base/keep.md", "stable content")
    assert lexstore.search_bm25(tmp_path, "stable", k=3, scope="kb")

    p = _write_page(tmp_path, "Knowledge Base/new.md", "freshly hooked page")
    lexstore.upsert_after_write(tmp_path, [p])
    hits = lexstore.search_bm25(tmp_path, "freshly", k=3, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/new.md"

    # Edit through the hook: old tokens gone, new ones live.
    p = _write_page(tmp_path, "Knowledge Base/new.md", "rewritten wording entirely")
    lexstore.upsert_after_write(tmp_path, [p])
    assert lexstore.search_bm25(tmp_path, "freshly", k=3, scope="kb") == []
    assert lexstore.search_bm25(tmp_path, "rewritten", k=3, scope="kb")

    # Delete through the hook.
    (tmp_path / "Knowledge Base/new.md").unlink()
    lexstore.delete_after_remove(tmp_path, ["Knowledge Base/new.md"])
    assert lexstore.search_bm25(tmp_path, "rewritten", k=3, scope="kb") == []
    side = lexstore.lexical_path(tmp_path)
    assert _count(side, "pages") == 1


def test_kill_switch_stops_writer_maintenance(tmp_path, monkeypatch):
    """EXOMEM_LEXICAL_BACKEND=python is full old behavior: hooks never create or
    touch the sidecar (the escape hatch must not depend on lexstore health)."""
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    p = _write_page(tmp_path, "Knowledge Base/a.md", "body")
    lexstore.upsert_after_write(tmp_path, [p])
    lexstore.delete_after_remove(tmp_path, ["Knowledge Base/a.md"])
    assert not lexstore.lexical_path(tmp_path).exists()
    assert lexstore.search_bm25(tmp_path, "body", k=3, scope="kb") is None


def test_unparseable_page_counts_but_never_matches(tmp_path):
    """A file the parser rejects still gets a (empty-text) row so the count
    check stays honest, but it can never match a query — mirroring the python
    rungs, which skip pages the cache can't parse."""
    _write_page(tmp_path, "Knowledge Base/good.md", "findable body")
    bad = tmp_path / "Knowledge Base" / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00broken utf-8 \xff")
    hits = lexstore.search_bm25(tmp_path, "findable", k=5, scope="kb")
    assert hits and [p for p, _ in hits] == ["Knowledge Base/good.md"]
    assert _count(lexstore.lexical_path(tmp_path), "pages") == 2


# ---------------------------------------------------------------- bm25 primitive


def test_bm25_or_semantics_and_ordering(tmp_path):
    """rank_bm25 scores docs matching ANY query token (OR), drops zero-score
    docs, and truncates to k with a deterministic (score, path) tie-break. The
    FTS5 rung must reproduce that shape."""
    _write_page(tmp_path, "Knowledge Base/both.md", "alpha beta alpha beta")
    _write_page(tmp_path, "Knowledge Base/one.md", "alpha only here")
    _write_page(tmp_path, "Knowledge Base/none.md", "entirely different words")
    hits = lexstore.search_bm25(tmp_path, "alpha beta", k=10, scope="kb")
    assert hits is not None
    paths = [p for p, _ in hits]
    assert "Knowledge Base/none.md" not in paths  # no-term docs excluded
    assert set(paths) == {"Knowledge Base/both.md", "Knowledge Base/one.md"}  # OR
    assert paths[0] == "Knowledge Base/both.md"  # more terms rank higher
    assert all(s > 0 for _, s in hits)  # positive scores
    assert lexstore.search_bm25(tmp_path, "alpha beta", k=1, scope="kb") == hits[:1]


def test_bm25_stemming_is_byte_identical_to_python_rung(tmp_path):
    """Pre-stemming: 'regulation' finds a page saying 'regulator' because BOTH
    sides pass through bm25.tokenize() — the indexed text and the query."""
    _write_page(tmp_path, "Knowledge Base/reg.md", "the regulator issued a decision")
    _write_page(tmp_path, "Knowledge Base/other.md", "nothing relevant")
    hits = lexstore.search_bm25(tmp_path, "regulation", k=5, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/reg.md"


def test_bm25_vault_scope_reaches_outside_kb(tmp_path):
    _write_page(tmp_path, "Knowledge Base/in.md", "inside page")
    _write_page(tmp_path, "Projects/out.md", "outside curator page")
    kb_hits = lexstore.search_bm25(tmp_path, "curator", k=5, scope="kb")
    assert kb_hits == []
    vault_hits = lexstore.search_bm25(tmp_path, "curator", k=5, scope="vault")
    assert vault_hits and vault_hits[0][0] == "Projects/out.md"


# ---------------------------------------------------------------- substring primitive


def test_substring_search_mid_word_and_ordering(tmp_path):
    """Strict substring semantics incl. mid-word, ordered `updated` desc then
    path desc — exactly what _keyword_match_paths produces."""
    _write_page(tmp_path, "Knowledge Base/old.md", "xylophone practice", updated="2024-01-01")
    _write_page(tmp_path, "Knowledge Base/new.md", "the xylophones sang", updated="2026-01-01")
    _write_page(tmp_path, "Knowledge Base/none.md", "silent percussion", updated="2025-01-01")
    got = lexstore.search_substring(tmp_path, "ylophon", scope="kb")  # mid-word
    assert got == ["Knowledge Base/new.md", "Knowledge Base/old.md"]


def test_substring_all_tokens_must_be_present(tmp_path):
    _write_page(tmp_path, "Knowledge Base/a.md", "employment contract terms")
    _write_page(tmp_path, "Knowledge Base/b.md", "contract only")
    got = lexstore.search_substring(tmp_path, "contract employment", scope="kb")
    assert got == ["Knowledge Base/a.md"]


def test_substring_title_or_body(tmp_path):
    _write_page(tmp_path, "Knowledge Base/t.md", "plain body", title="Quarterly Budget")
    got = lexstore.search_substring(tmp_path, "budget plain", scope="kb")
    assert got == ["Knowledge Base/t.md"]  # one token in title, one in body


def test_substring_short_needles_honor_contract(tmp_path):
    """1- and 2-char needles are below the trigram floor and must still match
    via the fallback lookup."""
    _write_page(tmp_path, "Knowledge Base/ab.md", "xq marks the spot")
    _write_page(tmp_path, "Knowledge Base/cd.md", "nothing here")
    assert lexstore.search_substring(tmp_path, "xq", scope="kb") == ["Knowledge Base/ab.md"]
    assert lexstore.search_substring(tmp_path, "q", scope="kb") == ["Knowledge Base/ab.md"]
    # Mixed lengths: short token narrows alongside an indexable one.
    assert lexstore.search_substring(tmp_path, "xq spot", scope="kb") == ["Knowledge Base/ab.md"]


def test_substring_like_metachars_are_literal(tmp_path):
    """% and _ in the query are literal characters, not wildcards."""
    _write_page(tmp_path, "Knowledge Base/pct.md", "growth was 42% overall")
    _write_page(tmp_path, "Knowledge Base/us.md", "snake_case identifiers")
    _write_page(tmp_path, "Knowledge Base/decoy.md", "growth was 42 percent overall x")
    assert lexstore.search_substring(tmp_path, "42%", scope="kb") == ["Knowledge Base/pct.md"]
    assert lexstore.search_substring(tmp_path, "e_c", scope="kb") == ["Knowledge Base/us.md"]


def test_substring_quote_chars_survive(tmp_path):
    _write_page(tmp_path, "Knowledge Base/q.md", 'she said "hello there" loudly')
    assert lexstore.search_substring(tmp_path, '"hello', scope="kb") == ["Knowledge Base/q.md"]


def test_substring_non_ascii(tmp_path):
    _write_page(tmp_path, "Knowledge Base/uni.md", "tere tulemast Tallinnasse sõbrad")
    assert lexstore.search_substring(tmp_path, "sõbra", scope="kb") == ["Knowledge Base/uni.md"]
    # Case-insensitivity via Python-side lowering of both sides:
    assert lexstore.search_substring(tmp_path, "tallinnasse", scope="kb") == [
        "Knowledge Base/uni.md"
    ]


def test_substring_skips_navigation_files(tmp_path):
    """index.md / log.md are excluded from the keyword lane (they name-drop
    every recent page) — the indexed lane must apply the same filter."""
    _write_page(tmp_path, "Knowledge Base/index.md", "everything mentioned peculiar")
    _write_page(tmp_path, "Knowledge Base/real.md", "peculiar finding")
    assert lexstore.search_substring(tmp_path, "peculiar", scope="kb") == ["Knowledge Base/real.md"]


def test_substring_matches_punctuation_only_page(tmp_path):
    """A page whose body stems to nothing must still be substring-matchable —
    the keyword contract is over raw text, not tokens."""
    _write_page(tmp_path, "Knowledge Base/sym.md", "+++ ~~~ !!!")
    assert lexstore.search_substring(tmp_path, "~~~", scope="kb") == ["Knowledge Base/sym.md"]


# ---------------------------------------------------------------- failure ladder


def test_probe_failure_memoizes_and_signals_fallback(tmp_path, monkeypatch):
    """A failed FTS5 probe: search_* return None (the caller's cue to use the
    python rung), no sidecar appears, and the probe is not retried per call."""
    calls = {"n": 0}

    def _boom(conn):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(lexstore, "_probe_fts5", _boom)
    lexstore.reset_memo()
    _write_page(tmp_path, "Knowledge Base/a.md", "body text")
    assert lexstore.search_bm25(tmp_path, "body", k=3, scope="kb") is None
    assert lexstore.search_bm25(tmp_path, "body", k=3, scope="kb") is None
    assert lexstore.search_substring(tmp_path, "body", scope="kb") is None
    assert calls["n"] == 1  # memoized after the first failure
    assert not lexstore.lexical_path(tmp_path).exists()


def test_runtime_failure_falls_back_for_the_process(tmp_path, monkeypatch):
    """A query that raises at runtime: that call signals fallback (None) and
    later calls stop attempting the sidecar."""
    _write_page(tmp_path, "Knowledge Base/a.md", "content")
    assert lexstore.search_bm25(tmp_path, "content", k=3, scope="kb")  # healthy first

    calls = {"n": 0}
    real = lexstore.LexicalStore._bm25_query

    def _flaky(self, *a, **kw):
        calls["n"] += 1
        raise sqlite3.OperationalError("simulated corruption")

    monkeypatch.setattr(lexstore.LexicalStore, "_bm25_query", _flaky)
    assert lexstore.search_bm25(tmp_path, "content", k=3, scope="kb") is None
    assert lexstore.search_bm25(tmp_path, "content", k=3, scope="kb") is None
    assert calls["n"] == 1
    monkeypatch.setattr(lexstore.LexicalStore, "_bm25_query", real)


def test_deleting_sidecar_is_always_safe(tmp_path):
    """Rollback contract: rm .lexical.sqlite; the next use rebuilds it."""
    _write_page(tmp_path, "Knowledge Base/a.md", "resilient text")
    assert lexstore.search_bm25(tmp_path, "resilient", k=3, scope="kb")
    lexstore.clear_stores()
    lexstore.lexical_path(tmp_path).unlink()
    hits = lexstore.search_bm25(tmp_path, "resilient", k=3, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/a.md"
