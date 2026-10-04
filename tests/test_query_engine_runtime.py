"""Read-only canonical queries with bounded, request-local admission."""

import json
import pickle
import sqlite3
from contextlib import closing

import pytest
from test_collection_store_governance import inspection_token, redeem
from test_collection_store_governance import session as grant_session
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem.collection_store import connection, governance
from exomem.collection_store.preview import preview_store
from exomem.governance import authorization_session_authority
from exomem.governance import store as authority_store
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime


def test_query_reader_allows_private_temp_membership(store):
    """Mixed admission needs writable TEMP without a writable main database."""
    create(store)
    with closing(connection.open_query_reader(store.handle.path)) as reader:
        reader.execute("CREATE TEMP TABLE admitted(row_id INTEGER PRIMARY KEY)")
        reader.execute("INSERT INTO admitted VALUES(1)")
        assert reader.execute("SELECT row_id FROM admitted").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM main.collections")
        assert reader.execute("PRAGMA cache_size").fetchone() == (-8192,)
        assert reader.execute("PRAGMA mmap_size").fetchone() == (0,)
        assert reader.execute("PRAGMA temp_store").fetchone() == (1,)


def test_subjects_do_not_read_rows_before_manifest_admission(store):
    """A denied manifest must not first materialize all its row identities."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    statements = []
    with closing(connection.open_reader(store.handle.path)) as reader:
        reader.set_trace_callback(statements.append)
        subjects = governance.iter_subjects(reader, CID, "unbound")
        assert next(subjects).row_id is None
        assert not any("FROM items" in sql for sql in statements)
        subjects.close()


def test_admission_identity_scan_uses_index_without_external_sort(store):
    """Admission must not spill a row-ID sort outside the TEMP-table quota."""
    create(store)
    store.append_record(CID, item={"title": "Second key"}, item_key=OTHER, why="capture")
    store.append_record(CID, item={"title": "First key"}, item_key=KEY, why="capture")
    with closing(connection.open_reader(store.handle.path)) as reader:
        subjects = governance.subjects(reader, CID, "unbound")
    assert [subject.basis.identity.rsplit("/", 1)[-1] for subject in subjects[1:]] == [OTHER, KEY]

    write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    statements = []
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        session.connection.set_trace_callback(statements.append)
        assert session.admit(CID).visible_count == 1
    scans = [sql for sql in statements if sql.startswith(
        "SELECT row_id,item_key,row_version,payload_hash,view_path,governance_json "
    )]
    assert scans
    for sql in scans:
        plan = [row[3] for row in store.connection.execute("EXPLAIN QUERY PLAN " + sql)]
        assert not any("TEMP B-TREE" in step for step in plan), plan
        assert any("USING INDEX sqlite_autoindex_items_1" in step for step in plan), plan


def values(admitted):
    admitted.check()
    sql = (f"SELECT {admitted.values_sql} FROM items AS i WHERE i.collection_id=? "
           f"AND {admitted.membership_sql} ORDER BY {admitted.input_order}")
    return [json.loads(row[0]) for batch in admitted.session.fetch(
        admitted.session.connection.execute(sql, (admitted.collection_id,))
    ) for row in batch]


def test_withheld_row_is_absent_before_decode_and_projection(store):
    """A hidden sibling never reaches a Python value function, even without WHERE."""
    create(store)
    store.append_record(CID, item={"title": "Visible", "count": 2}, item_key=KEY, why="capture")
    write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        absent = session.admit(CID)
        absent_count, absent_values = absent.visible_count, values(absent)
    with request_scope(owner_principal()):
        store.append_record(CID, item={"title": "Secret", "count": 100}, item_key=OTHER, why="capture")
    seen = []

    def project(value):
        assert value["title"] == "Visible"
        seen.append(value)
        return value

    # Invalid JSON is an actual decode trap, not a mock of authorization.
    store.connection.execute("UPDATE items SET values_json='not json' WHERE item_key=?", (OTHER,))
    with request_scope(_external()):
        with runtime.read_session(store.root, store.handle.path, project_values=project) as session:
            admitted = session.admit(CID)
            assert admitted.visible_count == absent_count == 1
            assert values(admitted) == absent_values == [{"title": "Visible", "count": 2}]
            cursor = session.connection.execute(f"SELECT {admitted.values_sql} FROM items AS i")
            assert sum(row[0] is None for batch in session.fetch(cursor) for row in batch) == 1
    assert seen == [{"title": "Visible", "count": 2}] * 2


def test_admission_cannot_cross_principal_or_session_and_policy_is_fresh(store):
    """An owner handle or an earlier open policy cannot authorize a later reader."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with request_scope(owner_principal()), runtime.read_session(store.root, store.handle.path) as session:
        admitted = session.admit(CID)
        assert values(admitted) == [{"title": "One"}]
        with pytest.raises(TypeError, match="request-local"):
            pickle.dumps(admitted)
        with request_scope(_external()), pytest.raises(runtime.QueryError, match="QUERY_CANCELLED"):
            admitted.check()
    with pytest.raises(runtime.QueryError):
        admitted.check()
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError, match="COLLECTION_NOT_FOUND"):
            session.admit(CID)


