"""Task 1.3 (catalog) — RED: FTS-independent catalog + compound completeness.

Pins OpenSpec change ``restore-indexed-category-recall`` decision 1 and specs
*FTS-Independent Semantic Catalog*, *Incomplete Exact Recall Is Observable*,
and *FTS-Unavailable Category Correctness*:

* the semantic catalog (normal-table page/unit category-kind metadata) is
  maintained and queried independently of FTS5 / trigram availability;
* catalog completeness is a COMPOUND identity — catalog schema version,
  semantic-unit parser version, core category/authoring-contract identity, and
  extension semantic-language registry content hash — so a parser or registry
  change invalidates the projection even when no note Markdown changed;
* a safe exact category request against an incomplete catalog raises a typed,
  non-cacheable ``RETRIEVAL_INDEX_WARMING`` outcome instead of a false empty.

RED until ``lexstore.catalog_semantic_identity`` exists, the catalog builds and
answers without FTS5, and ``find`` raises the warming outcome.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import cli_ops, freshness, lexstore, semantic_index, semantic_language_registry
from exomem import find as find_module

needs_fts5 = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()


def _write_note(root: Path, rel_path: str, body: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"catalog-identity:{rel_path}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {path.stem}\n"
        f"exomem_id: {page_id}\n"
        "status: active\n"
        "updated: 2026-07-22\n"
        "---\n\n"
        f"# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _write_registry(root: Path, body: str) -> None:
    path = semantic_language_registry.registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _seed_live_freshness(root: Path, paths: list[Path]) -> None:
    vault_entries = [(str(path), freshness.stat_signature(path)) for path in paths]
    kb_entries = [
        entry for entry in vault_entries if Path(entry[0]).is_relative_to(root / "Knowledge Base")
    ]
    freshness.seed(root, "kb", kb_entries)
    freshness.seed(root, "vault", vault_entries)


# --------------------------------------------------------------------------- #
# Compound semantic projection identity.
# --------------------------------------------------------------------------- #


def test_catalog_identity_is_stable_when_nothing_changes(tmp_path: Path) -> None:
    _write_note(tmp_path, "Knowledge Base/Notes/a.md", "- [config] a ^a")
    first = lexstore.catalog_semantic_identity(tmp_path)
    second = lexstore.catalog_semantic_identity(tmp_path)
    assert isinstance(first, str) and first
    assert first == second


def test_catalog_identity_tracks_catalog_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = lexstore.catalog_semantic_identity(tmp_path)
    monkeypatch.setattr(lexstore, "SCHEMA_VERSION", lexstore.SCHEMA_VERSION + 1)
    assert lexstore.catalog_semantic_identity(tmp_path) != baseline


def test_catalog_identity_tracks_semantic_unit_parser_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = lexstore.catalog_semantic_identity(tmp_path)
    monkeypatch.setattr(semantic_index, "PARSER_VERSION", semantic_index.PARSER_VERSION + 1)
    assert lexstore.catalog_semantic_identity(tmp_path) != baseline


def test_precore_constraints_alias_change_invalidates_projection_identity(
    tmp_path: Path,
) -> None:
    """A sidecar built before a portable-core category contract must not be
    treated as complete once the core resolves an authored ``[constraints]``
    label differently — the identity changes with no note edit."""
    _write_note(tmp_path, "Knowledge Base/Notes/c.md", "- [constraints] pre-core token ^c")
    _write_registry(
        tmp_path,
        "schema_version: 1\ncategories: {}\nkinds: {}\n",
    )
    before = lexstore.catalog_semantic_identity(tmp_path)

    _write_registry(
        tmp_path,
        "schema_version: 1\n"
        "categories:\n"
        "  constraint:\n"
        "    description: Constraint facts\n"
        "    aliases: [constraints]\n"
        "kinds: {}\n",
    )
    after = lexstore.catalog_semantic_identity(tmp_path)
    assert before != after


def test_extension_registry_save_invalidates_projection_identity(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        "schema_version: 1\n"
        "categories:\n"
        "  config:\n"
        "    description: Configuration facts\n"
        "    aliases: [configuration]\n"
        "kinds: {}\n",
    )
    before = lexstore.catalog_semantic_identity(tmp_path)

    _write_registry(
        tmp_path,
        "schema_version: 1\n"
        "categories:\n"
        "  config:\n"
        "    description: Revised configuration facts\n"
        "    aliases: [configuration, cfg]\n"
        "kinds: {}\n",
    )
    after = lexstore.catalog_semantic_identity(tmp_path)
    assert before != after


# --------------------------------------------------------------------------- #
# FTS-independent exact category metadata.
# --------------------------------------------------------------------------- #


def test_exact_category_recall_without_fts5_avoids_a_corpus_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FTS5 probing fails, yet the maintained catalog answers exact category
    recall from normal indexed tables — no false empty, no Markdown scope walk."""

    def _no_fts5(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(lexstore, "_probe_fts5", _no_fts5)
    lexstore.reset_memo()
    assert lexstore.fts5_available() is False

    target = _write_note(
        tmp_path,
        "Knowledge Base/Notes/lean.md",
        "- [config] exact metadata even without fts5 ^lean",
    )
    _seed_live_freshness(tmp_path, [target])
    # The catalog is a normal-table capability; it must build without FTS5.
    lexstore.ensure_fresh(tmp_path)

    def forbidden_walk(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("FTS-unavailable exact metadata must not walk the corpus")

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        categories=["config"],
        limit=20,
    )

    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/lean.md"]
    assert [hit.category for hit in hits] == ["config"]


# --------------------------------------------------------------------------- #
# Incomplete recall is a typed, non-cacheable warming outcome.
# --------------------------------------------------------------------------- #


@needs_fts5
def test_incomplete_catalog_raises_non_cacheable_warming_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A safe exact category request before catalog readiness raises a typed
    ``RETRIEVAL_INDEX_WARMING`` outcome — never a cached authoritative empty."""
    # A cold, unbuilt sidecar with live freshness and no inline-repair budget:
    # completeness cannot be established for the safe category plan.
    monkeypatch.setattr(find_module, "_FOREGROUND_LEXICAL_REPAIR_PAGE_CAP", 0)
    scheduled: list[Path] = []
    monkeypatch.setattr(lexstore, "_schedule_repair", scheduled.append)
    target = _write_note(
        tmp_path,
        "Knowledge Base/Notes/warming.md",
        "- [config] not yet indexed ^warming",
    )
    _seed_live_freshness(tmp_path, [target])
    assert not lexstore.lexical_path(tmp_path).exists()

    with pytest.raises(Exception) as caught:  # noqa: PT011 - typed op outcome, class TBD in stage 2
        find_module.find(
            tmp_path,
            query="",
            scope="kb-only",
            mode="keyword",
            result_level="unit",
            categories=["config"],
            limit=20,
        )
    error = caught.value
    assert getattr(error, "code", None) == "RETRIEVAL_INDEX_WARMING"
    assert getattr(error, "complete", None) is False
    assert getattr(error, "status", None) in {"warming", "temporarily_unavailable"}
    retry_after = getattr(error, "retry_after_ms", None)
    assert isinstance(retry_after, int) and retry_after > 0
    assert isinstance(error, cli_ops.OpError)
    public = error.as_public_dict()
    assert public["code"] == "RETRIEVAL_INDEX_WARMING"
    assert public["complete"] is False
    assert public["status"] in {"warming", "temporarily_unavailable"}
    assert public["retry_after_ms"] == retry_after
    assert cli_ops.error_dict(error) == public
    assert scheduled == [tmp_path]

    # Not sticky and not cached-as-empty: once the catalog is built the exact
    # request returns the real hit.
    monkeypatch.setattr(find_module, "_FOREGROUND_LEXICAL_REPAIR_PAGE_CAP", 64)
    lexstore.ensure_fresh(tmp_path)
    find_module.clear_cache()
    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        categories=["config"],
        limit=20,
    )
    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/warming.md"]


@needs_fts5
def test_projection_identity_mismatch_is_warming_not_stale_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_note(
        tmp_path,
        "Knowledge Base/Notes/identity.md",
        "- [config] indexed under the first language contract ^identity",
    )
    _write_registry(
        tmp_path,
        "schema_version: 1\n"
        "categories:\n"
        "  local_lens:\n"
        "    description: First public-safe definition\n"
        "kinds: {}\n",
    )
    _seed_live_freshness(tmp_path, [target])
    lexstore.ensure_fresh(tmp_path)

    # Registry bytes are part of the semantic projection identity even though
    # the Markdown freshness checkpoint did not move.
    _write_registry(
        tmp_path,
        "schema_version: 1\n"
        "categories:\n"
        "  local_lens:\n"
        "    description: Revised public-safe definition\n"
        "kinds: {}\n",
    )
    scheduled: list[Path] = []
    monkeypatch.setattr(lexstore, "_schedule_repair", scheduled.append)
    monkeypatch.setattr(
        find_module,
        "_walk_md",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity mismatch must not trigger a foreground walk")
        ),
    )

    with pytest.raises(find_module.RetrievalIndexWarming) as caught:
        find_module.find(
            tmp_path,
            query="",
            scope="kb-only",
            mode="keyword",
            result_level="page",
            categories=["config"],
            limit=20,
        )

    assert caught.value.complete is False
    assert scheduled == [tmp_path]


@needs_fts5
def test_complete_delta_at_cap_repairs_catalog_once_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        _write_note(
            tmp_path,
            f"Knowledge Base/Notes/delta-{index:02d}.md",
            f"- [config] before bounded repair {index} ^d{index}",
        )
        for index in range(32)
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    for page in pages:
        page.write_text(
            page.read_text(encoding="utf-8").replace("[config]", "[rule]"),
            encoding="utf-8",
        )
    freshness.on_files_changed(tmp_path, changed=pages)

    applied: list[Any] = []
    original_apply = store.apply_catalog_delta

    def observed_apply(scope: str, delta: Any) -> None:
        applied.append(delta)
        original_apply(scope, delta)

    monkeypatch.setattr(store, "apply_catalog_delta", observed_apply)
    monkeypatch.setattr(
        find_module,
        "_walk_md",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete <=32 delta must not walk the corpus")
        ),
    )

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="page",
        categories=["rule"],
        limit=100,
    )

    assert len(hits) == 32
    assert len(applied) == 1
    assert applied[0].from_ == before
    assert store.catalog_checkpoint("kb") == applied[0].to


@needs_fts5
def test_delta_over_cap_returns_warming_and_schedules_one_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        _write_note(
            tmp_path,
            f"Knowledge Base/Notes/over-cap-{index:02d}.md",
            f"- [config] before oversized repair {index} ^o{index}",
        )
        for index in range(33)
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    for page in pages:
        page.write_text(
            page.read_text(encoding="utf-8").replace("[config]", "[rule]"),
            encoding="utf-8",
        )
    freshness.on_files_changed(tmp_path, changed=pages)

    scheduled: list[Path] = []
    monkeypatch.setattr(lexstore, "_schedule_repair", scheduled.append)
    monkeypatch.setattr(
        find_module,
        "_walk_md",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized delta must not walk the corpus")
        ),
    )

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(
            tmp_path,
            query="",
            scope="kb-only",
            mode="keyword",
            result_level="page",
            categories=["rule"],
            limit=100,
        )

    assert scheduled == [tmp_path]
