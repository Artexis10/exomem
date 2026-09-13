"""Current-source Basic Memory proofs use exact scoped SQLite and public identities."""
from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def adapter():
    path = SCRIPTS / "parity_basic_memory.py"
    assert path.is_file(), "current-source Basic Memory adapter is missing"
    spec = importlib.util.spec_from_file_location("parity_basic_memory", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def database(state: Path, *, version: str = "y8f9a0b1c2d3",
             last_indexed_at: str | None = "2026-01-01T00:00:00+00:00") -> Path:
    store = state / "config/memory.db"
    store.parent.mkdir(parents=True)
    with sqlite3.connect(store) as conn:
        conn.executescript("""
            CREATE TABLE alembic_version (version_num TEXT);
            CREATE TABLE project (id INTEGER, name TEXT, path TEXT, last_indexed_at TEXT);
            CREATE TABLE entity (id INTEGER, file_path TEXT, project_id INTEGER);
            CREATE VIRTUAL TABLE search_index USING fts5(id UNINDEXED, file_path UNINDEXED,
                project_id UNINDEXED, entity_id UNINDEXED, type UNINDEXED, content);
            CREATE TABLE note_content (entity_id INTEGER, project_id INTEGER, db_version INTEGER);
            CREATE TABLE relation_search_refresh (id INTEGER, project_id INTEGER, entity_id INTEGER);
            CREATE TABLE relation (id INTEGER, from_id INTEGER, to_id INTEGER, to_name TEXT,
                relation_type TEXT, project_id INTEGER, generation INTEGER);
        """)
        conn.execute("INSERT INTO alembic_version VALUES (?)", (version,))
        conn.execute("INSERT INTO project VALUES (1,'main',?,?)", (str(state / "home"), last_indexed_at))
        for identity, path in enumerate(("active-tracker.md", "archived-runbook.md", "background.md"), 1):
            conn.execute("INSERT INTO entity VALUES (?,?,1)", (identity, path))
            conn.execute("INSERT INTO search_index VALUES (?,?,1,?,'entity','body')", (identity, path, identity))
            conn.execute("INSERT INTO note_content VALUES (?,1,7)", (identity,))
        conn.execute("INSERT INTO relation VALUES (1,1,3,'Background','supports',1,7)")
    return store


FIXTURE = {"pages": [{"path": path} for path in ("active-tracker.md", "archived-runbook.md", "background.md")]}


def test_prepare_uses_shared_bytes_and_isolates_source_and_configuration(tmp_path, monkeypatch):
    bm = adapter()
    import parity_fixture
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", "/should/not/read")
    monkeypatch.setenv("EXOMEM_STATE_ROOT", "/should/not/read")
    state, vault, source = tmp_path / "state", tmp_path / "vault", tmp_path / "source"
    env, fixture, corpus = bm.prepare(state, vault, 4, source=source)
    assert corpus == state / "home"
    assert (corpus / bm.TRACKER).read_text() == parity_fixture.tracker_body(4)
    assert fixture["page_count"] == 4
    assert env["PYTHONPATH"] == str(source / "src")
    assert env["BASIC_MEMORY_CONFIG_DIR"] == str(state / "config")
    assert env["BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED"] == "false"
    assert "EXOMEM_STATE_ROOT" not in env
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert Path(env[key]).is_relative_to(state)


def test_index_requires_exact_entity_search_identity_multisets(tmp_path):
    bm = adapter()
    store = database(tmp_path)
    proof = bm.inspect_index(tmp_path, FIXTURE)
    assert proof["ready"], proof
    assert proof["alembic"] == bm.ALEMBIC
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE search_index SET entity_id=99 WHERE file_path='active-tracker.md'")
    assert not bm.inspect_index(tmp_path, FIXTURE)["ready"]


@pytest.mark.parametrize("mutation", [
    "DELETE FROM entity WHERE id=3",
    "UPDATE entity SET file_path='stale.md' WHERE id=3",
    "INSERT INTO search_index SELECT * FROM search_index WHERE id=1",
    "UPDATE project SET path='/outside' WHERE name='main'",
    "UPDATE search_index SET project_id=2 WHERE id=1",
])
def test_index_rejects_missing_stale_duplicate_and_foreign_identity(tmp_path, mutation):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.execute(mutation)
    assert not bm.inspect_index(tmp_path, FIXTURE)["ready"]


def test_missing_store_is_unready_without_creating_it(tmp_path):
    bm = adapter()
    assert not bm.inspect_index(tmp_path, FIXTURE)["ready"]
    assert not bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)["ready"]
    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize("version", ["old-head", ""])
