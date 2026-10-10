"""An activation-index update re-derives only the pages that changed.

`update()` reuses each unchanged page's walk entry and writes only the rows
that differ. That is safe only while the rows it leaves are the rows a build
from nothing leaves for the same vault, whatever came before: these tests
hold it to that, through edits, adds, deletes and renames, and through the
three ways a reused entry could go stale without its file's own stat moving.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from exomem import working_set_index

KB = "Knowledge Base"
CATALOGUE = (
    "anchors",
    "anchor_aliases",
    "anchor_categories",
    "anchor_entity_types",
    "anchor_links",
    "anchor_term_rows",
    "page_names",
    "term_anchor_counts",
    "anchor_terms",
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rows(vault: Path) -> dict[str, list[tuple]]:
    """Every catalogue row and the build's own meta, read with plain SQL."""
    conn = sqlite3.connect(working_set_index.sidecar_path(vault))
    try:
        rows = {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in CATALOGUE
        }
        rows["index_meta"] = sorted(
            conn.execute(
                "SELECT key, value FROM index_meta "
                "WHERE key IN ('learned_alias_rejected', 'project_registry_hash')"
            ).fetchall()
        )
        return rows
    finally:
        conn.close()


def _from_nothing(vault: Path, copy: Path) -> dict[str, list[tuple]]:
    """The rows a first build of the same vault leaves. `copytree` keeps
    modification times, so every page's source signature matches."""
    shutil.copytree(vault, copy, symlinks=True)
    working_set_index.WorkingSetIndex(copy).rebuild()
    return _rows(copy)


def _seed(vault: Path) -> None:
    kb = vault / KB
    _write(
        kb / "_Schema" / "project-keys.yaml",
        "projects:\n"
        "  harbor:\n    folder: Harbor Survey\n    category: logistics\n"
        "  orchard:\n    folder: Orchard Plan\n    category: home\n",
    )
    _write(
        kb / "Entities" / "People" / "Ada Lindqvist.md",
        "---\ntype: entity\nentity_type: person\naliases: [Ada]\n---\n\n"
        "# Ada Lindqvist\n\nAda runs the winter depot.\n",
    )
    _write(
        kb / "Entities" / "Organisations" / "Norrland Freight (Rail).md",
        "---\ntype: entity\nentity_type: organisation\ntitle: Norrland Freight (Rail)\n---\n\n"
        "# Norrland Freight\n\nThe rail carrier for the corridor.\n",
    )
    _write(
        kb / "Entities" / "Places" / "Kiruna Depot.md",
        "---\ntype: entity\nentity_type: place\n---\n\n# Kiruna Depot\n\nThe northern depot.\n",
    )
    _write(
        kb / "Notes" / "Insights" / "corridor-hub.md",
        "---\ntype: insight\ntags: [hub]\n---\n\n# Corridor hub\n\n"
        "Everything about the corridor. [[Ada Lindqvist]]\n\n## Open questions\n\nWho owns winter?\n",
    )
    _write(
        kb / "Products" / "Cargo Sled.md",
        "---\ntype: note\n---\n\n# Cargo Sled\n\nA towed sled. [[Kiruna Depot]]\n",
    )
    _write(
        kb / "Notes" / "Research" / "depth-notice.md",
        "---\ntype: research-note\nproject: harbor\nupdated: 2026-09-10\n---\n\n"
        "# Depth notice\n\nDraft limit, per [[Ada Lindqvist]].\n",
    )
    _write(
        kb / "Notes" / "Research" / "harbor-method.md",
        "---\ntype: research-note\nproject: harbor\nstanding: true\nupdated: 2026-09-01\n---\n\n"
        "# Harbor method\n\nHow the survey is run.\n",
    )
    _write(
        kb / "Notes" / "Journal" / "monday.md",
        "---\ntype: journal\nproject: orchard\n---\n\n# Monday\n\nPruned the rows.\n",
    )
    _write(
        kb / "Sources" / "clipping.md",
        "---\ntype: source\nproject: harbor\n---\n\n# Clipping\n\nA captured article.\n",
    )