def test_default_owner_declaration_is_not_open_when_policy_is_empty(store):
    """No policy cannot bypass a canonical type's default-deny contract."""
    create(store)
    declaration = json.loads(store.connection.execute(
        "SELECT declaration_json FROM collection_type_versions WHERE name='records'"
    ).fetchone()[0])
    declaration["default_audience"] = "owner"
    declaration["name"] = "private-records"
    declaration.pop("wire")
    store.connection.execute("INSERT INTO collection_types VALUES('private-records',1,0)")
    store.connection.execute("INSERT INTO collection_type_versions VALUES('private-records',1,?,'private','additive',1)",
                             (json.dumps(declaration),))
    store.connection.execute("UPDATE collections SET type_name='private-records' WHERE collection_id=?", (CID,))
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError, match="COLLECTION_NOT_FOUND"):
            session.admit(CID)


@pytest.mark.parametrize("failure", ["cancel", "timeout", "sql", "python", "temp"])
def test_failure_closes_connection_and_releases_reader_slot(store, monkeypatch, failure):
    """Every exit frees its reader slot and destroys private tables and handlers."""
    create(store)
    limits = runtime.QueryLimits(max_temp_bytes=16 * 1024)
    cancelled = [False]
    expected = {"cancel": "QUERY_CANCELLED", "timeout": "QUERY_TIMEOUT",
                "sql": "QUERY_UNAVAILABLE", "temp": "QUERY_COST_LIMIT"}
    error_type = RuntimeError if failure == "python" else runtime.QueryError
    with pytest.raises(error_type, match=expected.get(failure, "caller failed")):
        with runtime.read_session(store.root, store.handle.path, limits=limits,
                                  cancelled=lambda: cancelled[0]) as session:
            conn = session.connection
            session.admit(CID)
            if failure == "cancel":
                cancelled[0] = True
                session.check()
            elif failure == "timeout":
                monkeypatch.setattr(runtime.time, "monotonic", lambda: session._deadline + 1)
                conn.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n")
            elif failure == "sql":
                conn.execute("SELECT * FROM missing_table")
            elif failure == "temp":
                conn.execute("CREATE TEMP TABLE payload(value BLOB)")
                conn.execute("INSERT INTO payload VALUES(zeroblob(32768))")
            else:
                raise RuntimeError("caller failed")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with runtime.read_session(store.root, store.handle.path) as first:
        with runtime.read_session(store.root, store.handle.path) as second:
            assert first.connection is not second.connection
            with pytest.raises(runtime.QueryError, match="QUERY_BUSY"), runtime.read_session(store.root, store.handle.path):
                pass


