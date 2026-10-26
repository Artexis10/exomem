"""A `#` inside a file name is part of the name, never a link fragment.

A graph file row is keyed by the exact vault path of its page. Link syntax
(`[[Page#unit]]`) splits a fragment off a *target*; a page path is not a target.
Two pages whose names differ only after a `#` once shared one row key, so the
sidecar held fewer file rows than dependency-coverage rows and never converged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

SOURCES = "Knowledge Base/Sources"
FIRST = f"{SOURCES}/Scan #1001 Report.PDF.md"
SECOND = f"{SOURCES}/Scan #1002 Report.PDF.md"
LINKER = "Knowledge Base/Notes/Insights/linker.md"
PLAIN = "Knowledge Base/Notes/Insights/plain.md"
ADDED = "Knowledge Base/Notes/Insights/added.md"


def _titled(title: str, body: str) -> str:
    return f"---\ntype: pattern\nstatus: active\ntitle: {title}\n---\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(root / "Knowledge Base")
        ),
    )


@pytest.fixture
def hash_vault(tmp_path: Path) -> Path:
    """Two pages whose names differ after `#`, and a page linking to the first by title."""
    root = tmp_path / "vault"
    for rel, text in {
        FIRST: _titled("First scan", "Body of the first scan."),
        SECOND: _titled("Second scan", "Body of the second scan."),
        LINKER: _titled("Linker", "It cites [[First scan]]."),
        PLAIN: _titled("Plain", "No links."),
    }.items():
        page = root / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")
    _seed_live_freshness(root)
    epistemic_graph.clear_publication_memos()
    yield root
    epistemic_graph.clear_publication_memos()


def _rows(root: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _complete(index: EpistemicGraphIndex) -> bool:
    connection = index._connect_existing(readonly=True)
    try:
        connection.execute("BEGIN")
        return index._dependency_index_complete(connection)
    finally:
        connection.rollback()
        connection.close()


def _edit(root: Path, rel: str) -> Path:
    page = root / rel
    page.write_text(page.read_text(encoding="utf-8") + "\nAn edit.\n", encoding="utf-8")
    freshness.rebaseline(root)
    return page


def _write_under_the_old_key(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the sidecar as a release that cut a file row's key at `#` wrote it."""
    exact_file_key = epistemic_graph._file_key

    def collapsing_file_key(rel_path: str) -> str:
        return "file:" + epistemic_graph._with_md(rel_path)

    monkeypatch.setattr(epistemic_graph, "_file_key", collapsing_file_key)
    EpistemicGraphIndex(root).rebuild_all()
    monkeypatch.setattr(epistemic_graph, "_file_key", exact_file_key)
    epistemic_graph.clear_publication_memos()


def test_pages_differing_after_a_hash_converge_like_any_other_pair(hash_vault: Path) -> None:
    root = hash_vault
    EpistemicGraphIndex(root).rebuild_all()
    index = EpistemicGraphIndex(root)

    assert dict(_rows(root, "SELECT path, title FROM graph_nodes WHERE kind = 'file'")) == {
        FIRST: "First scan",
        SECOND: "Second scan",
        LINKER: "Linker",
        PLAIN: "Plain",
    }
    assert {key for (key,) in _rows(root, "SELECT node_key FROM graph_nodes WHERE kind = 'file'")} == {
        f"file:{rel}" for rel in (FIRST, SECOND, LINKER, PLAIN)
    }
    assert (
        _rows(
            root,
            "SELECT dst_key FROM graph_edges WHERE source_path = "
            f"'{LINKER}' AND relation_type = 'links_to'",
        )
        == [(f"file:{FIRST}",)]
    )
    assert _complete(index)

    report = index.drain_paths([_edit(root, SECOND)])

    assert "requires_rebuild" not in report
    assert report["published"] is True
    assert _complete(index)
    titles = dict(_rows(root, "SELECT path, title FROM graph_nodes WHERE kind = 'file'"))
    assert titles[FIRST] == "First scan", "an edit to one page rewrote its namesake's row"
    connection = index._connect_existing(readonly=True)
    try:
        resolver = find_module.recall_resolver_snapshot(
            root, expected_checkpoint=freshness.recall_checkpoint(root, "vault")
        )
        assert index._created_rows_only(connection, resolver) == set(), (
            "a graph rebuilt over these pages must be able to publish behind the queue"
        )
    finally:
        connection.close()
    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    assert adoption.adopted is True, adoption.reason


