"""Exact incremental daily/ISO-week rollups under governance.

OpenSpec add-collection-query-engine Q7.5 (design §3, §13). Every case drives
the real CollectionWriter and the query runtime on an invented store, and
compares against the independent ``s1_export_fixture.expected_daily``
reference. Each case names the defect only it catches.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastmcp.tools.base import default_serializer
from s1_export_fixture import daily_summaries, expected_daily, iter_exercises
from test_collection_store_summary import COPY_CID, canonical, files, guards, summary_text
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import structured_collections as collections
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime
from exomem.query_engine.validation import normalize_query

AS_OF = "2026-10-06T00:00:00+00:00"
THIRD = "33333333-3333-4333-8333-333333333333"
FIELDS = {
    "id": "{type: string, required: true}", "kind": "{type: string}", "status": "{type: string}",
    "start": "{type: datetime, offset: utc_offset}", "utc_offset": "{type: string}", "calories": "{type: integer}",
    "distance_m": "{type: number}",
}
DAILY = {"bucket": "day", "timestamp": "start", "values": {"calories": ["count", "sum", "avg", "min", "max", "latest"]}}


def _yaml(value, indent=0):
    pad = " " * indent
    if isinstance(value, dict):
        return "".join(f"{pad}{key}:\n{_yaml(item, indent + 2)}" if isinstance(item, dict)
                       else f"{pad}{key}: {_yaml(item)}\n" for key, item in value.items())
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def manifest(*, rollups=None, fields=FIELDS, summary=True, cid=CID, title="Work", natural_key="id"):
    """The writer fixture's Records manifest with these fields and rollup declarations."""
    text = manifest_text().replace(
        "    title: {type: string, required: true}\n    count: {type: integer}\n",
        "".join(f"    {name}: {spec}\n" for name, spec in fields.items()),
    ).replace("natural_key: [title]", f"natural_key: [{natural_key}]")
    text = text.replace(CID, cid).replace("title: Work", f"title: {title}")
    if rollups:
        text = text.removesuffix("---\n") + "rollups:\n" + _yaml(rollups, 2) + "---\n"
    return summary_text(text) if summary else text


def exercise(record):
    """One invented export record as collection values; absent source fields stay absent."""
    values = {"id": record["id"], "kind": record["kind"], "status": "recorded",
              "calories": record["metrics"]["calories"], "distance_m": record["metrics"]["distance_m"]}
    if "start_utc" in record:
        values["start"] = record["start_utc"]
        if "utc_offset" in record:
            values["utc_offset"] = record["utc_offset"]
    elif "start" in record:
        values["start"] = record["start"]
    return values


def load(store, items, *, cid=CID):
    _evidence(store.root)
    after = guards(store, cid)["expected_container_hash"]
    for begin in range(0, len(items), 500):
        receipt = store.bulk_upsert_records(
            cid, rows=[{"item": item} for item in items[begin:begin + 500]], why="import",
            source=EVIDENCE, expected_container_hash=after)
        assert receipt["committed"], receipt
        after = receipt["after_container_hash"]
    return after


def request(*, field="calories", ops=("count", "sum", "avg"), bucket="day", timestamp="start", groups=()):
    return {"version": 1,
            "group_by": [{"field": timestamp, "bucket": bucket}, *({"field": name} for name in groups)],
            "aggregates": {op: {"op": op, "field": field} for op in ops}, "page": {"limit": 200}}


def reduce(store, raw, *, cid=CID, principal=None, limits=None):
    """Normalize ``raw`` against the stored declaration and reduce it in one read session."""
    with request_scope(principal or owner_principal()):
        with store.read_collection(cid) as stored:
            fields = {name: {"type": spec.type, "enum": spec.enum} for name, spec in stored.schema.fields.items()}
        fields["item_key"] = {"type": "string"}
        result = normalize_query(raw, collection=cid, declarations={cid: {
            "domain": "collections", "type": "records", "vault": "fixture", "fields": fields}})
        assert not result.findings, result.findings
        with runtime.read_session(store.root, store.handle.path, limits=limits) as session:
            return session.reduce(result.query, as_of=AS_OF)


def by_bucket(result, key="start"):
    return {group[key]: {name: value for name, value in group.items() if name != key}
            for group in result["groups"]}