def test_cost_rejection_precedes_value_evaluation(store):
    """A cardinality refusal does not decode values or call a projector."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Two"}, item_key=OTHER, why="capture")

    def trap(_value):
        pytest.fail("value evaluated before cost admission")

    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        with runtime.read_session(store.root, store.handle.path,
                                  limits=runtime.QueryLimits(max_row_visits=1), project_values=trap) as session:
            session.admit(CID)


def test_fetch_closes_cursor_and_obeys_tighter_batch_limit(store):
    """Consumers can stream without accidentally retaining an entire SQL result."""
    create(store)
    with runtime.read_session(store.root, store.handle.path, limits=runtime.QueryLimits(fetch_size=2)) as session:
        cursor = session.connection.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
        assert list(session.fetch(cursor)) == [[(1,), (2,)], [(3,)]]
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            cursor.fetchone()


def test_absent_policy_still_respects_access_exclusions(store):
    """The uniform shortcut cannot treat an excluded sibling as authorized."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Hidden"}, item_key=OTHER, why="capture")
    (store.root / "Knowledge Base/_access.yaml").write_text(f"excluded: [Records/Work/Items/{OTHER}.md]\n")
    with runtime.read_session(store.root, store.handle.path) as session:
        admitted = session.admit(CID)
        assert admitted.visible_count == 1
        assert values(admitted) == [{"title": "One"}]


def test_real_session_grant_and_revocation_are_resolved_for_each_reader(store, monkeypatch):
    """A grant releases its canonical row; the next read observes revocation."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = grant_session(store, monkeypatch)
    with request_scope(who), runtime.read_session(store.root, store.handle.path) as session:
        assert values(session.admit(CID)) == []
    with preview_store(store.root, store.handle):
        token = inspection_token(store, who)
        assert redeem(store, who, token)["status"] == "committed"
    with request_scope(who), runtime.read_session(store.root, store.handle.path) as session:
        assert values(session.admit(CID)) == [{"title": "One"}]
    with closing(authority_store.open_authorization_session_connection(store.root)) as conn:
        authorization_session_authority.revoke_session_grants(
            conn, context=who.verified_authorization_session, audience=who.audience_id,
            now=int(runtime.time.time()),
        )
    with request_scope(who), runtime.read_session(store.root, store.handle.path) as session:
        assert values(session.admit(CID)) == []


def test_cancel_during_projection_does_not_return_a_partial_row(store):
    """Python callbacks receive the same cancellation checks as SQLite VM work."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    cancelled = [False]

    def project(value):
        cancelled[0] = True
        return value

    with pytest.raises(runtime.QueryError, match="QUERY_CANCELLED"):
        with runtime.read_session(store.root, store.handle.path,
                                  cancelled=lambda: cancelled[0], project_values=project) as session:
            values(session.admit(CID))


def test_each_admitted_collection_keeps_its_own_projection_fields(store):
    """Admitting another collection cannot change an existing handle's schema."""
    create(store)
    store.append_record(CID, item={"title": "One", "count": 2}, item_key=KEY, why="capture")
    second_id = "33333333-3333-4333-8333-333333333333"
    store.create_collection(
        manifest_path().replace("Work/", "Other/"),
        manifest_text().replace(CID, second_id).replace("count: {type: integer}", "note: {type: string}"),
        why="capture", scaffold=False,
    )
    store.append_record(second_id, item={"title": "Two", "note": "text"}, item_key=OTHER, why="capture")
    with runtime.read_session(store.root, store.handle.path) as session:
        first, second = session.admit(CID), session.admit(second_id)
        assert values(first) == [{"title": "One", "count": 2}]
        assert values(second) == [{"title": "Two", "note": "text"}]


def test_abandoned_stream_is_closed_at_session_exit(store):
    """A partially consumed result cannot keep a cursor or snapshot alive."""
    create(store)
    with runtime.read_session(store.root, store.handle.path, limits=runtime.QueryLimits(fetch_size=1)) as session:
        cursor = session.connection.execute("SELECT 1 UNION ALL SELECT 2")
        stream = session.fetch(cursor)
        assert next(stream) == [(1,)]
    with pytest.raises(runtime.QueryError, match="QUERY_CANCELLED"):
        next(stream)


def test_oversized_value_refuses_before_json_materialization(store):
    """One large stored row cannot consume the whole reader memory budget."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    store.connection.execute("UPDATE items SET values_json=? WHERE item_key=?",
                             ('{"title":"' + "x" * (256 * 1024) + '"}', KEY))
    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        with runtime.read_session(store.root, store.handle.path) as session:
            values(session.admit(CID))
