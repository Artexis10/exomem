"""The explicit synchronous analytics execution profile on the shared query executor.

OpenSpec add-collection-query-engine Q7.6a (design §9, §11, §12). Reductions
run through the real read session on invented stores and compare with the
independent ``s1_export_fixture.expected_daily`` reference. Each case names
the defect only it catches.
"""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest
from s1_export_fixture import START_DAY, expected_daily, iter_exercises
from test_collection_rollups import by_bucket, exercise, load, manifest, reduce, request
from test_collection_store_summary import canonical
from test_collection_store_writer import CID, manifest_path
from test_collection_store_writer import store as store
from test_governance_egress import write_rule, write_scope

from exomem import structured_collections as collections
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime

MIB = 1024 * 1024
INTERACTIVE = {"timeout_ms": 200, "max_row_visits": 100_000, "max_temp_bytes": 64 * MIB,
               "max_groups": 1000, "max_state_bytes": 8 * MIB}
ANALYTICS = {**INTERACTIVE, "timeout_ms": 2000, "max_row_visits": 10_000_000, "max_temp_bytes": 256 * MIB}
#: Fixed volume for exact reduction and interactive visit-bound checks.
LARGE = 300_000
# These labels describe fixed test preparation boundaries, never dataset vocabulary.
_ANALYTICS_PREPARATION_PHASES = ("create", "generate", "convert", "seed", "reference", "interactive_refusal")


def analytics(raw):
    return {**raw, "execution_profile": "analytics"}


