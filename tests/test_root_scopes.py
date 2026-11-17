"""A `root` vault's managed root is its vault root, so a walk of managed pages must leave out what sits beside them.

The vault root of a `root` vault also holds dot folders, the import staging folder and the folders that
its layout record lists as `unmanaged`. A managed-page walk lists none of them. A walk of the whole vault
keeps its scope and lists the `unmanaged` folder, as a `nested` vault's walk lists the content outside its
wrapper folder. Each path predicate answers for the root vault's own folders.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from exomem import (
    audit,
    backfill,
    commands,
    find_corpus,
    freshness,
    lexstore,
    semantic_contract,
    vault,
    working_set_index,
)
from exomem.governance import policy
from exomem.governance.principal import library_scope
from exomem.kbdir import managed_root
from exomem.reserved_paths import PathDisposition, classify_logical
from exomem.trash_metadata import is_inside_trash, is_trash_path
from exomem.vault_store import ownership
from exomem.vault_store import read as store_read
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
NOTES = "Notes"
TOKEN = "zephyrquill"
PAGES = ("Insights/pump-cause.md", "Sources/Articles/pump-guide.md")
STAGED = "_Imports/5f0c2a1e-8b3d-4c7a-9e21-6a4d0b9c1f33/x.md"
OUTSIDE = (".obsidian/x.md", STAGED, "Scratch/x.md")
OUTSIDE_THE_VAULT_SCOPE = (".obsidian/x.md", STAGED)


def _write(vault_root: Path, rel: str, text: str) -> None:
    path = vault_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def scoped_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bound `root` vault with pages in two areas, a dot folder, a staged import and an `unmanaged` folder."""
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns(NOTES))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    for rel in (*PAGES, *OUTSIDE):
        _write(dest, rel, f"---\ntitle: Pump page\n---\n\nThe {TOKEN} gasket.\n")
    bind_root_vault(dest, monkeypatch)
    _write(dest, "_Schema/vault-layout.yaml", "layout: root\nunmanaged: [Scratch]\n")
    vault.resolve_vault()
    initialize_vault_state_offline(dest, source="root scopes fixture")
    return dest


def _rels(vault_root: Path, paths) -> set[str]:
    return {Path(path).relative_to(vault_root).as_posix() for path in paths}


def _assert_pages_only(listed: set[str]) -> None:
    assert set(PAGES) <= listed
    assert listed.isdisjoint(OUTSIDE)


def test_the_search_index_build_and_the_freshness_walk_list_only_the_pages(scoped_vault: Path) -> None:
    """The shared walk anchored at the vault root and entered every dot folder, staged import and unmanaged folder."""
    _assert_pages_only(_rels(scoped_vault, find_corpus.walk_md(managed_root(scoped_vault))))
    with library_scope():
        found = commands.op_search(scoped_vault, query=TOKEN, limit=50)
    assert {item["id"] for item in found["results"]} == set(PAGES)


def test_the_audit_corpus_lists_only_the_pages(scoped_vault: Path) -> None:
    pages = audit._parse_all(managed_root(scoped_vault), scoped_vault)
    _assert_pages_only({page.rel_path for page in pages})


def test_the_identity_census_lists_only_the_pages(scoped_vault: Path) -> None:
    census, _sources = semantic_contract._build_identity_census(scoped_vault)
    _assert_pages_only({entry.path for entry in census.entries})


def test_the_ownership_walk_lists_only_the_pages(scoped_vault: Path) -> None:
    _assert_pages_only({owned.rel for owned in ownership.iter_owned(scoped_vault)})


def test_the_working_set_and_media_scans_list_only_the_pages(scoped_vault: Path) -> None:
    _assert_pages_only(_rels(scoped_vault, working_set_index._walk_kb(scoped_vault)))
    listed = _rels(scoped_vault, backfill.iter_kb_files(managed_root(scoped_vault)))
    _assert_pages_only(listed)
    below = _rels(scoped_vault, backfill.iter_kb_files(scoped_vault / "Scratch", subtree=True))
    assert below == {"Scratch/x.md"}


def test_the_whole_vault_walk_keeps_the_unmanaged_folder_and_drops_dot_and_staging_folders(scoped_vault: Path) -> None:
    """The vault scope is not the managed pages: the unmanaged folder is in it, as outside content is in `nested`."""
    listed = _rels(scoped_vault, vault.walk_vault_md(scoped_vault))
    assert {*PAGES, "Scratch/x.md"} <= listed
    assert listed.isdisjoint(OUTSIDE_THE_VAULT_SCOPE)


def test_a_page_is_in_a_scope_when_the_walk_of_that_scope_lists_it(scoped_vault: Path) -> None:
    """A store-mode commit and a changed-file event judge a page from its path, so they must agree with the walks."""
    in_kb = _rels(scoped_vault, find_corpus.walk_md(managed_root(scoped_vault)))
    in_vault = _rels(scoped_vault, vault.walk_vault_md(scoped_vault))
    for rel in (*PAGES, *OUTSIDE):
        assert lexstore.page_membership(rel) == (rel in in_kb, rel in in_vault), rel
        assert freshness.scopes_for(scoped_vault, scoped_vault / rel) == (rel in in_kb, rel in in_vault), rel
        assert store_read.corpus_member(rel, "kb") == (rel in in_kb), rel
        assert store_read.corpus_member(rel, "vault") == (rel in in_vault), rel


def test_the_root_vault_paths_answer_for_their_own_folders(scoped_vault: Path) -> None:
    """The predicates looked for a wrapper folder; a root vault keeps its trash, sources and evidence at the root."""
    assert vault.in_append_only_tree("Sources/Articles/x.md") == "Sources"
    assert vault.in_append_only_tree("Evidence/Pump case/Readings/x.csv") == "Evidence"
    assert vault.in_append_only_tree("Insights/x.md") is None
    assert is_trash_path("_trash") and is_trash_path("_trash/Insights/x.md")
    assert is_inside_trash("_trash/Insights/x.md") and not is_inside_trash("Insights/x.md")
    assert policy.is_workflow_state_path("_Schema/relation-reviews/a.json")
    assert not policy.is_workflow_state_path(f"{KB}/_Schema/relation-reviews/a.json")
    assert classify_logical("_Compiled/history/a.json").disposition is PathDisposition.RESERVED
    assert classify_logical("Insights/x.md").disposition is PathDisposition.ORDINARY
