"""The unit row set reproduces the lexical unit rows and the graph's candidate nodes for the same bytes.

OpenSpec make-vault-store-canonical D10, task 5.1. `verify.derive_expected` derives a store's tables from
a vault's files, so it is the one place where the new `unit_rows` table sits beside the two row sets that
it merges. Each test reads the stored unit rows back and projects them again.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

import pytest

from exomem import (
    epistemic_graph,
    find_corpus,
    lexstore,
    semantic_index,
    semantic_language_registry,
    semantic_units,
    unit_rows,
    vault,
)
from exomem.relation_registry import core_registry as core_relations
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
    return f"---\ntype: insight\ntitle: {title}\n{identity}updated: 2026-07-15\n---\n# {title}\n\n{body}"


PAGES: dict[str, bytes] = {
    # An anchored compact unit.
    ANCHORED: _page(
        "Harbour tide", "## Observations\n- [config] The tide table is read at dawn ^tide\n",
        exomem_id=_ANCHORED_ID,
    ).encode(),
    # Two identical unanchored compact units: identity binds occurrence 1 and 2.
    TWINS: _page(
        "Gate latch", "## Observations\n- [config] The gate latch sticks\n- [config] The gate latch sticks\n",
        exomem_id=_TWINS_ID,
    ).encode(),
    # A unit whose text holds a reserved origin carrier, which the graph withholds and the lexical row keeps.
    CARRIER: _page(
        "Dock visit",
        '## Observations\n- [config] Visible text <!-- exomem-origin:v1 {"reason":"Neutral example."} --> after\n',
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
        "## Claim\n- id: pump-claim\n- check_by: 2026-12-31\n- tags: dock, pump\n- context: north dock\n\n"
        "The pump trips at night.\n",
    ).encode(),
    # A page whose structure is unavailable: its bytes are not UTF-8, so it has a page row and no unit.
    UNREADABLE: b"---\ntitle: Broken bytes\n---\n# Broken bytes\n\xff\xfe\n- [config] Unread ^unread\n",
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


def _stored_rows(expected: Path, path: str | None = None) -> list[unit_rows.UnitRow]:
    with closing(sqlite3.connect(expected)) as conn:
        conn.row_factory = sqlite3.Row
        where, arguments = ("WHERE path = ? ", (path,)) if path else ("", ())
        found = conn.execute(f"SELECT * FROM unit_rows {where}ORDER BY path, ordinal", arguments).fetchall()
    return [unit_rows.UnitRow.from_record(dict(row)) for row in found]


def _page_columns(expected: Path) -> dict[str, dict[str, object]]:
    with closing(sqlite3.connect(expected)) as conn:
        conn.row_factory = sqlite3.Row
        return {row["path"]: dict(row) for row in conn.execute("SELECT * FROM pages")}


def _differing(conn: sqlite3.Connection, one: str, other: str) -> int:
    return sum(
        conn.execute(f"SELECT COUNT(*) FROM (SELECT * FROM {a} EXCEPT SELECT * FROM {b})").fetchone()[0]
        for a, b in ((one, other), (other, one))
    )


def test_the_lexical_projection_of_the_unit_rows_is_the_semantic_units_table(derived: Derived) -> None:
    """A unit row that loses a field in the store would leave the lexical row and its projection apart."""
    pages = _page_columns(derived.expected)
    with closing(sqlite3.connect(":memory:")) as projected:
        lexstore.create_catalogue_schema(projected)
        columns = [name for (name,) in projected.execute("SELECT name FROM pragma_table_info('semantic_units')")]
        for row in _stored_rows(derived.expected):
            values = unit_rows.lexical_columns(row, pages[row.path])
            projected.execute(
                f"INSERT INTO semantic_units({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()),
            )
        projected.execute("ATTACH DATABASE ? AS derived", (str(derived.expected),))
        select = ", ".join(name for name in columns if name != "rowid")
        for name in ("main", "derived"):
            projected.execute(f"CREATE TEMP VIEW units_{name} AS SELECT {select} FROM {name}.semantic_units")
        stored = projected.execute("SELECT COUNT(*) FROM units_derived").fetchone()[0]
        assert stored >= len(PAGES) - 1, "the fixture vault must hold units for this check to mean anything"
        assert _differing(projected, "units_main", "units_derived") == 0
        stored_keys = {(row.path, row.occurrence_key, key) for row in _stored_rows(derived.expected) for key in row.row_keys}
        derived_keys = set(projected.execute("SELECT parent_path, occurrence_key, row_key FROM derived.unit_row_keys"))
        assert stored_keys == derived_keys != set()


def test_the_graph_projection_of_the_unit_rows_is_the_graph_candidate_nodes(derived: Derived) -> None:
    """Knowledge Base pages project the stored unit rows to the graph; a copy outside it has no node."""
    root = derived.root
    resolver = vault.WikilinkResolver.from_entries(root, [(rel, rel) for rel in GRAPH_PAGES])
    type_basis = epistemic_graph.producer_type_basis(root)
    with closing(sqlite3.connect(":memory:")) as projected:
        epistemic_graph.create_row_schema(projected, publication=False)
        for rel in GRAPH_PAGES:
            raw = PAGES[rel]
            page = find_corpus.parse_page(root / rel, 0.0, root, content=raw, resolved_relative=rel)
            state = semantic_index.current_parent_index_state(root, rel, source=raw.decode())
            graph = epistemic_graph.page_graph_rows(
                root, rel, raw, page=page, state=state, resolver=resolver, type_basis=type_basis,
                target_exists=lambda _rel: False, landing=lambda *_: pytest.fail("the fixture has no link"),
                prepared_unit_rows=_stored_rows(derived.expected, rel),
            )
            epistemic_graph.write_page_graph_rows(projected, graph, epistemic_graph.graph_registry_stamps(root))
        projected.execute("ATTACH DATABASE ? AS derived", (str(derived.expected),))
        select = "SELECT {} FROM {}.graph_nodes WHERE kind = 'candidate'"
        columns = ", ".join(
            name for (name,) in projected.execute("SELECT name FROM pragma_table_info('graph_nodes')")
            if name not in ("rowid", "node_id")
        )
        for name in ("main", "derived"):
            projected.execute(f"CREATE TEMP VIEW nodes_{name} AS {select.format(columns, name)}")
        assert projected.execute("SELECT COUNT(*) FROM nodes_derived").fetchone()[0] >= len(GRAPH_PAGES)
        assert _differing(projected, "nodes_main", "nodes_derived") == 0
        outside = projected.execute(
            "SELECT COUNT(*) FROM derived.graph_nodes WHERE path = ?", (OUTSIDE,)
        ).fetchone()[0]
        assert outside == 0
    assert _stored_rows(derived.expected, OUTSIDE), "the copy outside the Knowledge Base keeps its unit rows"


def test_references_bound_from_the_stored_identity_facts_are_the_ones_the_page_summary_binds(
    derived: Derived,
) -> None:
    """Binding at read time needs only the facts in the rows: an overlay change cannot leave a ref stale."""
    core = semantic_language_registry.core_registry()
    stored_by_page: dict[str, list[unit_rows.UnitRow]] = {}
    for row in _stored_rows(derived.expected):
        stored_by_page.setdefault(row.path, []).append(row)
    with closing(sqlite3.connect(derived.expected)) as conn:
        summaries = dict(conn.execute("SELECT path, structure_json FROM pages WHERE structure_json IS NOT NULL"))
    assert UNREADABLE not in summaries

    def bind(summary: dict, parent_ref: str | None, rel: str) -> list[tuple[str, str | None, str | None]]:
        selected = semantic_units.interpret_structural_summary(
            summary, language_registry=core, relation_registry=core_relations(), parent_ref=parent_ref, path=rel,
        )
        return [(unit.occurrence_key, unit.unit_ref, unit.fingerprint) for unit in selected.units]

    for rel, raw_summary in summaries.items():
        metadata = json.loads(raw_summary)
        summary = metadata["structure"]
        rows = {row.occurrence_key: row for row in stored_by_page.get(rel, [])}
        for item in (*summary["rich"], *summary["compact"]):
            row = rows.get(item["key"])
            item["identity"] = None if row is None else {
                "signature": row.identity_signature, "fingerprints": list(row.identity_fingerprints),
            }
        original = json.loads(raw_summary)["structure"]
        assert bind(summary, metadata["parent_ref"], rel) == bind(original, metadata["parent_ref"], rel)
        assert bind(summary, metadata["parent_ref"], rel), f"{rel} binds no unit"
    twins = bind(json.loads(summaries[TWINS])["structure"], _memory_ref(_TWINS_ID), TWINS)
    # The two identical unanchored units bind occurrence 1 and 2, so their references differ.
    assert len({unit_ref for _key, unit_ref, _fingerprint in twins}) == len(twins) == 2


def _memory_ref(identity: str) -> str:
    return f"exomem://memory/{identity}"


def test_the_lexical_rows_and_graph_nodes_say_what_the_parser_emits_for_the_same_bytes(derived: Derived) -> None:
    """The parser's own selected units are the oracle that no projection of the unit rows can share a defect with."""
    with closing(sqlite3.connect(derived.expected)) as conn:
        compared = 0
        for rel in (*GRAPH_PAGES, OUTSIDE):
            raw = PAGES[rel]
            state = semantic_index.current_parent_index_state(derived.root, rel, source=raw.decode())
            lexical = {
                (form, line): rest for form, line, *rest in conn.execute(
                    "SELECT form, line, end_line, anchor, content, tags_json, context FROM semantic_units "
                    "WHERE parent_path = ?", (rel,),
                )
            }
            nodes = {
                (json.loads(metadata)["form"], line): rest for metadata, line, *rest in conn.execute(
                    "SELECT metadata, line_start, line_end, anchor, text FROM graph_nodes "
                    "WHERE path = ? AND kind = 'candidate'", (rel,),
                )
            }
            for unit in semantic_units.parse_semantic_units(state.body, path=rel).units:
                tags = json.dumps(list(unit.tags), separators=(",", ":"))
                assert lexical[(unit.form, unit.line)] == [unit.end_line, unit.anchor, unit.content, tags, unit.context]
                if rel != OUTSIDE:
                    assert nodes[(unit.form, unit.line)] == [unit.end_line, unit.anchor, unit.content]
                compared += 1
    assert compared >= 7, "the fixture vault must emit units for this check to mean anything"


def test_each_projection_keeps_the_rule_that_the_other_does_not_share(derived: Derived) -> None:
    """The lexical row keeps the authored anchor and never a fingerprint; the graph names a fallback anchor."""
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
    assert any("Visible text" in text for (path, _line), (_a, text) in graph.items() if path == CARRIER)


def test_a_page_with_no_structure_has_a_page_row_and_no_unit_row(derived: Derived) -> None:
    assert UNREADABLE in _page_columns(derived.expected)
    assert _stored_rows(derived.expected, UNREADABLE) == []
