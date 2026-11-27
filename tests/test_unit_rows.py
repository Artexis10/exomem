"""The one unit row builder reproduces the lexical unit rows and the graph's candidate nodes.

OpenSpec make-vault-store-canonical D10, task 5.1. For the same bytes, `verify.derive_expected`
derives a store's tables from a vault's files, so it holds the lexical rows and the graph nodes
that the one builder projects. The parser's own units are the oracle for both.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

import pytest

from exomem import semantic_index, semantic_units
from exomem.vault_store import verify

ANCHORED = "Knowledge Base/Notes/harbour-tide.md"
TWINS = "Knowledge Base/Notes/gate-latch.md"
CARRIER = "Knowledge Base/Notes/dock-visit.md"
RICH = "Knowledge Base/Notes/sea-wall.md"
GOVERNED = "Knowledge Base/Notes/pump-station.md"
UNREADABLE = "Knowledge Base/Notes/broken-bytes.md"
OUTSIDE = "Notes/copy-of-harbour-tide.md"

_ANCHORED_ID = "11111111-1111-4111-8111-111111111111"
_TWINS_ID = "22222222-2222-4222-8222-222222222222"


def _page(title: str, body: str, *, exomem_id: str | None = None) -> str:
    identity = f"exomem_id: {exomem_id}\n" if exomem_id else ""
    head = f"---\ntype: insight\ntitle: {title}\n{identity}updated: 2026-07-15\n---\n"
    return f"{head}# {title}\n\n{body}"


PAGES: dict[str, bytes] = {
    # An anchored compact unit.
    ANCHORED: _page(
        "Harbour tide", "## Observations\n- [config] The tide table is read at dawn ^tide\n",
        exomem_id=_ANCHORED_ID,
    ).encode(),
    # Two identical unanchored compact units: identity binds occurrence 1 and 2.
    TWINS: _page(
        "Gate latch",
        "## Observations\n- [config] The gate latch sticks\n- [config] The gate latch sticks\n",
        exomem_id=_TWINS_ID,
    ).encode(),
    # A unit whose text holds a reserved origin carrier, which the graph withholds and the lexical
    # row keeps.
    CARRIER: _page(
        "Dock visit",
        "## Observations\n- [config] Visible text "
        '<!-- exomem-origin:v1 {"reason":"Neutral example."} --> after\n',
    ).encode(),
    # A rich unit with an id, a compact unit nested in it, and a rich unit without an id.
    RICH: _page(
        "Sea wall",
        "## Claim\n- id: wall-claim\n\nThe sea wall holds at spring tide.\n"
        "- [config] Nested observation ^nested\n\n## Another claim\nA claim with no id.\n",
    ).encode(),
    # Governed columns: tags, context and a check-by day.
    GOVERNED: _page(
        "Pump station",
        "## Observations\n- [config] The pump trips at night #maintenance (north dock) ^pump\n\n"
        "## Claim\n- id: pump-claim\n- check_by: 2026-12-31\n- tags: dock, pump\n"
        "- context: north dock\n\nThe pump trips at night.\n",
    ).encode(),
    # A page whose structure is unavailable: its bytes are not UTF-8, so it has a page row and no
    # unit.
    UNREADABLE: (
        b"---\ntitle: Broken bytes\n---\n# Broken bytes\n\xff\xfe\n- [config] Unread ^unread\n"
    ),
    # A copy of a page outside the Knowledge Base: lexical rows only.
    OUTSIDE: _page(
        "Copy of harbour tide", "## Observations\n- [config] The tide table is read at dawn ^copy\n"
    ).encode(),
}
GRAPH_PAGES = tuple(rel for rel in PAGES if rel.startswith("Knowledge Base/") and rel != UNREADABLE)


class Derived(NamedTuple):
    root: Path
    expected: Path


@pytest.fixture
def derived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Derived:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    for rel, raw in PAGES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(raw)
    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(root, expected)
    return Derived(root, expected)


def test_the_lexical_rows_and_graph_nodes_say_what_the_parser_emits_for_the_same_bytes(
    derived: Derived,
) -> None:
    """The parser's own units are the oracle that no projection of the unit rows can share a defect
    with."""
    with closing(sqlite3.connect(derived.expected)) as conn:
        compared = 0
        for rel in (*GRAPH_PAGES, OUTSIDE):
            raw = PAGES[rel]
            state = semantic_index.current_parent_index_state(
                derived.root, rel, source=raw.decode()
            )
            lexical = {
                (form, line): rest for form, line, *rest in conn.execute(
                    "SELECT form, line, end_line, anchor, content, tags_json, context "
                    "FROM semantic_units WHERE parent_path = ?", (rel,),
                )
            }
            nodes = {
                (json.loads(metadata)["form"], line): rest
                for metadata, line, *rest in conn.execute(
                    "SELECT metadata, line_start, line_end, anchor, text FROM graph_nodes "
                    "WHERE path = ? AND kind = 'candidate'", (rel,),
                )
            }
            for unit in semantic_units.parse_semantic_units(state.body, path=rel).units:
                tags = json.dumps(list(unit.tags), separators=(",", ":"))
                assert lexical[(unit.form, unit.line)] == [
                    unit.end_line, unit.anchor, unit.content, tags, unit.context
                ]
                if rel != OUTSIDE:
                    assert nodes[(unit.form, unit.line)] == [
                        unit.end_line, unit.anchor, unit.content
                    ]
                compared += 1
    assert compared >= 7, "the fixture vault must emit units for this check to mean anything"


def test_each_projection_keeps_the_rule_that_the_other_does_not_share(derived: Derived) -> None:
    """The lexical row keeps the authored anchor and no fingerprint; the graph names a fallback."""
    with closing(sqlite3.connect(derived.expected)) as conn:
        lexical = {
            (path, form, line): (anchor, content, fingerprint)
            for path, form, line, anchor, content, fingerprint in conn.execute(
                "SELECT parent_path, form, line, anchor, content, fingerprint FROM semantic_units"
            )
        }
        graph = {
            (path, line): (anchor, text)
            for path, line, anchor, text in conn.execute(
                "SELECT path, line_start, anchor, text FROM graph_nodes WHERE kind = 'candidate'"
            )
        }
    # A rich unit without an `id`: the lexical anchor is empty, the graph's is the normalised title.
    (heading_line,) = [
        line for (path, form, line), (anchor, _content, _fingerprint) in lexical.items()
        if path == RICH and form == "rich" and anchor is None
    ]
    assert graph[(RICH, heading_line)][0] == "another_claim"
    # A lexical row never holds a fingerprint: a reader binds it from the identity facts.
    assert {fingerprint for _anchor, _content, fingerprint in lexical.values()} == {None}
    # No projection carries an origin payload, and the unit around it keeps its visible text.
    assert not [text for (_a, text, _f) in lexical.values() if "exomem-origin" in text]
    assert not [text for (_a, text) in graph.values() if "exomem-origin" in text]
    assert any(
        "Visible text" in text for (path, _line), (_a, text) in graph.items() if path == CARRIER
    )


def test_a_knowledge_base_page_has_the_same_units_in_the_lexical_rows_and_the_graph(
    derived: Derived,
) -> None:
    """One builder gives both row sets the same units; the copy outside the Knowledge Base has no
    graph node."""
    with closing(sqlite3.connect(derived.expected)) as conn:
        for rel in GRAPH_PAGES:
            lexical = set(
                conn.execute(
                    "SELECT unit_ref, line, end_line FROM semantic_units WHERE parent_path = ?",
                    (rel,),
                )
            )
            nodes = {
                (json.loads(metadata)["occurrence_key"], line, end_line)
                for metadata, line, end_line in conn.execute(
                    "SELECT metadata, line_start, line_end FROM graph_nodes "
                    "WHERE path = ? AND kind = 'candidate'", (rel,),
                )
            }
            assert lexical == nodes != set(), rel
        counts = [
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (OUTSIDE,)
            ).fetchone()[0]
            for table, column in (("graph_nodes", "path"), ("semantic_units", "parent_path"))
        ]
        assert counts[0] == 0 and counts[1] > 0


def test_a_page_with_no_structure_has_a_page_row_and_no_units(derived: Derived) -> None:
    with closing(sqlite3.connect(derived.expected)) as conn:
        pages = conn.execute("SELECT COUNT(*) FROM pages WHERE path = ?", (UNREADABLE,)).fetchone()
        assert pages[0] == 1
        for table, column in (("semantic_units", "parent_path"), ("graph_nodes", "path")):
            held = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (UNREADABLE,)
            ).fetchone()[0]
            assert held == 0, table