def folded(records, days=180):
    """The invented sessions moved back by whole multiples of ``days``, keeping each one's clock time,
    offset and flagged basis, so any number of them reduces into one page of daily groups."""
    for record in records:
        for name in ("start", "start_utc"):
            if name in record:
                moment = dt.datetime.fromisoformat(record[name].replace("Z", "+00:00"))
                moment -= dt.timedelta(days=(moment.date() - START_DAY).days // days * days)
                record[name] = moment.strftime("%Y-%m-%dT%H:%M:%SZ") if name == "start_utc" else moment.isoformat()
        yield record


def seed(store, items, *, cid=CID):
    """Stand in for earlier acknowledged imports: canonical typed rows, written directly.

    Driving hundreds of thousands of rows through the writer exceeds the
    per-test budget; the reduction under test reads only canonical rows. An
    items collection's rows get the view paths their files would have.
    """
    from exomem.collection_store import (
        governance,
        native_identity,
        tables,
        tokens,
        typed_storage,
    )

    conn = store.connection
    text, metadata, path, mode = conn.execute(
        "SELECT m.manifest_text,m.governance_json,c.manifest_path,c.view_mode FROM collection_manifests m "
        "JOIN collections c "
        "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
        (cid,)).fetchone()
    stored = collections.parse_manifest_bytes(store.root, path, text.encode())
    layout = typed_storage.require_layout(conn, cid)
    txn = conn.execute("SELECT MAX(txn_id) FROM txns").fetchone()[0]
    keyed = []
    for number, values in enumerate(items):
        key = f"00000000-0000-4000-8000-{number:012x}"
        keyed.append((key, values, tokens.payload_hash(1, key, values, "")))
    folder = None if mode == "summary" else path.rsplit("/", 1)[0] + "/Items/"
    with store.handle.transaction() as tx:
        tx.executemany(
            "INSERT INTO items(collection_id,item_key,natural_key,row_version,schema_version,values_json,"
            "payload_hash,view_path,created_txn,updated_txn,governance_json,encoding) "
            "VALUES (?,?,?,1,1,NULL,?,?,?,?,?,?)",
            ((cid, key, collections.manifest_natural_key(stored, values), payload_hash,
              folder and f"{folder}{key}.md", txn, txn, governance.row_metadata(stored.schema, values, metadata), layout.encoding)
             for key, values, payload_hash in keyed))
        ids = dict(tx.execute("SELECT item_key,row_id FROM items WHERE collection_id=?", (cid,)))
        _, _, upsert, insert_version = typed_storage._declarations(layout)
        # One metadata scope serves every row of this transaction; the layout's columns are fixed for it too.
        scope, columns = store.handle.write_scope, (*layout.value_columns, "r")
        # Core batches seed canonical fixtures without per-row dispatch or unbounded parameters.
        batch_size = 128
        for start in range(0, len(keyed), batch_size):
            current_rows, versions, identities = [], [], []
            for key, values, payload_hash in keyed[start:start + batch_size]:
                encoded = dict(zip(columns, typed_storage.encode_row(layout, values, connection=tx, scope=scope), strict=True))
                current = {"row_id": ids[key], "row_version": 1, **encoded}
                current_rows.append({**current, **({"item_key": native_identity.encode(key, layout.identity)}
                                    if layout.encoding == typed_storage.TYPED_V2 else {})})
                versions.append({**current, "body": ""})
                identities.append({"row_id": ids[key], "row_version": 1, "encoding": layout.encoding,
                                   "payload_hash": payload_hash, "txn_id": txn,
                                   "schema_version": 1, "payload_generation": layout.generation})
            store.handle.execute(upsert, current_rows)
            store.handle.execute(insert_version, versions)
            store.handle.execute(tables.INSERT_IDENTITY, identities)


def create(store, rollups=None):
    store.create_collection(manifest_path(), manifest(rollups=rollups), why="create", scaffold=False)


def test_a_write_scope_refuses_reuse_after_its_transaction_and_with_another_file(store, tmp_path):
    """A scope kept past ROLLBACK would store a frame that names a rolled-back format ID, and one used with
    another file's rows would store their instants against this file's epoch."""
    import sqlite3

    from exomem.collection_store import typed_storage

    create(store)
    layout = typed_storage.require_layout(store.connection, CID)
    assert layout.encoding == typed_storage.TYPED_V2
    values = {"id": "a", "start": "2024-01-02T00:30:00+02:00"}
    with pytest.raises(LookupError):
        with store.handle.transaction() as tx:
            kept = store.handle.write_scope
            typed_storage.encode_row(layout, values, connection=tx, scope=kept)
            raise LookupError("roll the transaction back")
    other = sqlite3.connect(tmp_path / "other.sqlite", isolation_level=None)
    try:
        other.execute("BEGIN")
        other.execute("CREATE TABLE marker(x)")
        with store.handle.transaction() as tx:
            with pytest.raises(RuntimeError, match="outlived"):
                typed_storage.encode_row(layout, values, connection=tx, scope=kept)
            with pytest.raises(RuntimeError, match="another connection"):
                typed_storage.encode_row(layout, values, connection=other, scope=store.handle.write_scope)
    finally:
        other.close()


@pytest.fixture(autouse=True)
def owner():
    with request_scope(owner_principal()):
        yield


@pytest.mark.query_deadline
def test_profiles_keep_their_shipped_bounds_and_refuse_wider_ones():
    """A profile whose shipped bounds drift, or a profile constructed with bounds wider than it
    admits. Opens no reader, so the shipped 200 ms deadline cannot trip here."""
    assert runtime.QueryLimits().bounds() == INTERACTIVE
    assert runtime.QueryLimits(profile="analytics").bounds() == ANALYTICS
    for wider in ({"timeout_ms": 2000}, {"max_row_visits": 100_001}, {"profile": "analytics", "timeout_ms": 2001},
                  {"profile": "analytics", "max_temp_bytes": 257 * MIB}):
        with pytest.raises(ValueError):
            runtime.QueryLimits(**wider)


def test_execution_profile_defaults_to_interactive_and_sessions_never_mix_bounds(store):
    """A request without a profile run under analytics bounds, or an interactive request
    silently widened inside an analytics session."""
    create(store)
    load(store, [exercise(record) for record in iter_exercises(24)])
    default = reduce(store, request())
    assert default["execution_profile"] == "interactive" and default["bounds"] == runtime.PROFILES["interactive"]
    wide = reduce(store, analytics(request()), limits=runtime.QueryLimits(profile="analytics"))
    assert wide["execution_profile"] == "analytics" and wide["bounds"] == ANALYTICS
    assert wide["groups"] == default["groups"]
    with pytest.raises(runtime.QueryError, match="QUERY_PROFILE_UNAVAILABLE"):
        reduce(store, request(), limits=runtime.QueryLimits(profile="analytics"))


def test_analytics_explain_preview_and_dry_run_report_profile_and_bounds_without_rows(store):
    """Explain, preview or dry-run that iterate results, omit the selected profile and bounds, or
    admit work the interactive profile would refuse."""
    create(store)
    load(store, [exercise(record) for record in iter_exercises(500)])
    tight = runtime.QueryLimits(max_row_visits=100)
    wide = runtime.QueryLimits(profile="analytics")
    explained = reduce(store, analytics({**request(), "mode": "explain"}), limits=wide)
    assert explained["execution_profile"] == "analytics" and explained["bounds"] == ANALYTICS
    assert explained["plan"]["strategy"] == "base" and explained["plan"]["reason"] == "no_matching_rollup"
    assert "groups" not in explained and "admitted_rows" not in explained
    previewed = reduce(store, analytics({**request(), "mode": "preview"}), limits=wide)
    assert previewed["admitted_rows"] == 500 and "groups" not in previewed
    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        reduce(store, {**request(), "mode": "dry_run"}, limits=tight)
    admitted = reduce(store, analytics({**request(), "mode": "dry_run"}), limits=wide)
    assert admitted["admitted"] is True and "groups" not in admitted and admitted["bounds"] == ANALYTICS


def test_analytics_exact_reduction_over_a_large_collection_matches_the_reference(store, monkeypatch):
    """A large exact reduction no rollup answers that is refused, sampled, partial or wrong under
    the analytics profile, or admitted past the interactive visit bound."""
    _install_analytics_timeout_observer(monkeypatch)
    preparation_marks = [time.monotonic()]
    create(store)
    preparation_marks.append(time.monotonic())
    records = list(folded(iter_exercises(LARGE)))
    preparation_marks.append(time.monotonic())
    items = [exercise(record) for record in records]
    preparation_marks.append(time.monotonic())
    seed(store, items)
    del items
    preparation_marks.append(time.monotonic())
    daily, flagged = expected_daily(records, "metrics.calories")
    preparation_marks.append(time.monotonic())
    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        reduce(store, request())
    preparation_marks.append(time.monotonic())
    started = time.monotonic()
    process_started, thread_started = time.process_time(), time.thread_time()
    try:
        result = reduce(store, analytics(request()), limits=runtime.QueryLimits(profile="analytics"))
    except runtime.QueryError as error:
        try:
            _report_analytics_failure(error, started, process_started, thread_started, preparation_marks)
        except Exception:  # noqa: BLE001 - diagnostics cannot replace the query failure.
            pass
        raise
    elapsed = time.monotonic() - started
    assert result["plan"]["strategy"] == "base" and len(result["groups"]) == len(daily) <= 200
    assert by_bucket(result) == daily and result["flagged_rows"] == len(flagged)
    assert elapsed < 2.0, elapsed


def _install_analytics_timeout_observer(monkeypatch):
    import sys
    import threading

    import pytest_timeout

    from exomem.query_engine import reductions

    original = pytest_timeout.dump_stacks
    main_ident = threading.main_thread().ident
    test_code = test_analytics_exact_reduction_over_a_large_collection_matches_the_reference.__code__
    reduce_code = reductions._from_rows.__code__

    def observed(terminal):
        # The locked plugin exits without unwinding; reuse its failure dump without another timer.
        try:
            _report_analytics_outer_timeout(terminal, sys._current_frames().get(main_ident),
                                            test_code, reduce_code)
        except Exception:  # noqa: BLE001 - diagnostics cannot replace the original timeout dump.
            pass
        original(terminal)

    monkeypatch.setattr(pytest_timeout, "dump_stacks", observed)


def _report_analytics_outer_timeout(terminal, frame, test_code, reduce_code):
    """Report elapsed clocks and a row count, never row content or query text."""
    now = time.monotonic()
    numbers = {"window": "outer-timeout-snapshot", "elapsed_since_reduce_start_s": None,
               "deadline_delta_ms": None, "visited": None, "preparation": None}
    while frame is not None:
        if frame.f_code is test_code:
            numbers["preparation"] = _analytics_preparation_windows(frame.f_locals.get("preparation_marks"))
            started = frame.f_locals.get("started")
            if type(started) in (int, float):
                numbers["elapsed_since_reduce_start_s"] = now - started
        elif frame.f_code is reduce_code:
            visited = frame.f_locals.get("visited")
            numbers["visited"] = visited if type(visited) is int else None
            deadline = getattr(frame.f_locals.get("session"), "_deadline", None)
            if type(deadline) in (int, float):
                # Positive means overdue; negative means time remains on the native deadline.
                numbers["deadline_delta_ms"] = (now - deadline) * 1000
        frame = frame.f_back
    terminal.write("analytics_outer_failure " + json.dumps(numbers, sort_keys=True) + "\n")


def _analytics_preparation_windows(marks):
    if type(marks) is not list or not marks:
        return None
    boundaries, now = tuple(marks), time.monotonic()
    windows = {}
    for index, name in enumerate(_ANALYTICS_PREPARATION_PHASES):
        if index >= len(boundaries):
            break
        complete = index + 1 < len(boundaries)
        end = boundaries[index + 1] if complete else now
        windows[name] = {"wall_s": end - boundaries[index], "state": "complete" if complete else "incomplete"}
    return windows


def _report_analytics_failure(error, started, process_started, thread_started, preparation_marks=None):
    """Report only numeric counters after failure, without tracing the measured loop."""
    numbers = {"window": "reduce-call-through-error-unwind", "wall_s": time.monotonic() - started,
               "process_cpu_s": time.process_time() - process_started,
               "thread_cpu_s": time.thread_time() - thread_started, "partial_reduce": None,
               "preparation": _analytics_preparation_windows(preparation_marks)}
    from exomem.query_engine import reductions

    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend((current.__cause__, current.__context__))
        trace = current.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            if frame.f_code is reductions._from_rows.__code__:
                # Fixed reducer counters exclude source rows, values and query text.
                counters = {name: value if type(value := frame.f_locals.get(name)) is int else None
                            for name in ("visited", "flagged", "retained")}
                groups = frame.f_locals.get("groups")
                counters["groups"] = len(groups) if type(groups) is dict else None
                numbers["partial_reduce"] = counters
            trace = trace.tb_next
    print("analytics_failure " + json.dumps(numbers, sort_keys=True), flush=True)


def test_whole_call_deadline_returns_no_partial_reduction_or_continuation(store):
    """A deadline overrun that returns a partial aggregate, or leaves half-computed state that a
    later call continues from."""
    create(store)
    records = list(folded(iter_exercises(5000)))
    seed(store, [exercise(record) for record in records])
    before = canonical(store)
    with pytest.raises(runtime.QueryError, match="QUERY_TIMEOUT"):
        reduce(store, analytics(request()), limits=runtime.QueryLimits(profile="analytics", timeout_ms=5))
    assert canonical(store) == before
    daily, _ = expected_daily(records, "metrics.calories")
    complete = reduce(store, analytics(request()), limits=runtime.QueryLimits(profile="analytics"))
    assert by_bucket(complete) == daily


@pytest.mark.parametrize("cap", ["groups", "temp", "state"])
def test_group_temp_and_retained_state_caps_refuse_without_partial_results(store, cap):
    """A reduction past 1,000 groups, the disk-temp quota or the retained-value budget that
    returns a truncated or partial answer instead of a typed refusal."""
    if cap == "state":
        fields = {"id": "{type: string, required: true}", "start": "{type: datetime}", "note": "{type: string}"}
        store.create_collection(manifest_path(), manifest(fields=fields), why="create", scaffold=False)
        first = dt.date(2026, 1, 1)
        seed(store, [{"id": f"n{n}", "start": f"{first + dt.timedelta(days=n)}T08:00:00+00:00",
                      "note": f"{n:04d}" + "x" * 200_000} for n in range(50)])
        raw = request(field="note", ops=("max",))
        code, limits = "QUERY_COST_LIMIT", runtime.QueryLimits(profile="analytics")
    else:
        first, days = dt.date(2023, 1, 1), 1001 if cap == "groups" else 10
        rows = [{"id": f"g{n}", "start": f"{first + dt.timedelta(days=n % days)}T08:00:00+00:00", "calories": n}
                for n in range(3000)]
        raw = request()
        if cap == "groups":
            create(store)
            seed(store, rows)
            code, limits = "QUERY_GROUP_LIMIT", runtime.QueryLimits(profile="analytics")
        else:
            # A row-varying rule makes release mixed, so admission streams released row ids to disk temp.
            store.create_collection(manifest_path(), manifest(summary=False), why="create", scaffold=False)
            assert store.migrate_typed_encoding(CID) == "ready"
            seed(store, rows)
            write_scope(store.root, paths="Records/**")
            write_rule(store.root, ceiling=0)
            code, limits = "QUERY_COST_LIMIT", runtime.QueryLimits(profile="analytics", max_temp_bytes=16 * 1024)
    with pytest.raises(runtime.QueryError, match=code):
        reduce(store, analytics(raw), limits=limits)


def test_tighter_compiler_bounds_refuse_analytics_widening(store):
    """A compiler-bounded caller whose request widens it to analytics."""
    create(store)
    seed(store, [exercise(record) for record in iter_exercises(2000)])
    compiler = runtime.QueryLimits(max_row_visits=1000)
    with pytest.raises(runtime.QueryError, match="QUERY_PROFILE_UNAVAILABLE"):
        reduce(store, analytics(request()), limits=compiler)
    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        reduce(store, request(), limits=compiler)