def test_unsupported_schema_is_an_adapter_fault(tmp_path, version):
    bm = adapter()
    database(tmp_path, version=version)
    with pytest.raises(bm.common.AdapterFault, match="schema"):
        bm.inspect_index(tmp_path, FIXTURE)
    with pytest.raises(bm.common.AdapterFault, match="schema"):
        bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)


def test_relation_proves_current_typed_resolved_edge_and_old_edge_absence(tmp_path):
    bm = adapter()
    database(tmp_path)
    proof = bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)
    assert proof["ready"], proof
    assert proof["present"] and not proof["absent_target_present"]
    assert proof["generation"] == 7


@pytest.mark.parametrize("mutation", [
    "UPDATE relation SET relation_type='links_to'",
    "UPDATE relation SET from_id=2",
    "UPDATE relation SET to_id=NULL",
    "UPDATE relation SET project_id=2",
    "UPDATE entity SET project_id=2 WHERE id=3",
    "UPDATE relation SET generation=6",
    "INSERT INTO relation VALUES (2,1,2,'Archived Runbook','supports',1,7)",
    "INSERT INTO relation VALUES (2,1,NULL,'Archived Runbook','supports',1,7)",
])
def test_relation_rejects_wrong_unresolved_foreign_stale_and_retained_edges(tmp_path, mutation):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.execute(mutation)
    assert not bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)["ready"]


def test_read_requires_exact_body_and_tracker_identity():
    bm = adapter()
    payload = {"file_path": bm.TRACKER, "content": "---\ntitle: tracker\n---\n# Body\n"}
    assert bm.verify_read(payload, "# Body\n")
    assert bm.verify_read(payload, "# Body")
    assert not bm.verify_read(payload, "# Body \n")
    assert not bm.verify_read(payload, "# Body\n\n")
    assert not bm.verify_read({**payload, "content": "# Body changed\n"}, "# Body\n")
    assert not bm.verify_read({**payload, "file_path": "other.md"}, "# Body\n")


def test_search_rejects_echo_lookalike_path_and_non_entity():
    bm = adapter()
    hit = {"type": "entity", "file_path": bm.TRACKER, "entity": "main/active-tracker", "content": "State: paritymarker123"}
    assert bm.verify_search({"results": [hit]}, "paritymarker123")
    assert not bm.verify_search({"query": "paritymarker123", "results": []}, "paritymarker123")
    for update in ({"file_path": "other.md"}, {"entity": "elsewhere/active-tracker"}, {"type": "relation"}, {"content": "stale"}):
        assert not bm.verify_search({"results": [{**hit, **update}]}, "paritymarker123")


def graph_payload():
    return {"has_more": False, "results": [{
        "primary_result": {"type": "entity", "file_path": "background.md", "entity_id": 3, "permalink": "main/background"},
        "related_results": [{"type": "relation", "file_path": "active-tracker.md", "relation_type": "supports", "from_entity_id": 1,
                             "to_entity_id": 3, "to_name": "Background", "to_entity": "background", "permalink": ""},
                            {"type": "entity", "file_path": "active-tracker.md", "entity_id": 1, "permalink": "main/active-tracker"}]
    }]}


def test_public_graph_requires_primary_identity_and_resolved_typed_target():
    bm = adapter()
    payload = graph_payload()
    assert bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)
    for field, value in (("from_entity_id", 99), ("to_entity_id", None), ("relation_type", "links_to"), ("file_path", "other.md"), ("to_name", "Other")):
        altered = copy.deepcopy(payload)
        altered["results"][0]["related_results"][0][field] = value
        assert not bm.verify_graph(altered, bm.NEW_TARGET, bm.OLD_TARGET)
    payload["results"][0]["primary_result"]["file_path"] = "other.md"
    assert not bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)


def test_public_graph_rejects_hidden_stale_edge_and_truncation():
    bm = adapter()
    payload = graph_payload()
    assert not bm.verify_graph({**payload, "has_more": True}, bm.NEW_TARGET, bm.OLD_TARGET)
    payload["results"][0]["related_results"].append({"type": "relation", "file_path": bm.TRACKER, "from_entity_id": 1,
        "to_name": "Archived Runbook", "relation_type": "supports", "to_entity_id": None})
    assert not bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)


