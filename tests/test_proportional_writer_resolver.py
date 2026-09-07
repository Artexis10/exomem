"""Writer resolver snapshots can borrow a proven warm semantic corpus."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from exomem import find, freshness, semantic_contract, vault
from exomem.vault import WikilinkResolver


def _page(title: str, body: str = "") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "project: atlas\n"
        "---\n\n"
        f"{body}\n"
    )


@pytest.fixture(autouse=True)
def _clean_process_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    find.clear_cache()
    freshness.clear()
    semantic_contract.reset_corpus_context_cache()
    yield
    find.clear_cache()
    freshness.clear()
    semantic_contract.reset_corpus_context_cache()


@pytest.fixture()
def warm_corpus(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "alpha.md").write_text(_page("Alpha", "[[Beta]]"), encoding="utf-8")
    (notes / "beta.md").write_text(_page("Beta"), encoding="utf-8")
    records = tmp_path / "Knowledge Base" / "Records"
    records.mkdir()
    (records / "record.md").write_text(_page("Record page"), encoding="utf-8")
    freshness.seed(
        tmp_path,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.walk_vault_md(tmp_path)),
    )
    semantic_contract.build_corpus_context(tmp_path)
    return tmp_path


def test_warm_corpus_supplies_cold_writer_snapshot_without_body_rebuild(
    warm_corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm semantic corpus replaces the writer's otherwise full disk resolver."""
    original_build = WikilinkResolver._build
    original_read_text = Path.read_text
    builds: list[None] = []
    markdown_reads: list[Path] = []

    def count_build(self: WikilinkResolver) -> None:
        builds.append(None)
        original_build(self)

    def count_read_text(self: Path, *args, **kwargs):
        if self.suffix == ".md":
            markdown_reads.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(WikilinkResolver, "_build", count_build)
    monkeypatch.setattr(Path, "read_text", count_read_text)

    snapshot = find.writer_resolver_snapshot(warm_corpus)

    assert builds == []
    assert markdown_reads == []
    assert "Knowledge Base/Records/record" in snapshot.full_paths
    fresh = WikilinkResolver(warm_corpus)
    assert snapshot.full_paths == fresh.full_paths
    assert snapshot.stems == fresh.stems
    assert snapshot.titles == fresh.titles


def test_warm_corpus_snapshot_is_detached_from_pending_destination(
    warm_corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview-only destinations remain local when the corpus supplies entries."""
    original_build = WikilinkResolver._build

    def no_disk_build(self: WikilinkResolver) -> None:
        raise AssertionError("warm corpus resolver preparation read the vault")

    monkeypatch.setattr(WikilinkResolver, "_build", no_disk_build)
    snapshot = find.writer_resolver_snapshot(warm_corpus)
    snapshot.add_pending("Knowledge Base/Notes/Insights/pending", title="Pending")

    assert "Knowledge Base/Notes/Insights/pending" in snapshot.full_paths
    assert find.cache_status()["resolvers"]["entries"] == 0
    monkeypatch.setattr(WikilinkResolver, "_build", original_build)
    fresh = WikilinkResolver(warm_corpus)
    assert "Knowledge Base/Notes/Insights/pending" not in fresh.full_paths


def test_resolver_entries_decline_when_direct_freshness_differs(
    warm_corpus: Path,
) -> None:
    """A caller's direct disk key cannot relabel an older cached corpus."""
    alpha = warm_corpus / "Knowledge Base" / "Notes" / "Insights" / "alpha.md"
    alpha.write_text(_page("Alpha renamed", "[[Beta]]"), encoding="utf-8")
    direct_key = find._walk_freshness_key(vault.walk_vault_md(warm_corpus))

    assert semantic_contract.current_writer_resolver_entries(
        warm_corpus, freshness_key=direct_key
    ) is None


def test_resolver_entries_decline_when_broad_membership_exceeds_semantic_entries(
    warm_corpus: Path,
) -> None:
    """An unreadable non-governed file cannot silently disappear from writers."""
    cache_key = semantic_contract._corpus_cache_key(warm_corpus)
    census, context = semantic_contract._CORPUS_CONTEXT_CACHE[cache_key]
    missing = next(entry for entry in context.resolver_entries if entry[0].endswith("record.md"))
    incomplete = dataclasses.replace(
        context,
        resolver_entries=tuple(entry for entry in context.resolver_entries if entry != missing),
    )
    semantic_contract._CORPUS_CONTEXT_CACHE[cache_key] = (census, incomplete)

    assert semantic_contract.current_writer_resolver_entries(warm_corpus) is None


def test_resolver_entries_decline_when_an_event_arrives_during_capture(
    warm_corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer event cannot attach a new caption to the prior entry snapshot."""
    original_walk = semantic_contract.vault.walk_vault_md
    alpha = warm_corpus / "Knowledge Base" / "Notes" / "Insights" / "alpha.md"

    def event_during_walk(root: Path):
        paths = tuple(original_walk(root))
        alpha.write_text(_page("Alpha updated", "[[Beta]]"), encoding="utf-8")
        freshness.on_files_changed(root, changed=(alpha,))
        return iter(paths)

    monkeypatch.setattr(semantic_contract.vault, "walk_vault_md", event_during_walk)

    assert semantic_contract.current_writer_resolver_entries(warm_corpus) is None


def test_resolver_entries_decline_when_configuration_changes_during_capture(
    warm_corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration proof is checked again after collecting path membership."""
    original_census = semantic_contract._config_census
    stable = original_census(warm_corpus)
    assert stable is not None
    changed = stable + (("simulated", "cfg", 1, 1),)
    calls = 0

    def changing_census(root: Path):
        nonlocal calls
        calls += 1
        return stable if calls == 1 else changed

    monkeypatch.setattr(semantic_contract, "_config_census", changing_census)

    assert semantic_contract.current_writer_resolver_entries(warm_corpus) is None
