"""Current-source Basic Memory public arguments and read-only comparison proofs."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

import durable_closure_common as common
import parity_fixture

REVISION = "368e607622e9af3d982d0429cb48cfc6c83521f1"
ALEMBIC = "y8f9a0b1c2d3"
TRACKER = parity_fixture.TRACKER
OLD_TARGET = parity_fixture.OLD_TARGET
NEW_TARGET = parity_fixture.NEW_TARGET
_TARGET_NAMES = {OLD_TARGET: "Archived Runbook", NEW_TARGET: "Background"}


def prepare(state: Path, vault: Path, pages: int, *, source: Path) -> tuple[dict, dict, Path]:
    """Materialize shared bytes in roots already claimed by the caller."""
    env = common.basic_memory_environment(state, vault)
    env.update({"PYTHONPATH": str(source / "src"), **{
        f"XDG_{kind}_HOME": str(state / f"xdg-{kind.lower()}")
        for kind in ("STATE", "CONFIG", "CACHE", "DATA")
    }})
    corpus = Path(env["BASIC_MEMORY_HOME"])
    return env, parity_fixture.materialize(corpus, pages), corpus


def edit_arguments(old: str, new: str) -> tuple[str, dict]:
    return "edit_note", {"identifier": TRACKER, "operation": "find_replace",
                         "find_text": old, "content": new, "expected_replacements": 1,
                         "project": "main", "output_format": "json"}


def append_arguments(content: str) -> tuple[str, dict]:
    return "edit_note", {"identifier": TRACKER, "operation": "append", "content": "\n\n" + content + "\n\n",
                         "project": "main", "output_format": "json"}


def read_arguments() -> tuple[str, dict]:
    return "read_note", {"identifier": TRACKER, "project": "main", "output_format": "json",
                         "include_frontmatter": True}


def search_arguments(marker: str) -> tuple[str, dict]:
    return "search_notes", {"query": marker, "search_type": "text", "entity_types": ["entity"],
                            "project": "main", "output_format": "json"}


def graph_arguments(target: str = OLD_TARGET) -> tuple[str, dict]:
    return "build_context", {"url": f"memory://{Path(target).stem}", "depth": 1, "timeframe": None,
                             "page_size": 50, "max_related": 100,
                             "project": "main", "output_format": "json"}


def verify_read(payload: dict, expected: str) -> bool:
    return (common.result_classification(payload) == "ok" and payload.get("file_path") == TRACKER
            and parity_fixture.read_body_equals(payload, expected))


def verify_search(payload: dict, marker: str) -> bool:
    rows = payload.get("results")
    if common.result_classification(payload) != "ok" or not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and row.get("type") == "entity"
               and row.get("file_path") == TRACKER and row.get("entity") == "main/active-tracker"
               and isinstance(row.get("content"), str)
               and re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", row["content"])
               for row in rows)


def verify_graph(payload: dict, target: str, absent_target: str | None = None) -> bool:
    """Join explicit public relation endpoints to exact returned note identities."""
    rows = payload.get("results")
    if (common.result_classification(payload) != "ok" or payload.get("has_more") is not False
            or not isinstance(rows, list)):
        return False
    roots = [row for row in rows if isinstance(row, dict)
             and isinstance(row.get("primary_result"), dict)
             and row["primary_result"].get("type") == "entity"
             and row["primary_result"].get("file_path") == target
             and row["primary_result"].get("permalink") == f"main/{Path(target).stem}"]
    if len(roots) != 1:
        return False
    primary = roots[0]["primary_result"]
    target_id = primary.get("entity_id")
    related = roots[0].get("related_results")
    if type(target_id) is not int or not isinstance(related, list):
        return False
    source_ids = [row.get("entity_id") for row in [primary, *related] if isinstance(row, dict)
                  and row.get("type") == "entity" and row.get("file_path") == TRACKER
                  and row.get("permalink") == "main/active-tracker"]
    if len(source_ids) != 1 or type(source_ids[0]) is not int:
        return False
    source_id = source_ids[0]
    relations = [row for row in related if isinstance(row, dict) and row.get("type") == "relation"
                 and row.get("relation_type") == "supports" and row.get("file_path") == TRACKER
                 and type(row.get("from_entity_id")) is int and row.get("from_entity_id") == source_id]
    absent_ids = {row.get("entity_id") for row in related if isinstance(row, dict)
                  and row.get("type") == "entity" and row.get("file_path") == absent_target
                  and type(row.get("entity_id")) is int}
    if absent_target and any(row.get("to_name") == _TARGET_NAMES[absent_target]
                             or row.get("to_entity_id") in absent_ids for row in relations):
        return False
    return sum(type(row.get("to_entity_id")) is int and row.get("to_entity_id") == target_id
               and row.get("to_name") == _TARGET_NAMES[target] for row in relations) == 1


def _connect(state: Path) -> sqlite3.Connection | None:
    store = state / "config/memory.db"
    if not store.is_file():
        return None
    store = common._contained_path(state, store)
    conn = sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)
    conn.execute("BEGIN")
    return conn


def _schema(conn: sqlite3.Connection) -> bool:
    if not common._table_exists(conn, "alembic_version"):
        if any(common._table_exists(conn, name) for name in ("project", "entity", "search_index")):
            raise common.AdapterFault("incompatible indexed-store schema: missing alembic_version")
        return False
    if conn.execute("SELECT version_num FROM alembic_version").fetchall() != [(ALEMBIC,)]:
        raise common.AdapterFault("incompatible indexed-store schema: unsupported Basic Memory head")
    common._require_columns(conn, "project", {"id", "name", "path"})
    common._require_columns(conn, "entity", {"id", "file_path", "project_id"})
    return True


def _project(conn: sqlite3.Connection, state: Path) -> int | None:
    rows = conn.execute("SELECT id, path FROM project WHERE name='main'").fetchall()
    if len(rows) != 1 or Path(str(rows[0][1])).resolve() != (state / "home").resolve():
        return None
    common._contained_path(state, Path(str(rows[0][1])))
    return int(rows[0][0])


def inspect_index(state: Path, fixture: dict) -> dict[str, Any]:
    """Prove exact initial note/search membership in one current-schema snapshot."""
    expected = [str(row["path"]) for row in fixture["pages"]]
    proof: dict[str, Any] = {"ready": False, "alembic": ALEMBIC,
                             "expected_path_count": len(expected),
                             "expected_path_digest": common._membership_digest(expected),
                             "reason": "indexed store is not present"}
    conn = _connect(state)
    if conn is None:
        return proof
    with closing(conn):
        if not _schema(conn):
            proof["reason"] = "indexed store is not initialized yet"
            return proof
        common._require_columns(conn, "search_index", {"id", "file_path", "project_id", "entity_id", "type"})
        common._require_fts5_virtual_table(conn, "search_index")
        proof["schema_identity"] = common._schema_identity(conn, ("alembic_version", "project", "entity", "search_index"))
        project = _project(conn, state)
        if project is None:
            proof["reason"] = "configured main project does not bind to the disposable fixture root"
            return proof
        entities = [(int(identity), str(path), int(project_id)) for identity, path, project_id in
                    conn.execute("SELECT id,file_path,project_id FROM entity WHERE project_id=?", (project,))]
        search = [(int(identity), str(path), int(project_id), int(entity_id))
                  for identity, path, project_id, entity_id in conn.execute(
                      "SELECT id,file_path,project_id,entity_id FROM search_index WHERE type='entity' AND project_id=?", (project,))]
        observed = [row[1] for row in entities]
        proof.update({"observed_path_count": len(observed), "observed_path_digest": common._membership_digest(observed),
                      "proof_method": "entity/search_index identity multiset join",
                      "product_binding": {"project": "main", "project_id": project}})
        proof["ready"] = (bool(expected) and Counter(observed) == Counter(expected)
                          and Counter(entities) == Counter(row[:3] for row in search)
                          and all(row[0] == row[3] for row in search))
        proof["reason"] = None if proof["ready"] else "entity/search identity or fixture membership mismatch"
    return proof


def inspect_relation(state: Path, source: str, target: str, absent_target: str | None = None) -> dict[str, Any]:
    """Prove a current resolved typed edge and removal of the prior source edge."""
    proof: dict[str, Any] = {"ready": False, "alembic": ALEMBIC, "source": source, "target": target,
                             "absent_target": absent_target, "reason": "indexed store is not present"}
    conn = _connect(state)
    if conn is None:
        return proof
    with closing(conn):
        if not _schema(conn):
            proof["reason"] = "indexed store is not initialized yet"
            return proof
        common._require_columns(conn, "relation", {"id", "from_id", "to_id", "to_name", "relation_type", "project_id", "generation"})
        common._require_columns(conn, "note_content", {"entity_id", "project_id", "db_version"})
        common._require_columns(conn, "relation_search_refresh", {"entity_id", "project_id"})
        project = _project(conn, state)
        if project is None:
            proof["reason"] = "configured main project does not bind to the disposable fixture root"
            return proof
        sources = conn.execute("SELECT e.id,n.db_version FROM entity e JOIN note_content n ON n.entity_id=e.id "
                               "AND n.project_id=e.project_id WHERE e.file_path=? AND e.project_id=?", (source, project)).fetchall()
        targets = conn.execute("SELECT id FROM entity WHERE file_path=? AND project_id=?", (target, project)).fetchall()
        if len(sources) != 1 or len(targets) != 1:
            proof["reason"] = "exact source or target is missing or ambiguous"
            return proof
        source_id, generation = sources[0]
        observed = conn.execute("SELECT r.to_id,r.to_name,r.generation,t.file_path,t.project_id,r.id,r.relation_type,r.from_id "
                                "FROM relation r LEFT JOIN entity t ON t.id=r.to_id WHERE r.from_id=? AND r.project_id=? "
                                "ORDER BY r.id", (source_id, project)).fetchall()
        relations = [row for row in observed if row[6] == "supports"]
        refresh_count = conn.execute("SELECT COUNT(*) FROM relation_search_refresh WHERE entity_id=? AND project_id=?",
                                     (source_id, project)).fetchone()[0]
        present = sum(row[0] == targets[0][0] and row[3] == target and row[4] == project
                      and row[2] == generation for row in relations) == 1
        absent_present = bool(absent_target and any(row[3] == absent_target
                              or row[1] == _TARGET_NAMES[absent_target] for row in relations))
        current = generation > 0 and all(row[2] == generation for row in relations)
        proof.update({"present": present, "absent_target_present": absent_present, "generation": generation,
                      "source_entity_id": source_id, "source_db_version": generation,
                      "relation_search_refresh_count": refresh_count,
                      "observed_relations": [{"relation_id": row[5], "from_id": row[7], "to_id": row[0],
                                              "to_name": row[1], "target_path": row[3], "relation_type": row[6],
                                              "generation": row[2]} for row in observed],
                      "relations_current": current, "product_binding": {"project": "main", "project_id": project},
                      "schema_identity": common._schema_identity(conn, ("alembic_version", "project", "entity", "relation", "note_content", "relation_search_refresh")),
                      "ready": present and not absent_present and current and refresh_count == 0})
        proof["reason"] = (None if proof["ready"] else "source relation search refresh work remains"
                           if refresh_count else "expected current typed edge or old-edge absence is unproven")
    return proof