def weekly(daily):
    """Fold the independent daily reference into ISO weeks keyed by their Monday."""
    weeks = {}
    for day, bucket in daily.items():
        local = dt.date.fromisoformat(day)
        monday = (local - dt.timedelta(days=local.weekday())).isoformat()
        week = weeks.setdefault(monday, {"count": 0, "sum": 0})
        week["count"] += bucket["count"]
        week["sum"] += bucket["sum"]
    return {monday: {**week, "avg": week["sum"] / week["count"]} for monday, week in sorted(weeks.items())}


@pytest.fixture(autouse=True)
def owner():
    with request_scope(owner_principal()):
        yield


@pytest.mark.parametrize("bucket", ["day", "week"])
def test_rollup_matches_the_source_local_reference_across_midnight_offsets(store, bucket):
    """A bucket keyed by the UTC or host day instead of each record's own offset, or a flagged
    time basis guessed into a day, including a UTC instant whose declared offset field is absent;
    the ISO week starts on the record's local Monday."""
    store.create_collection(manifest_path(), manifest(rollups={"activity": {**DAILY, "bucket": bucket}}),
                            why="create", scaffold=False)
    records = list(iter_exercises(96))
    unknown = next(n for n, record in enumerate(records) if "start_utc" in record)
    records[unknown] = {name: value for name, value in records[unknown].items() if name != "utc_offset"}
    load(store, [exercise(record) for record in records])
    daily, flagged = expected_daily(records, "metrics.calories")
    assert flagged, "the fixture must carry flagged time bases"
    result = reduce(store, request(bucket=bucket))
    assert result["plan"]["strategy"] == "rollup" and result["plan"]["rollup"] == "activity"
    assert by_bucket(result) == (daily if bucket == "day" else weekly(daily))
    assert result["flagged_rows"] == len(flagged)
    assert result["plan"]["basis"] == {"field": "start", "bucket": bucket, "offset": "utc_offset", "kind": "instant",
                                       "from": None, "to": None}


def test_date_only_summaries_bucket_on_their_recorded_local_date(store):
    """A date-only daily summary given an invented instant or offset that moves it to another day."""
    fields = {"date": "{type: date, required: true}", "steps": "{type: integer}", "resting_hr": "{type: integer}"}
    rollup = {"bucket": "day", "timestamp": "date", "values": {"steps": ["count", "sum", "avg"]}}
    store.create_collection(manifest_path(), manifest(fields=fields, natural_key="date",
                                                      rollups={"steps": rollup}), why="create", scaffold=False)
    rows = daily_summaries(10)
    load(store, rows)
    daily, flagged = expected_daily(rows, "steps")
    result = reduce(store, request(field="steps", timestamp="date"))
    assert result["plan"]["strategy"] == "rollup"
    assert by_bucket(result, "date") == daily and result["flagged_rows"] == len(flagged) == 0
    assert result["plan"]["basis"] == {"field": "date", "bucket": "day", "offset": None, "kind": "date",
                                       "from": None, "to": None}


NINE = {f"r{n}": DAILY for n in range(9)}
FIVE_DIMENSIONS = {"wide": {**DAILY, "group_by": ["kind", "status", "utc_offset", "id", "distance_m"]}}
PARTITIONED = {"split": {**DAILY, "partitions": ["audience"]}}
PERCENTILE = {"typical": {**DAILY, "values": {"calories": ["percentile"]}}}


MISSPELT_OFFSET = {**FIELDS, "start": "{type: datetime, offset: utc_ofset}"}


@pytest.mark.parametrize("rollups,fields,code", [
    (NINE, FIELDS, "ROLLUP_LIMIT"), (FIVE_DIMENSIONS, FIELDS, "ROLLUP_LIMIT"),
    (PARTITIONED, FIELDS, "ROLLUP_PARTITIONS_UNSUPPORTED"), (PERCENTILE, FIELDS, "INVALID_ROLLUP_DECLARATION"),
    ({"daily": DAILY}, MISSPELT_OFFSET, "INVALID_ITEM_SCHEMA"),
], ids=["nine-rollups", "five-dimensions", "partitions", "percentile", "misspelt-offset"])
def test_declarations_over_caps_refuse_before_commit(store, rollups, fields, code):
    """A declaration past 8 rollups (16 old/new bucket updates per row), 4 group dimensions, a
    maintained release partition, a non-rollup reduction, or an offset naming no declared field
    (silently bucketing by the instant's own offset) that commits, or half-commits."""
    store.create_collection(manifest_path(), manifest(rollups={"daily": DAILY}), why="create", scaffold=False)
    load(store, [exercise(record) for record in iter_exercises(6)])
    before, vault_before = canonical(store), files(store)
    with pytest.raises(collections.CollectionError, match=code):
        store.revise_collection(CID, manifest_text=manifest(rollups=rollups, fields=fields), why="widen",
                                **guards(store))
    assert canonical(store) == before and files(store) == vault_before


