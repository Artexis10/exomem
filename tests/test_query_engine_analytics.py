"""The explicit synchronous analytics execution profile on the shared query executor.

OpenSpec add-collection-query-engine Q7.6a (design §9, §11, §12). Reductions
run through the real read session on invented stores and compare with the
independent ``s1_export_fixture.expected_daily`` reference. Each case names
the defect only it catches.
"""

from __future__ import annotations

import datetime as dt
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
#: The largest invented collection that seeds and reduces inside the per-test budget.
LARGE = 300_000


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
    from exomem.collection_store import governance, tables, tokens, typed_storage

    conn = store.connection
    text, metadata, path, mode = conn.execute(
        "SELECT m.manifest_text,m.governance_json,c.manifest_path,c.view_mode FROM collection_manifests m "
        "JOIN collections c "
        "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
        (cid,)).fetchone()
    stored = collections.parse_manifest_bytes(store.root, path, text.encode())
    layout = typed_storage.require_layout(conn, cid)
    txn = conn.execute("SELECT MAX(txn_id) FROM txns").fetchone()[0]
    keyed = [(f"seed-{number}", values) for number, values in enumerate(items)]
    folder = None if mode == "summary" else path.rsplit("/", 1)[0] + "/Items/"
    with store.handle.transaction() as tx:
        tx.executemany(
            "INSERT INTO items(collection_id,item_key,natural_key,row_version,schema_version,values_json,"
            "payload_hash,view_path,created_txn,updated_txn,governance_json,encoding) "
            "VALUES (?,?,?,1,1,NULL,?,?,?,?,?,'typed-v1')",
            ((cid, key, collections.manifest_natural_key(stored, values), tokens.payload_hash(1, key, values, ""),
              folder and f"{folder}{key}.md", txn, txn, governance.row_metadata(stored.schema, values, metadata))
             for key, values in keyed))
        ids = dict(tx.execute("SELECT item_key,row_id FROM items WHERE collection_id=?", (cid,)))
        _, _, upsert, insert_version = typed_storage._declarations(layout)
        # Core batches seed canonical fixtures without per-row dispatch or unbounded parameters.
        batch_size = 128
        for start in range(0, len(keyed), batch_size):
            current_rows, versions, identities = [], [], []
            for key, values in keyed[start:start + batch_size]:
                encoded = dict(zip((*layout.value_columns, "r"), typed_storage.encode_row(layout, values), strict=True))
                current = {"row_id": ids[key], "row_version": 1, **encoded}
                current_rows.append(current)
                versions.append({**current, "body": ""})
                identities.append({"row_id": ids[key], "row_version": 1, "encoding": typed_storage.TYPED_V1,
                                   "payload_hash": tokens.payload_hash(1, key, values, ""), "txn_id": txn,
                                   "schema_version": 1})
            store.handle.execute(upsert, current_rows)
            store.handle.execute(insert_version, versions)
            store.handle.execute(tables.INSERT_IDENTITY, identities)


def create(store, rollups=None):
    store.create_collection(manifest_path(), manifest(rollups=rollups), why="create", scaffold=False)


@pytest.fixture(autouse=True)
def owner():
    with request_scope(owner_principal()):
        yield


def test_execution_profile_defaults_to_interactive_and_sessions_never_mix_bounds(store):
    """A request without a profile run under analytics bounds, or an interactive request
    silently widened inside an analytics session."""
    create(store)
    load(store, [exercise(record) for record in iter_exercises(24)])
    default = reduce(store, request())
    assert default["execution_profile"] == "interactive" and default["bounds"] == INTERACTIVE
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


def test_analytics_exact_reduction_over_a_large_collection_matches_the_reference(store):
    """A large exact reduction no rollup answers that is refused, sampled, partial or wrong under
    the analytics profile, or admitted past the interactive visit bound."""
    create(store)
    records = list(folded(iter_exercises(LARGE)))
    seed(store, [exercise(record) for record in records])
    daily, flagged = expected_daily(records, "metrics.calories")
    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        reduce(store, request())
    started = time.monotonic()
    result = reduce(store, analytics(request()), limits=runtime.QueryLimits(profile="analytics"))
    elapsed = time.monotonic() - started
    assert result["plan"]["strategy"] == "base" and len(result["groups"]) == len(daily) <= 200
    assert by_bucket(result) == daily and result["flagged_rows"] == len(flagged)
    assert elapsed < 2.0, elapsed


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
    """A compiler-bounded caller whose request widens it to analytics, or a profile constructed
    with bounds wider than the profile admits."""
    create(store)
    seed(store, [exercise(record) for record in iter_exercises(2000)])
    compiler = runtime.QueryLimits(timeout_ms=50, max_row_visits=1000)
    with pytest.raises(runtime.QueryError, match="QUERY_PROFILE_UNAVAILABLE"):
        reduce(store, analytics(request()), limits=compiler)
    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        reduce(store, request(), limits=compiler)
    for wider in ({"timeout_ms": 2000}, {"max_row_visits": 100_001}, {"profile": "analytics", "timeout_ms": 2001},
                  {"profile": "analytics", "max_temp_bytes": 257 * MIB}):
        with pytest.raises(ValueError):
            runtime.QueryLimits(**wider)