def test_an_update_after_any_change_leaves_the_rows_a_first_build_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reused entry that outlives its page's change leaves a stale anchor,
    link, name or count that only a rebuild would remove; so would a write
    that skips a row it should have changed."""
    # Every page here is seconds old, so with the shipped window none would
    # be reused at all; the racy-clean rule has its own test below.
    monkeypatch.setattr(working_set_index, "_RACY_WINDOW_NS", 0)
    vault = tmp_path / "vault"
    kb = vault / KB
    _seed(vault)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert _rows(vault) == _from_nothing(vault, tmp_path / "first")

    def edit(rel: str, old: str, new: str) -> None:
        path = kb / rel
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    steps = [
        ("a note gains a link", lambda: edit(
            "Products/Cargo Sled.md", "[[Kiruna Depot]]", "[[Kiruna Depot]] and [[Ada]]")),
        ("prose changes, nothing derived does", lambda: edit(
            "Notes/Journal/monday.md", "Pruned the rows.", "Pruned the last rows today.")),
        ("a second page derives the same short name", lambda: _write(
            kb / "Entities" / "Organisations" / "Norrland Freight (Road).md",
            "---\ntype: entity\nentity_type: organisation\ntitle: Norrland Freight (Road)\n---\n\n"
            "# Norrland Freight Road\n\nThe road carrier.\n")),
        ("a newer page declares itself standing", lambda: _write(
            kb / "Notes" / "Research" / "harbor-method-v2.md",
            "---\ntype: research-note\nproject: harbor\nstanding: true\nupdated: 2026-10-01\n---\n\n"
            "# Harbor method v2\n\nThe revised survey method. [[Kiruna Depot]]\n")),
        ("a note is renamed", lambda: os.rename(
            kb / "Notes" / "Research" / "depth-notice.md",
            kb / "Notes" / "Research" / "draft-limit.md")),
        ("a linked entity is deleted", lambda: (kb / "Entities" / "Places" / "Kiruna Depot.md").unlink()),
        ("a folder is renamed", lambda: os.rename(kb / "Notes" / "Journal", kb / "Notes" / "Diary")),
        ("an entity is retitled", lambda: edit(
            "Entities/People/Ada Lindqvist.md", "aliases: [Ada]", "title: Ada Berg\naliases: [Ada]")),
        ("a page leaves its project", lambda: edit(
            "Notes/Research/harbor-method-v2.md", "project: harbor\n", "")),
    ]
    before = _rows(vault)
    for number, (label, change) in enumerate(steps):
        generation = index.generation()
        change()
        report = index.update()
        after = _rows(vault)
        assert after == _from_nothing(vault, tmp_path / f"step-{number}"), label
        # A generation moves exactly when the rows do, as a full write's does.
        assert report.get("unchanged", False) is (after == before), label
        assert index.generation() == generation + (0 if after == before else 1), label
        before = after


def test_a_page_rewritten_inside_one_file_time_tick_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where file times move in coarse ticks, a same-size rewrite inside the
    tick keeps the page's whole stat, and only the racy-clean rule stops the
    update reusing the entry derived from the old bytes."""
    # Every page counts as just written, however slowly the runner gets from
    # the write to the walk.
    monkeypatch.setattr(working_set_index, "_RACY_WINDOW_NS", 10**15)
    vault = tmp_path / "vault"
    _seed(vault)
    page = vault / KB / "Entities" / "Places" / "Kiruna Depot.md"
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    held = os.lstat(page)
    real_lstat = os.lstat

    def coarse_lstat(path, *args, **kwargs):
        # The file system reports the stat it had before the rewrite.
        return held if os.fspath(path) == os.fspath(page) else real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", coarse_lstat)
    page.write_text(
        page.read_text(encoding="utf-8").replace("Kiruna Depot", "Abisko Depot"), encoding="utf-8"
    )
    assert os.lstat(page).st_size == len(page.read_bytes())

    index.update()

    titles = {anchor.title for anchor in index.anchors()}
    assert "Abisko Depot" in titles
    assert "Kiruna Depot" not in titles


def test_pages_under_a_folder_swapped_for_a_link_leave_as_on_a_first_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The governed read refuses a page reached through a symlinked folder.
    Moving a folder out and linking it back leaves every page's own stat as
    it was, so only the folder identities in the key drop the reused entries."""
    monkeypatch.setattr(working_set_index, "_RACY_WINDOW_NS", 0)
    vault = tmp_path / "vault"
    _seed(vault)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert any(anchor.title == "Ada Lindqvist" for anchor in index.anchors())

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.rename(vault / KB / "Entities", elsewhere / "Entities")
    os.symlink(elsewhere / "Entities", vault / KB / "Entities", target_is_directory=True)
    index.update()

    assert not any(anchor.kind == "entity" for anchor in index.anchors())
    assert _rows(vault) == _from_nothing(vault, tmp_path / "reference")


def test_an_update_racing_another_writer_leaves_one_vault_states_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows are diffed against what was read before the write lock. A second
    process that commits a later vault state in between must not leave its
    rows mixed into this update's."""
    monkeypatch.setattr(working_set_index, "_RACY_WINDOW_NS", 0)
    vault = tmp_path / "vault"
    _seed(vault)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    real_plan = working_set_index.WorkingSetIndex._plan_vectors
    raced: list[bool] = []

    def plan_after_another_writer(self, *args, **kwargs):
        if self is index and not raced:
            raced.append(True)
            # The other process sees one more page than this update did.
            _write(vault / KB / "Notes" / "Journal" / "tuesday.md", "# Tuesday\n\nWatered.\n")
            working_set_index.WorkingSetIndex(vault).update()
            (vault / KB / "Notes" / "Journal" / "tuesday.md").unlink()
        return real_plan(self, *args, **kwargs)

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "_plan_vectors", plan_after_another_writer)
    _write(vault / KB / "Notes" / "Journal" / "wednesday.md", "# Wednesday\n\nHarvest.\n")
    report = index.update()

    assert raced
    assert "unavailable" not in report
    names = {path for _name, path in _rows(vault)["page_names"]}
    assert f"{KB}/Notes/Journal/wednesday.md" in names
    assert f"{KB}/Notes/Journal/tuesday.md" not in names