def test_update_moves_rows_between_buckets_and_recomputes_replaced_extremes(store):
    """An update or lifecycle change that leaves the old bucket's count, sum, minimum or latest
    member stale, keeps an emptied bucket, or misses the new bucket."""
    rollup = {**DAILY, "group_by": ["status"]}
    store.create_collection(manifest_path(), manifest(rollups={"daily": rollup}), why="create", scaffold=False)
    rows = [{"id": "a", "status": "recorded", "start": "2026-03-01T08:00:00+02:00", "calories": 100},
            {"id": "b", "status": "recorded", "start": "2026-03-01T23:30:00+02:00", "calories": 300},
            {"id": "c", "status": "recorded", "start": "2026-03-02T09:00:00+02:00", "calories": 500}]
    for key, row in zip((KEY, OTHER, THIRD), rows, strict=True):
        store.append_record(CID, item=row, item_key=key, why="observe")
    ops = ("count", "sum", "min", "max", "latest")

    def update(key, changes):
        from test_collection_store_summary import _item_version
        store.update_record(CID, item_key=key, changes=changes, why="correct",
                            expected_container_hash=guards(store)["expected_container_hash"],
                            expected_item_version=_item_version(store, key))

    update(KEY, {"start": "2026-03-02T01:00:00+02:00"})
    moved = reduce(store, request(ops=ops, groups=("status",)))
    assert moved["plan"]["strategy"] == "rollup"
    assert moved["groups"] == [
        {"start": "2026-03-01", "status": "recorded", "count": 1, "sum": 300, "min": 300, "max": 300, "latest": 300},
        {"start": "2026-03-02", "status": "recorded", "count": 2, "sum": 600, "min": 100, "max": 500, "latest": 500},
    ]
    update(OTHER, {"status": "retracted"})
    retracted = reduce(store, request(ops=ops, groups=("status",)))
    assert retracted["groups"] == [
        {"start": "2026-03-01", "status": "retracted", "count": 1, "sum": 300, "min": 300, "max": 300, "latest": 300},
        {"start": "2026-03-02", "status": "recorded", "count": 2, "sum": 600, "min": 100, "max": 500, "latest": 500},
    ]


def test_backfill_keeps_a_new_rollup_unavailable_until_every_row_is_built(store):
    """A rollup declared on a populated collection that serves partial or stale buckets as complete
    while its backfill or concurrent writes are still outstanding."""
    store.create_collection(manifest_path(), manifest(), why="create", scaffold=False)
    records = list(iter_exercises(40))
    load(store, [exercise(record) for record in records[:30]])
    store.revise_collection(CID, manifest_text=manifest(rollups={"daily": DAILY}), why="rollup", **guards(store))
    building = reduce(store, request())
    assert (building["plan"]["strategy"], building["plan"]["reason"]) == ("base", "rollup_building")
    assert store.backfill_rollups(CID, limit=10) is False
    load(store, [exercise(record) for record in records[30:]])
    assert reduce(store, request())["plan"]["strategy"] == "base"
    while not store.backfill_rollups(CID, limit=10):
        pass
    ready = reduce(store, request())
    daily, flagged = expected_daily(records, "metrics.calories")
    assert ready["plan"]["strategy"] == "rollup" and by_bucket(ready) == daily
    assert ready["flagged_rows"] == len(flagged)


def test_held_edits_and_refused_writes_never_reach_rollup_buckets(store):
    """A held row candidate or an edited, held summary page counted into rollup buckets."""
    store.create_collection(manifest_path(), manifest(rollups={"daily": DAILY}), why="create", scaffold=False)
    load(store, [exercise(record) for record in iter_exercises(12)])
    store.reconcile_views()
    before = reduce(store, request(ops=("count", "sum", "max")))
    with pytest.raises(collections.CollectionError):
        store.append_record(CID, item={"id": "held", "start": "2026-03-01T08:00:00+00:00", "calories": "many"},
                            why="observe")
    page = store.root / store.connection.execute(
        "SELECT path FROM projection_state WHERE kind='summary'").fetchone()[0]
    page.write_bytes(page.read_bytes() + b"\n- id: forged\n  calories: 99999\n")
    store.reconcile_views()
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 2
    after = reduce(store, request(ops=("count", "sum", "max")))
    assert after["plan"]["strategy"] == "rollup" and after["groups"] == before["groups"]


