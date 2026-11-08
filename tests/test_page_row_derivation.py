"""The lexical and graph per-page row builders derive from bytes, with no read of the page on disk.

OpenSpec make-vault-store-canonical D2, task 2.1. A store-mode commit derives rows for bytes that
no file holds yet, so `page_rows` and `page_graph_rows` take the bytes. `dependent_paths` finds the
pages whose links a new, removed or renamed page re-resolves.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from exomem import epistemic_graph, find_corpus, lexstore, semantic_index, vault
from exomem.vault import WikilinkResolver

ALPHA = "Knowledge Base/Notes/alpha.md"
BETA = "Knowledge Base/Notes/beta.md"
GAMMA = "Knowledge Base/Notes/gamma.md"
FRESH = "Knowledge Base/Notes/fresh.md"
HARBOUR = "Knowledge Base/Notes/harbour-2026.md"
BYSTANDER = "Knowledge Base/Notes/bystander.md"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(title: str, observation: str, link: str) -> bytes:
    return (
        f"---\ntype: insight\ntitle: {title}\nupdated: 2026-07-15\n---\n# {title}\n\n"
        f"## Observations\n- [config] {observation} ^obs\n\nSee [[{link}]].\n"
    ).encode()


def test_rows_derive_from_the_given_bytes_not_from_the_page_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, BETA, "---\ntitle: Beta\n---\n# Beta\n")
    _write(root, GAMMA, "---\ntitle: Gamma\n---\n# Gamma\n")
    # The disk copy names one title, one unit and one link ...
    _write(root, ALPHA, _page("Disk Alpha", "disk observation", "Gamma").decode())
    # ... and the bytes that the rows derive from name others. FRESH has no file at all.
    given = _page("Given Alpha", "given observation", "Beta")
    resolver = WikilinkResolver.from_entries(
        root, [(ALPHA, "Alpha"), (BETA, "Beta"), (GAMMA, "Gamma"), (FRESH, "Fresh")]
    )
    type_basis = epistemic_graph.producer_type_basis(root)

    for rel in (ALPHA, FRESH):
        page = find_corpus.parse_page(root / rel, 0.0, root, content=given, resolved_relative=rel)
        state = semantic_index.current_parent_index_state(root, rel, source=given.decode())
        lexical = lexstore.page_rows(
            root, rel, given, page=page, state=state, mtime_ns=7, in_kb=True, in_vault=True
        )
        graph = epistemic_graph.page_graph_rows(
            root, rel, given, page=page, state=state, resolver=resolver, type_basis=type_basis
        )

        catalogue = sqlite3.connect(":memory:")
        lexstore.create_catalogue_schema(catalogue)
        lexstore.write_page_rows(catalogue, lexical)
        assert catalogue.execute(
            "SELECT path, title, mtime_ns, content_hash FROM pages"
        ).fetchall() == [(rel, "Given Alpha", 7, hashlib.sha256(given).hexdigest())]
        units = [row[0] for row in catalogue.execute("SELECT content FROM semantic_units")]
        assert "given observation" in units
        assert not any("disk" in unit for unit in units)

        rows = sqlite3.connect(":memory:")
        epistemic_graph.create_row_schema(rows, publication=False)
        epistemic_graph.write_page_graph_rows(rows, graph)
        assert rows.execute(
            "SELECT title, source_hash FROM graph_nodes WHERE kind = 'file'"
        ).fetchall() == [("Given Alpha", vault.content_hash(given.decode()))]
        assert rows.execute("SELECT DISTINCT raw_target FROM graph_dependencies").fetchall() == [
            ("Beta",)
        ]


def test_dependent_paths_name_the_pages_whose_links_a_title_re_resolves(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    linkers = [
        ("Knowledge Base/Notes/linker-one.md", "Met [[Harbour Notes]]."),
        ("Knowledge Base/Notes/linker-two.md", "Also [[Harbour Notes]]."),
    ]
    for rel, sentence in linkers:
        _write(root, rel, f"---\ntype: insight\n---\n# Linker\n\n{sentence}\n")
    _write(root, BYSTANDER, "---\ntype: insight\n---\n# Bystander\n\nNo links.\n")
    titled = [(rel, "Linker") for rel, _sentence in linkers] + [(BYSTANDER, "Bystander")]
    holding_the_page = WikilinkResolver.from_entries(root, [*titled, (HARBOUR, "Harbour Notes")])
    expected = {rel for rel, _sentence in linkers}

    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()

    def dependents_of_the_page() -> set[str]:
        connection = sqlite3.connect(index.path)
        try:
            return epistemic_graph.dependent_paths(connection, {HARBOUR}, [holding_the_page])
        finally:
            connection.close()

    # A new page whose title the others link to: its stem names none of them, only its title does.
    assert dependents_of_the_page() == expected

    # Once the page is indexed the stored links resolve to it. Deleting it re-resolves them again.
    _write(root, HARBOUR, "---\ntype: insight\ntitle: Harbour Notes\n---\n# Harbour Notes\n")
    index.rebuild_all()
    assert dependents_of_the_page() == expected
