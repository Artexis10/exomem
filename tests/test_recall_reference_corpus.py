"""Reference composition uses source membership while backend pages remain load."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import recall_latency_gate as gate  # noqa: E402, I001


@pytest.fixture(params=[False, True], ids=["lexical", "canonical"])
def reference_corpus(tmp_path, monkeypatch, request):
    from exomem import lexstore
    from exomem.vault_store import mode, read

    root = tmp_path / "reference"
    root.mkdir()
    rels = [f"Knowledge Base/Notes/reference-{i}.md" for i in range(4)]
    support = "Knowledge Base/Records/Example/_collection.md"
    database = tmp_path / "catalogue.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE pages(path TEXT PRIMARY KEY, content_hash TEXT, in_kb INTEGER, is_nav INTEGER);"
            "CREATE TABLE semantic_units(parent_path TEXT, in_kb INTEGER);"
            "CREATE TABLE fts(value TEXT); CREATE TABLE tri(value TEXT); CREATE TABLE unit_fts(value TEXT);"
        )
        for index, rel in enumerate([*rels, support]):
            content = ("---\nmedia_type: audio\n---\n" if index == 0 else "") + "alpha beta [[reference-0]]\n"
            if index in (0, 2):
                content += "\n".join(f"- [finding] Recorded measurement {j}." for j in range(3))
                conn.executemany("INSERT INTO semantic_units VALUES (?,1)", [(rel,)] * 3)
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            conn.execute("INSERT INTO pages VALUES (?,?,1,0)", (rel, hashlib.sha256(path.read_bytes()).hexdigest()))
    prose = tmp_path / "prose.txt"
    prose.write_text("alpha beta")
    profile = {
        "pages": 4, "unit_page_fraction": 0.5, "transcript_fraction": 0.25,
        "vocabulary_min": 2, "links_min": 1, "links_max": 1,
        "median_page_bytes": [1, 1000], "transcript_bytes": [1, 1000],
        "source": {"path": str(prose), "sha256": hashlib.sha256(prose.read_bytes()).hexdigest()},
    }
    monkeypatch.setattr(gate, "REFERENCE", {**gate.REFERENCE, "pages_min": 4, "units_min": 6})
    monkeypatch.setattr(lexstore, "lexical_path", lambda _root: database)
    monkeypatch.setattr(mode, "store_mode_for_reads", lambda _root: request.param)
    monkeypatch.setattr(read, "lend", lambda _root: sqlite3.connect(database) if request.param else None)
    return SimpleNamespace(root=root, rels=rels, support=support, database=database, profile=profile)


def test_a_backend_page_does_not_change_reference_composition(reference_corpus):
    fixture = reference_corpus
    corpus = gate._stored_corpus(fixture.root, None, fixture.rels, fixture.profile)

    assert corpus["pages"] == 5
    assert corpus["units"] == 6 and corpus["unit_pages"] == 2
    assert corpus["reference"] == {"paths": 4, "pages": 4, "units": 6, "unit_pages": 2}
    assert corpus["transcript_pages"] == 1
    gate.validate_document(corpus, "sql-calibration.schema.json", definition="corpus")


def test_a_backend_page_cannot_replace_a_missing_reference_page(reference_corpus):
    fixture = reference_corpus
    with sqlite3.connect(fixture.database) as conn:
        # This is not one of the three source-hash spot checks.
        conn.execute("DELETE FROM pages WHERE path=?", (fixture.rels[1],))

    with pytest.raises(gate.MeasurementRefused, match="stored-corpus-below-reference-profile"):
        gate._stored_corpus(fixture.root, None, fixture.rels, fixture.profile)


def test_backend_units_cannot_replace_reference_unit_parents(reference_corpus):
    fixture = reference_corpus
    with sqlite3.connect(fixture.database) as conn:
        conn.execute("DELETE FROM semantic_units WHERE parent_path=?", (fixture.rels[0],))
        conn.executemany("INSERT INTO semantic_units VALUES (?,1)", [(fixture.support,)] * 3)

    with pytest.raises(gate.MeasurementRefused, match="stored-corpus-below-reference-profile"):
        gate._stored_corpus(fixture.root, None, fixture.rels, fixture.profile)


def test_reference_transcript_shortfall_remains_a_refusal(reference_corpus):
    fixture = reference_corpus
    path = fixture.root / fixture.rels[0]
    path.write_text(path.read_text().removeprefix("---\nmedia_type: audio\n---\n"))
    with sqlite3.connect(fixture.database) as conn:
        conn.execute("UPDATE pages SET content_hash=? WHERE path=?",
                     (hashlib.sha256(path.read_bytes()).hexdigest(), fixture.rels[0]))

    with pytest.raises(gate.MeasurementRefused, match="stored-corpus-below-reference-profile"):
        gate._stored_corpus(fixture.root, None, fixture.rels, fixture.profile)


def test_duplicate_reference_paths_cannot_supply_missing_membership(reference_corpus):
    fixture = reference_corpus
    rels = [fixture.rels[0], fixture.rels[0], *fixture.rels[2:]]

    with pytest.raises(gate.MeasurementRefused, match="stored-corpus-below-reference-profile"):
        gate._stored_corpus(fixture.root, None, rels, fixture.profile)