def test_a_retitled_hash_page_is_adopted_as_residue(hash_vault: Path) -> None:
    """The adoption proof reverts a residue page through its own row, not its namesake's."""
    root = hash_vault
    EpistemicGraphIndex(root).rebuild_all()
    (root / SECOND).write_text(_titled("Second scan renamed", "Body."), encoding="utf-8")
    freshness.rebaseline(root)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)

    assert adoption.adopted is True, adoption.reason
    assert list(adoption.residue) == [SECOND]


def test_a_sidecar_with_the_old_collapsed_row_converges_by_one_rebuild(
    hash_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar written under the old key heals through the ordinary fallback.

    The collapsed sidecar has no file row for one page yet keeps its dependency
    coverage, so the drain cannot prove its topology widening and asks for the
    whole-vault rebuild every drain falls back to. That rebuild derives exact
    keys and the next drain is incremental again.
    """
    root = hash_vault
    _write_under_the_old_key(root, monkeypatch)
    index = EpistemicGraphIndex(root)

    assert len(_rows(root, "SELECT 1 FROM graph_nodes WHERE kind = 'file'")) == 3
    assert not _complete(index)
    assert index.drain_paths([_edit(root, PLAIN)]).get("requires_rebuild") == 1

    index.rebuild_all()

    assert len(_rows(root, "SELECT 1 FROM graph_nodes WHERE kind = 'file'")) == 4
    assert _complete(index)
    assert "requires_rebuild" not in index.drain_paths([_edit(root, PLAIN)])


@pytest.mark.parametrize("drift", ["a new page", "an unrelated retitle"])
def test_a_standby_adopts_a_sidecar_written_under_the_old_key_after_a_topology_change(
    hash_vault: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    """The page that lost its file row is residue the drain repairs, not a topology mismatch.

    The stored fingerprint saw that page present. Reverting it as absent, as a page
    with no file row looks, could never reproduce that fingerprint once anything else
    had moved, so the standby paid a cold start on exactly the vault that needs it.
    """
    root = hash_vault
    _write_under_the_old_key(root, monkeypatch)
    covered = {path for (path,) in _rows(root, "SELECT source_path FROM graph_dependency_coverage")}
    with_a_row = {path for (path,) in _rows(root, "SELECT path FROM graph_nodes WHERE kind = 'file'")}
    (lost,) = covered - with_a_row
    if drift == "a new page":
        (root / ADDED).write_text(_titled("Added", "A page since publication."), encoding="utf-8")
    else:
        (root / PLAIN).write_text(_titled("Plain renamed", "No links."), encoding="utf-8")
    freshness.rebaseline(root)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)

    assert adoption.adopted is True, adoption.reason
    assert lost in adoption.residue, "the repair would never be queued for the page without a row"


def test_a_lone_hash_page_in_an_old_sidecar_keeps_its_stored_title(
    hash_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `#` page without a namesake sits under a collapsed key; its entry is found by path.

    Both consumers of a stored entry are covered: the residue revert of a standby
    proof, and the topology carry a drain keeps for the next proof.
    """
    root = hash_vault
    (root / SECOND).unlink()
    freshness.rebaseline(root)
    _write_under_the_old_key(root, monkeypatch)
    (root / FIRST).write_text(_titled("First scan renamed", "Body."), encoding="utf-8")
    freshness.rebaseline(root)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)

    assert adoption.adopted is True, adoption.reason
    assert list(adoption.residue) == [FIRST]

    assert EpistemicGraphIndex(root).drain_paths([root / FIRST])["published"] is True
    (root / ADDED).write_text(_titled("Added", "A page since the drain."), encoding="utf-8")
    _edit(root, LINKER)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)

    assert adoption.adopted is True, adoption.reason