def test_public_argument_helpers_use_supported_explicit_json_contracts():
    bm = adapter()
    tool, edit = bm.edit_arguments("old", "new")
    assert tool == "edit_note" and edit["operation"] == "find_replace"
    assert edit["expected_replacements"] == 1 and edit["find_text"] == "old" and edit["content"] == "new"
    assert bm.append_arguments("token")[1]["operation"] == "append"
    for _tool, arguments in (bm.read_arguments(), bm.search_arguments("marker"), bm.graph_arguments(), bm.append_arguments("token")):
        assert arguments["project"] == "main" and arguments["output_format"] == "json"
    assert bm.graph_arguments()[1]["page_size"] <= 50


def test_public_graph_rejects_old_resolved_endpoint_even_when_label_differs():
    bm = adapter()
    payload = graph_payload()
    payload["results"][0]["related_results"].extend([
        {"type": "entity", "entity_id": 2, "file_path": bm.OLD_TARGET, "permalink": "main/archived-runbook"},
        {"type": "relation", "file_path": bm.TRACKER, "from_entity_id": 1,
         "to_name": "an alias", "relation_type": "supports", "to_entity_id": 2},
    ])
    assert not bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)


@pytest.mark.parametrize("content", ["prefixparitymarker123", "paritymarker1234", "paritymarker123_suffix"])
def test_search_rejects_marker_prefix_and_suffix_lookalikes(content):
    bm = adapter()
    hit = {"type": "entity", "file_path": bm.TRACKER, "entity": "main/active-tracker", "content": content}
    assert not bm.verify_search({"results": [hit]}, "paritymarker123")


def test_graph_arguments_seed_the_requested_target_with_bounded_depth():
    bm = adapter()
    assert bm.graph_arguments()[1]["url"] == "memory://archived-runbook"
    arguments = bm.graph_arguments(bm.NEW_TARGET)[1]
    assert arguments["url"] == "memory://background"
    assert arguments["depth"] == 1 and arguments["max_related"] == 100


def test_public_graph_looks_up_exact_source_identity_among_unrelated_rows():
    bm = adapter()
    payload = graph_payload()
    related = payload["results"][0]["related_results"]
    related[:0] = [{"type": "entity", "file_path": f"reference-{i:05d}.md", "entity_id": 100+i,
                    "permalink": f"main/reference-{i:05d}"} for i in range(95)]
    assert bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)
    source = related[-1]
    source["file_path"] = "tracker-lookalike.md"
    assert not bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)
    source["file_path"] = bm.TRACKER
    source["entity_id"] = 99
    assert not bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)


def test_public_graph_rejects_reverse_direction_despite_both_endpoint_entities():
    bm = adapter()
    payload = graph_payload()
    edge = payload["results"][0]["related_results"][0]
    edge.update(from_entity_id=3, to_entity_id=1)
    assert not bm.verify_graph(payload, bm.NEW_TARGET, bm.OLD_TARGET)


def test_relation_proof_records_wrong_types_stale_generations_and_scoped_refresh_debt(tmp_path):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE relation SET relation_type='links_to', generation=6")
        conn.executemany("INSERT INTO relation_search_refresh VALUES (?,?,?)", [(1,1,1),(2,1,1),(3,2,1),(4,1,2)])
    proof = bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)
    assert not proof["ready"]
    assert proof["source_db_version"] == 7
    assert proof["source_entity_id"] == 1
    assert proof["relation_search_refresh_count"] == 2
    assert proof["observed_relations"] == [{"relation_id": 1, "from_id": 1, "to_id": 3,
        "to_name": "Background", "target_path": "background.md", "relation_type": "links_to", "generation": 6}]


def test_relation_proof_rejects_missing_refresh_table_for_pinned_schema(tmp_path):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.execute("DROP TABLE relation_search_refresh")
    with pytest.raises(bm.common.AdapterFault, match="schema"):
        bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)


def test_append_keeps_concurrent_tokens_outside_the_relation_list_item():
    import parity_fixture
    from markdown_it import MarkdownIt
    bm = adapter()
    body = parity_fixture.tracker_body(4)
    for token in ("parityconcurrent001", "parityconcurrent002"):
        body = body.rstrip() + "\n" + bm.append_arguments(token)[1]["content"]
    inline = [token.content for token in MarkdownIt().parse(body) if token.type == "inline"]
    assert "supports [[Archived Runbook]]" in inline
    assert "parityconcurrent001" in inline and "parityconcurrent002" in inline