def test_failed_write_rolls_back_bucket_updates_with_its_rows(store, monkeypatch):
    """Bucket deltas that survive a write whose rows, versions and generation rolled back."""
    store.create_collection(manifest_path(), manifest(rollups={"daily": DAILY}), why="create", scaffold=False)
    load(store, [exercise(record) for record in iter_exercises(12)])
    before, result = canonical(store), reduce(store, request(ops=("count", "sum", "max", "latest")))

    def refuse(manifest):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "authority lost at commit")

    monkeypatch.setattr(store, "_precommit", refuse)
    with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        store.append_record(CID, item={"id": "late", "start": "2026-03-01T08:00:00+00:00", "calories": 5000},
                            why="observe")
    monkeypatch.undo()
    assert canonical(store) == before
    assert reduce(store, request(ops=("count", "sum", "max", "latest")))["groups"] == result["groups"]


def test_withheld_member_forces_an_exact_base_reduction_equal_to_its_absent_twin(store):
    """A whole-collection rollup answering a mixed-release query, so a withheld row's maximum,
    count or sum reaches another audience instead of the admitted rows' exact reduction."""
    twin_path = manifest_path().replace("/Work/", "/Twin/")
    store.create_collection(manifest_path(), manifest(rollups={"daily": DAILY}, summary=False),
                            why="create", scaffold=False)
    store.create_collection(twin_path, manifest(rollups={"daily": DAILY}, summary=False, cid=COPY_CID,
                                                title="Twin"), why="create", scaffold=False)
    visible = [{"id": "a", "start": "2026-03-01T08:00:00+02:00", "calories": 100},
               {"id": "b", "start": "2026-03-01T20:00:00+02:00", "calories": 200}]
    hidden = {"id": "secret", "start": "2026-03-01T12:00:00+02:00", "calories": 9000}
    for key, row in zip((KEY, OTHER), visible, strict=True):
        store.append_record(CID, item=row, item_key=key, why="observe")
        store.append_record(COPY_CID, item=row, item_key=key, why="observe")
    store.append_record(CID, item=hidden, item_key=THIRD, why="observe")
    write_scope(store.root, paths=f"Records/Work/Items/{THIRD}.md")
    write_rule(store.root, ceiling=0)
    ops = ("count", "sum", "avg", "max", "latest")
    owner = reduce(store, request(ops=ops))
    assert owner["groups"][0]["max"] == 9000
    mixed = reduce(store, request(ops=ops), principal=_external())
    twin = reduce(store, request(ops=ops), cid=COPY_CID, principal=_external())
    assert mixed["plan"]["strategy"] == "base" and mixed["plan"]["reason"] == "mixed_release"
    assert mixed["groups"] == twin["groups"] == [
        {"start": "2026-03-01", "count": 2, "sum": 300, "avg": 150.0, "max": 200, "latest": 200}]
    assert mixed["flagged_rows"] == twin["flagged_rows"] == 0


def test_mixed_release_answers_one_page_and_refuses_an_unbound_continuation(store):
    """A mixed-release answer longer than one page that issues a cursor no visible-state basis
    binds, so its pages could silently mix two states."""
    store.create_collection(manifest_path(), manifest(rollups={"daily": DAILY}, summary=False),
                            why="create", scaffold=False)
    for day, key in enumerate((KEY, OTHER, THIRD), start=1):
        store.append_record(CID, item={"id": key, "start": f"2026-03-0{day}T08:00:00+00:00", "calories": 100},
                            item_key=key, why="observe")
    write_scope(store.root, paths=f"Records/Work/Items/{THIRD}.md")
    write_rule(store.root, ceiling=0)
    with pytest.raises(runtime.QueryError, match="QUERY_UNSUPPORTED: continuation under mixed release"):
        reduce(store, {**request(), "page": {"limit": 1}}, principal=_external())
    whole = reduce(store, {**request(), "page": {"limit": 2}}, principal=_external())
    assert whole["plan"]["reason"] == "mixed_release" and whole["next_cursor"] is None
    assert [group["start"] for group in whole["groups"]] == ["2026-03-01", "2026-03-02"]