def test_relation_readiness_waits_only_for_this_sources_refresh_work(tmp_path):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.executemany("INSERT INTO relation_search_refresh VALUES (?,?,?)", [(1,2,1),(2,1,2)])
    assert bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)["ready"]
    with sqlite3.connect(store) as conn:
        conn.execute("INSERT INTO relation_search_refresh VALUES (3,1,1)")
    proof = bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)
    assert proof["present"] and proof["relations_current"] and not proof["absent_target_present"]
    assert proof["relation_search_refresh_count"] == 1
    assert not proof["ready"]
    with sqlite3.connect(store) as conn:
        conn.execute("DELETE FROM relation_search_refresh WHERE id=3")
    assert bm.inspect_relation(tmp_path, bm.TRACKER, bm.NEW_TARGET, bm.OLD_TARGET)["ready"]


def test_initial_index_waits_for_native_completion_despite_exact_membership(tmp_path):
    bm = adapter()
    store = database(tmp_path, last_indexed_at=None)
    proof = bm.inspect_index(tmp_path, FIXTURE)
    assert not proof["ready"]
    assert proof["last_indexed_at"] is None
    assert proof["observed_path_count"] == proof["search_entity_row_count"] == 3
    assert "project indexing" in proof["reason"]
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE project SET last_indexed_at='2026-01-01T00:00:00+00:00' WHERE id=1")
    proof = bm.inspect_index(tmp_path, FIXTURE)
    assert proof["ready"] and proof["last_indexed_at"] == "2026-01-01T00:00:00+00:00"
    assert proof["observed_path_digest"] == proof["search_entity_path_digest"]
    assert proof["missing_search_identity_count"] == proof["extra_search_identity_count"] == 0
    assert proof["misbound_search_entity_id_count"] == 0


@pytest.mark.parametrize("mutation,missing,extra,misbound,search_count", [
    ("DELETE FROM search_index WHERE id=2", [(2,"archived-runbook.md",1)], [], 0, 2),
    ("INSERT INTO search_index VALUES (99,'extra.md',1,99,'entity','body')", [], [(99,"extra.md",1)], 0, 4),
    ("UPDATE search_index SET id=99 WHERE id=1", [(1,"active-tracker.md",1)], [(99,"active-tracker.md",1)], 1, 3),
    ("UPDATE search_index SET entity_id=99 WHERE id=1", [], [], 1, 3),
    ("INSERT INTO search_index SELECT * FROM search_index WHERE id=1", [], [(1,"active-tracker.md",1)], 0, 4),
])
def test_initial_index_retains_exact_search_identity_mismatch_diagnostics(tmp_path, mutation, missing, extra, misbound, search_count):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.execute(mutation)
        paths = [row[0] for row in conn.execute("SELECT file_path FROM search_index WHERE type='entity' AND project_id=1")]
    proof = bm.inspect_index(tmp_path, FIXTURE)
    assert not proof["ready"]
    assert proof["observed_path_count"] == 3
    assert proof["search_entity_row_count"] == search_count
    assert proof["search_entity_path_digest"] == bm.common._membership_digest(paths)
    assert proof["missing_search_identity_count"] == len(missing)
    assert proof["extra_search_identity_count"] == len(extra)
    assert proof["missing_search_identity_digest"] == bm.common._membership_digest(
        [json.dumps(row, separators=(",", ":")) for row in missing])
    assert proof["extra_search_identity_digest"] == bm.common._membership_digest(
        [json.dumps(row, separators=(",", ":")) for row in extra])
    assert proof["misbound_search_entity_id_count"] == misbound


def test_initial_index_rejects_missing_native_completion_column(tmp_path):
    bm = adapter()
    store = database(tmp_path)
    with sqlite3.connect(store) as conn:
        conn.execute("ALTER TABLE project DROP COLUMN last_indexed_at")
    with pytest.raises(bm.common.AdapterFault, match="schema"):
        bm.inspect_index(tmp_path, FIXTURE)


def test_initial_index_stamp_and_membership_share_the_read_snapshot(tmp_path, monkeypatch):
    bm = adapter()
    store = database(tmp_path, last_indexed_at=None)
    with sqlite3.connect(store) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original_project = bm._project

    def publish_after_binding(conn, state):
        project = original_project(conn, state)
        with sqlite3.connect(store) as writer:
            writer.execute("UPDATE project SET last_indexed_at='2026-01-01T00:00:00+00:00' WHERE id=1")
            writer.execute("DELETE FROM entity WHERE id=3")
        return project

    monkeypatch.setattr(bm, "_project", publish_after_binding)
    proof = bm.inspect_index(tmp_path, FIXTURE)
    assert not proof["ready"]
    assert proof["last_indexed_at"] is None and proof["observed_path_count"] == 3
    with sqlite3.connect(store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 2
        assert conn.execute("SELECT last_indexed_at FROM project").fetchone()[0] is not None