def test_percentile_and_distinct_count_refuse_instead_of_assembling_from_rollups(store):
    """A percentile or distinct count fabricated from count/sum/min/max rollup statistics."""
    store.create_collection(manifest_path(), manifest(rollups={"daily": DAILY}), why="create", scaffold=False)
    load(store, [exercise(record) for record in iter_exercises(12)])
    for extra in ({"op": "percentile", "field": "calories", "p": 0.5}, {"op": "distinct_count", "field": "calories"}):
        raw = request(ops=("count",))
        raw["aggregates"]["other"] = extra
        with pytest.raises(runtime.QueryError, match="QUERY_UNSUPPORTED"):
            reduce(store, raw)


SUMMARY_FIELDS = {"date": "{type: date, required: true}", "source": "{type: string}", "steps": "{type: integer}",
                  "resting_hr": "{type: integer}"}
STEPS = {"bucket": "day", "timestamp": "date", "values": {"steps": ["count", "sum", "avg"]}}


def pages(store, raw):
    """Every page of ``raw``, following each page's cursor until one comes back complete."""
    result = [reduce(store, raw)]
    while result[-1]["next_cursor"] is not None:
        result.append(reduce(store, {**raw, "page": {**raw["page"], "after": result[-1]["next_cursor"]}}))
    return result


@pytest.mark.parametrize("strategy", ["rollup", "base"])
def test_daily_answer_longer_than_a_page_is_read_whole_through_cursors(store, strategy):
    """A daily answer past one page that is unreachable or refused, a page of non-ASCII labels past
    the 64 KiB result cap as MCP or REST serializes it, or a continuation that repeats, skips or
    reorders a group on either execution path."""
    rollup = {**STEPS, "group_by": ["source"]}
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date",
                            rollups={"steps": rollup} if strategy == "rollup" else None), why="create", scaffold=False)
    rows = daily_summaries(400)
    load(store, [{"date": row["date"], "steps": row["steps"], "source": f"{row['date']} " + "歩" * 200}
                 for row in rows])
    daily, _ = expected_daily(rows, "steps")
    read = pages(store, request(field="steps", timestamp="date", groups=("source",)))
    assert {page["plan"]["strategy"] for page in read} == {strategy}
    for page in read:
        assert len(default_serializer(page).encode()) <= 64 * 1024  # MCP tool result text
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 64 * 1024  # REST and CLI
    assert [page["truncation_reason"] for page in read] == ["bytes"] * (len(read) - 1) + [None]
    groups = [{name: value for name, value in group.items() if name != "source"}
              for page in read for group in page["groups"]]
    assert {group.pop("date"): group for group in groups} == daily and len(groups) == len(daily) == 400


@pytest.mark.parametrize("bucket,strategy,reason", [
    ("day", "rollup", "rollup_ready"), ("day", "base", "no_matching_rollup"), ("week", "base", "window_unaligned"),
])
def test_local_day_window_returns_exactly_its_days(store, bucket, strategy, reason):
    """A window that drops or adds an edge day, reads stored buckets past either end, or serves a
    window that splits a week from whole stored weeks."""
    declared = {"steps": {**STEPS, "bucket": bucket}} if (bucket, strategy) != ("day", "base") else None
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date", rollups=declared),
                            why="create", scaffold=False)
    rows = daily_summaries(400)
    load(store, rows)
    start, end = "2026-04-08", "2026-06-06"  # a Wednesday to a Saturday: 60 days
    daily, _ = expected_daily([row for row in rows if start <= row["date"] <= end], "steps")
    raw = request(field="steps", timestamp="date", bucket=bucket)
    raw["group_by"][0] |= {"from": start, "to": end}
    read = pages(store, {**raw, "page": {"limit": 50}})
    assert {(page["plan"]["strategy"], page["plan"]["reason"]) for page in read} == {(strategy, reason)}
    assert [page["truncation_reason"] for page in read] == ["limit"] * (len(read) - 1) + [None]
    answer = {group.pop("date"): group for page in read for group in page["groups"]}
    assert answer == (daily if bucket == "day" else weekly(daily))
