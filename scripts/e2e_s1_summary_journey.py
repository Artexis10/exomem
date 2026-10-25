#!/usr/bin/env python
"""Installed-wheel owner journey for the S1 Records summary collection.

OpenSpec add-collection-query-engine task S1.7 (installed MCP owner journey,
independent expected rows and daily results) over the parent change
move-structured-collections-to-sqlite. It runs as one step of the product E2E
(`scripts/e2e_product_loop.py`) on the wheel that run already built and installed.

The journey requires a wheel that releases summary collections and keeps its
installed capability bytes unchanged. It proves that an unenrolled vault
refuses MCP summary creation, then exercises owner enrolment and the released
workflow. It never changes an installed release capability.

All data is invented and generated here from a fixed seed. The expected values
come from `reference_daily` below, written from the raw rows with `datetime`
arithmetic; nothing in it reads a response from the product.

The recipient reads metrics through authenticated MCP. The owner releases and
revokes the invented location fields through REST on the same running writer.
Field release never releases the preserved original.

A second summary collection derives nested October/November samples from protected
archive members. Its independent reference includes both occurrences of the autumn
fold. Restart and restore must reproduce the exact rows; a missing member blocks row
reads during rebuild while complete daily rollups remain available.

The last step (OpenSpec P3.6, the GA gate) runs `exomem collections migrate` on the adopted copy,
which holds the live summary store beside file Records and Planning collections with items. It
compares file-mode and store-mode answers, writes in store mode, edits an item view while the
service is stopped, and restarts. The main vault stays in file mode for the rollback step.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cell_memory_profile as memory_profile  # noqa: E402
import e2e_product_loop as loop  # noqa: E402

# --- the invented export and its independent reference -------------------------

#: Enough rows for an interrupted import to leave work for the restarted service.
EXPORT_ROWS = 1600
EXPORT_SEED = 20261007
WINDOW_START = date(2026, 3, 1)
WINDOW_DAYS = 80
#: UTC offsets in minutes: west of UTC, UTC, east of UTC, a half hour and a far east zone.
OFFSETS = (-480, -300, 0, 60, 330, 720)
#: Evidence path the owner preserves the export at.
EXPORT_SCOPE, EXPORT_CATEGORY, EXPORT_FILENAME = "wearable", "export", "workouts.ndjson"


def _offset_text(minutes: int) -> str:
    hours, mins = divmod(abs(minutes), 60)
    return f"{'+' if minutes >= 0 else '-'}{hours:02d}:{mins:02d}"


def export_rows() -> list[dict[str, Any]]:
    """The invented workout-summary export: offsets, date-only rows, invalid rows, repeated ids.

    Three time shapes occur: a UTC instant with its own offset field, a local instant
    that carries its offset, and a date with no time. A few rows sit within a minute of
    local midnight, so their offset moves them across a day. Invalid rows lack the
    natural key, carry a non-numeric calories value, or name no usable time: an unzoned
    instant or none at all, since the import never guesses a day. Three ids repeat later
    in the file with other values, one of them in a later import batch than its first row.
    """
    rng = random.Random(EXPORT_SEED)
    origin = datetime(WINDOW_START.year, WINDOW_START.month, WINDOW_START.day, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for index in range(EXPORT_ROWS):
        offset = rng.choice(OFFSETS)
        instant = origin + timedelta(minutes=rng.randrange(WINDOW_DAYS * 24 * 60))
        local = instant.astimezone(timezone(timedelta(minutes=offset)))
        distance = rng.randint(1, 12) * 1000
        row: dict[str, Any] = {
            "id": f"wk-{index:05d}",
            "kind": rng.choice(("run", "ride", "swim", "row")),
            "duration_s": rng.randint(600, 5400),
            # Whole numbers, half of them stored as floats, so both subtypes occur.
            "metrics": {
                "calories": rng.randint(80, 950),
                "distance_m": float(distance) if index % 2 else distance,
            },
        }
        # Optional locations cover nested shapes without exceeding the preservation tool's text limit.
        if index < 500:
            row["route"] = {"fixes": [
                {"latitude": 12.345 + index / 100000, "longitude": 64.987 - index / 100000},
                {"latitude": 12.346 + index / 100000, "longitude": 64.988 - index / 100000},
            ]}
            row["place"] = {"label": f"Invented place {index % 7}", "region": "Invented region"}
        # The preview never sees this source subtree; its undeclared values must stay private.
        if index >= EXPORT_ROWS - 800:
            row["context"] = {"unreviewed": {"place": f"Late invented place {index}"}}
        shape = index % 10
        if shape < 4:
            row["start_utc"] = instant.strftime("%Y-%m-%dT%H:%M:%SZ")
            row["utc_offset"] = _offset_text(offset)
        elif shape < 9:
            row["start"] = local.isoformat()
        else:
            row["date"] = local.date().isoformat()
        if index % 250 == 9:
            row.pop("date")
            row["start"] = local.replace(tzinfo=None).isoformat()  # unzoned: refused, never guessed
        if index % 250 == 19:
            row.pop("date", None)
            row.pop("start", None)
            row.pop("start_utc", None)
            row.pop("utc_offset", None)
        rows.append(row)
    for index, (utc_text, offset) in enumerate(
        (
            ("2026-03-10T23:30:00Z", 120),
            ("2026-03-11T00:30:00Z", -300),
            ("2026-03-12T07:59:59Z", -480),
            ("2026-03-12T08:00:00Z", -480),
            ("2026-03-13T11:30:00Z", 720),
            ("2026-03-14T18:29:59Z", 330),
            ("2026-03-14T18:30:00Z", 330),
        )
    ):
        rows.append(
            {
                "id": f"wk-edge-{index}",
                "kind": "run",
                "duration_s": 1800,
                "metrics": {"calories": 400 + index, "distance_m": 5000},
                "start_utc": utc_text,
                "utc_offset": _offset_text(offset),
            }
        )
    rows.insert(40, {"kind": "run", "metrics": {"calories": 100}, "start": "2026-03-05T08:00:00Z"})
    rows.insert(
        900,
        {"id": "wk-bad-calories", "kind": "run", "metrics": {"calories": "n/a"},
         "start": "2026-03-06T08:00:00Z"},
    )
    rows.append({"kind": "swim", "metrics": {"calories": 90}, "date": "2026-03-07"})
    valid = [position for position, row in enumerate(rows) if _is_valid(row)]
    for source, back in ((valid[50], 0), (valid[1300], 500), (valid[100], 2)):
        original = rows[source]
        repeat = {**original, "metrics": {**original["metrics"], "calories": 900 + source % 50}}
        rows.insert(len(rows) - back, repeat)
    return rows


def export_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows)


def _is_valid(row: dict[str, Any]) -> bool:
    """A row the import keeps: a natural key, integer calories, and a day in its own offset."""
    return (
        isinstance(row.get("id"), str)
        and type(row["metrics"]["calories"]) is int
        and _local_day(row) is not None
    )


def _local_day(row: dict[str, Any]) -> date | None:
    """The calendar day in the row's own offset, or None when the row names no time."""
    if "date" in row:
        return date.fromisoformat(row["date"])
    if "start_utc" in row:
        instant = datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00"))
        sign = 1 if row["utc_offset"][0] == "+" else -1
        hours, minutes = row["utc_offset"][1:].split(":")
        return (instant + sign * timedelta(hours=int(hours), minutes=int(minutes))).date()
    if "start" in row:
        local = datetime.fromisoformat(row["start"])
        # The offset travels inside the text; a text without one names no day.
        return local.date() if local.tzinfo is not None else None
    return None


def reference_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The rows an import must leave, by id: valid rows, the last occurrence of an id winning."""
    kept: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _is_valid(row):
            kept[row["id"]] = row
    return kept


def reference_daily(rows: list[dict[str, Any]], metric: str) -> dict[str, dict[str, Any]]:
    """Per local day ``{count, sum, avg}`` of ``metric`` over the rows an import keeps.

    The sum and mean are exact, each rounded once; an all-integer sum stays an integer.
    """
    buckets: dict[str, list[Any]] = {}
    for row in reference_rows(rows).values():
        value = row["metrics"].get(metric)
        if value is not None:
            buckets.setdefault(_local_day(row).isoformat(), []).append(value)
    daily: dict[str, dict[str, Any]] = {}
    for key, values in sorted(buckets.items()):
        exact = sum((Fraction(value) for value in values), Fraction(0))
        total = int(exact) if all(type(value) is int for value in values) else float(exact)
        daily[key] = {"count": len(values), "sum": total, "avg": float(exact / len(values))}
    return daily


# --- the collection, its mapping and the queries -------------------------------

WORKOUTS_ID = "5d1c2f0a-7a3e-4b8d-9c61-3f2a8e4b7d10"
WORKOUTS_PATH = "Knowledge Base/Records/Health/Workouts/_collection.md"
# The offline enrolment needs one summary collection of its own; the journey
# collection is then created through the service, as the owner does from here on.
ENROLMENT_ID = "0e8b7c34-92d5-4f61-a3b8-6c5d1e9f2a47"
ENROLMENT_PATH = "Knowledge Base/Records/Health/Enrolment/_collection.md"

FIELDS = """    exercise_id: {type: string, required: true}
    kind: {type: string}
    duration_s: {type: integer}
    calories: {type: integer}
    distance_m: {type: number}
    started_at: {type: datetime}
    utc_offset: {type: string}
    local_date: {type: date}
    route:
      type: object
      classification: location
      properties:
        fixes:
          type: array
          items:
            type: object
            properties:
              latitude: {type: number}
              longitude: {type: number}
    place:
      type: object
      classification: location
      properties:
        label: {type: string}
        region: {type: string}
    place_label: {type: string, depends_on: [place]}
    extra_context: {type: object}
"""


def summary_manifest(collection_id: str, title: str) -> str:
    return f"""---
type: collection
exomem_id: {collection_id}
title: {title}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
view_mode: summary
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [exercise_id]
  fields:
{FIELDS}---
"""


#: Source path to target field, and the time bases in the order the import tries them.
MAPPING = {
    "fields": {
        "exercise_id": "id",
        "kind": "kind",
        "duration_s": "duration_s",
        "calories": "metrics.calories",
        "distance_m": "metrics.distance_m",
        "route": "route",
        "place": "place",
        "place_label": "place.label",
        "extra_context": "context",
    },
    "coverage": {
        "id": {"classification": None},
        "kind": {"classification": None},
        "duration_s": {"classification": None},
        "metrics.calories": {"classification": None},
        "metrics.distance_m": {"classification": None},
        "start_utc": {"classification": None},
        "utc_offset": {"classification": None},
        "start": {"classification": None},
        "date": {"classification": None},
        "route": {"classification": "location", "subtree": True},
        "place": {"classification": "location", "subtree": True},
    },
    "time": {
        "from": [
            {"instant": "start_utc", "offset": "utc_offset"},
            {"instant": "start"},
            {"date": "date"},
        ],
        "instant": "started_at",
        "offset": "utc_offset",
        "local_date": "local_date",
    },
    "on_invalid": "skip",
}
DAILY = {
    "version": 1,
    "group_by": [{"field": "local_date", "bucket": "day"}],
    "aggregates": {
        "calories_count": {"op": "count", "field": "calories"},
        "calories_sum": {"op": "sum", "field": "calories"},
        "calories_avg": {"op": "avg", "field": "calories"},
        "distance_sum": {"op": "sum", "field": "distance_m"},
    },
}
ROW_FIELDS = ["exercise_id", "calories", "local_date"]


# --- harness --------------------------------------------------------------------

SAMPLES_ID = "714e193a-a755-4fe4-98d9-301aa2c70647"
SAMPLES_PATH = "Knowledge Base/Records/Telemetry/Samples/_collection.md"
SAMPLE_ROWS = {"version": 1, "select": ["sample_id", "value", "at", "utc_offset", "local_date"],
               "order_by": [{"field": "local_date"}]}
SAMPLE_DAILY = {"version": 1, "group_by": [{"field": "local_date", "bucket": "day"}],
                "aggregates": {"count": {"op": "count", "field": "value"},
                               "sum": {"op": "sum", "field": "value"},
                               "avg": {"op": "avg", "field": "value"}}}
SAMPLE_MAPPING = {
    "rows": "days[].samples[]",
    "fields": {"sample_id": "id", "value": "value", "place": "place",
               "place_label": "place.label", "extra": "extra"},
    "coverage": {"id": {"classification": None}, "value": {"classification": None},
                 "at": {"classification": None}, "place": {"classification": "location", "subtree": True}},
    "time": {"from": [{"instant": "at", "zone": "Europe/Paris", "fold": "order"}],
             "instant": "at", "offset": "utc_offset", "local_date": "local_date"},
}


def member_documents() -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """Invented ten-minute samples; expected UTC comes from explicit transition arithmetic.

    Paris repeats 02:00–02:59 on 25 October 2026: +02:00 first, +01:00 second.
    The reference uses those stated offsets, never the product or the host zone database.
    """
    documents, expected = {}, {}
    for month, days in ((10, range(24, 32)), (11, range(1, 8))):
        nested = []
        for day in days:
            clocks = [(minute, 120 if month == 10 and day < 25 else 60) for minute in range(0, 1440, 10)]
            if (month, day) == (10, 25):
                clocks = ([(minute, 120) for minute in range(0, 180, 10)]
                          + [(minute, 60) for minute in range(120, 1440, 10)])
            samples = []
            for index, (minute, offset) in enumerate(clocks):
                wall = datetime(2026, month, day) + timedelta(minutes=minute)
                sample_id = f"sample-{month:02d}{day:02d}-{index:03d}"
                value = day * 100 + index
                samples.append({"id": sample_id, "value": value, "at": wall.isoformat(),
                                "place": {"label": "Invented plateau"}, "extra": {"private": "unreviewed"}})
                expected[sample_id] = {"sample_id": sample_id, "value": value,
                                       "at": (wall - timedelta(minutes=offset)).isoformat() + "Z",
                                       "utc_offset": _offset_text(offset), "local_date": wall.date().isoformat()}
            nested.append({"date": f"2026-{month:02d}-{day:02d}", "samples": samples})
        documents[f"telemetry/{month:02d}.json"] = json.dumps({"days": nested}).encode()
    return documents, expected


def sample_manifest() -> str:
    return f"""---
type: collection
exomem_id: {SAMPLES_ID}
title: Derived telemetry samples
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
view_mode: summary
derived: true
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [sample_id]
  fields:
    sample_id: {{type: string, required: true}}
    value: {{type: integer}}
    at: {{type: datetime}}
    utc_offset: {{type: string}}
    local_date: {{type: date}}
    place:
      type: object
      classification: location
      properties:
        label: {{type: string}}
    place_label: {{type: string, depends_on: [place]}}
    extra: {{type: object}}
---
"""


def sample_import(journey: Journey) -> dict[str, Any]:
    return {"source_ref": journey.member_receipt["path"], "format": "json-document",
            "members": "telemetry/*", "mapping": SAMPLE_MAPPING}


def member_blob(journey: Journey) -> Path:
    manifest = journey.vault / journey.member_receipt["path"]
    return manifest.parent / json.loads(manifest.read_bytes())["members"][1]["blob"]


async def import_samples(journey: Journey) -> None:
    """Installed upload → owner MCP preview → interrupted member import → exact derived rows."""
    import httpx
    import yaml

    with journey.step("3a. preserve nested members, preview and interrupt their derived import"):
        async with journey.recipient_service() as (base_url, _):
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                for name, document in journey.documents.items():
                    zipped.writestr(name, document)
            async with httpx.AsyncClient(timeout=journey.timeout) as client:
                uploaded = await client.post(
                    f"{base_url}/upload", headers={"Authorization": f"Bearer {journey.rest_key}"},
                    data={"scope": "telemetry", "category": "export", "archive": "members"},
                    files={"file": ("invented-samples.zip", archive.getvalue(), "application/zip")},
                )
            expect(uploaded.status_code == 201, "the installed archive upload failed", uploaded.text)
            journey.member_receipt = uploaded.json()
            expect(journey.member_receipt["archive"]["sha256"] == hashlib.sha256(archive.getvalue()).hexdigest(),
                   "the member receipt does not identify the uploaded bytes", journey.member_receipt)
        async with journey.service("member-import") as service:
            await service.mutate("record_memory", action="create", manifest_path=SAMPLES_PATH,
                                 manifest_text=sample_manifest(), why="import the invented nested samples")
            preview = await service.import_job(SAMPLES_ID, mode="preview", **sample_import(journey))
            expect(preview["rows"]["sampled"] == 100 and not preview["mapping"]["findings"]
                   and not preview["time"]["flagged"], "the nested preview misread the invented source", preview)
            manifest = yaml.safe_load(sample_manifest().split("---\n")[1])
            recommended = preview["recommended_declarations"]
            for name, flags in recommended["fields"].items():
                manifest["item_schema"]["fields"][name].update(flags)
            manifest["rollups"] = recommended["rollups"]
            inspected = await service.records(action="inspect", collection=SAMPLES_ID)
            await service.mutate("record_memory", action="revise", collection=SAMPLES_ID,
                                 manifest_text="---\n" + yaml.safe_dump(manifest, sort_keys=False) + "---\n",
                                 why="declare daily sample totals", **inspected["lifecycle_guards"])
            job = await service.import_job(SAMPLES_ID, mode="start", **sample_import(journey))

            async def advanced():
                status = await service.import_job(SAMPLES_ID, mode="status", continuation=job["continuation"])
                expect(status["state"] == "running", "the member import ended before interruption", status)
                return status if status["rows"]["imported"] > 0 else None

            stopped = await until(advanced, within=60, what="a derived member batch to commit", pause=0.02)
            expect(stopped["next_position"]["member"] == 0 and stopped["source_bytes"]["consumed"] == 0
                   and stopped["rows"]["imported"] < len(journey.sample_reference),
                   "the member import did not stop inside a member", stopped)
    with journey.step("3b. resume nested import and prove every October instant and daily result"):
        async with journey.service("member-resume") as service:
            async def finished():
                status = await service.import_job(SAMPLES_ID, mode="status", continuation=job["continuation"])
                expect(status["state"] in ("running", "complete"), "the member import stopped", status)
                return status if status["state"] == "complete" else None

            done = await until(finished, within=90, what="the nested import to finish")
            expect(done["rows"]["imported"] == len(journey.sample_reference)
                   and done["rows"]["rejected"] == done["rows"]["duplicates"] == 0
                   and done["source_bytes"]["consumed"] == sum(map(len, journey.documents.values())),
                   "the resumed member import has incorrect counts", done)
            await compare_samples(journey, service)
            # Re-reading protected members stays an owner operation; cancel exercises the
            # public continuation without changing the source or broadening its release.
            replay = await service.import_job(SAMPLES_ID, mode="start", reimport="all", **sample_import(journey))
            cancelled = await service.import_job(SAMPLES_ID, mode="cancel", continuation=replay["continuation"])
            expect(cancelled["state"] == "partial" and cancelled["reason"] == "cancelled",
                   "the owner could not cancel re-import", cancelled)


async def samples_ready(journey: Journey, service: Session) -> None:
    async def ready():
        try:
            return await service.query({**SAMPLE_ROWS, "page": {"limit": 1}}, SAMPLES_ID)
        except Refused as refusal:
            # These query protocol states mean the writer has not finished preparing rows.
            expect(refusal.code in {"QUERY_REBUILDING", "QUERY_BUSY"},
                   "derived recovery returned an unrelated refusal", str(refusal))
            return None
    await until(ready, within=90, what="derived rows to rebuild from protected members")


async def compare_sample_days(journey: Journey, service: Session) -> None:
    buckets: dict[str, list[int]] = {}
    for row in journey.sample_reference.values():
        buckets.setdefault(row["local_date"], []).append(row["value"])
    expected = [{"local_date": day, "count": len(values), "sum": sum(values),
                 "avg": sum(values) / len(values)} for day, values in sorted(buckets.items())]
    groups, plans = await paged(service, SAMPLE_DAILY, "groups", 13, SAMPLES_ID)
    expect(groups == expected, "derived daily count/sum/avg differs from the independent source reference",
           {"groups": groups, "expected": expected})
    expect(all(plan["strategy"] == "rollup" for plan in plans), "derived daily totals did not use rollups", plans)


async def compare_samples(journey: Journey, service: Session) -> None:
    rows, _ = await paged(service, SAMPLE_ROWS, "rows", None, SAMPLES_ID)
    expect(len(rows) == len(journey.sample_reference)
           and {row["sample_id"]: row for row in rows} == journey.sample_reference,
           "derived rows differ from the independent source reference, including the October fold",
           {"count": len(rows), "first": rows[:1]})
    await compare_sample_days(journey, service)


async def discover_and_plan(journey: Journey, service: Session) -> None:
    """An installed client can discover and use each first-slice query lifecycle route."""
    from exomem import tool_surface

    await samples_ready(journey, service)
    described = await service.records(action="describe")
    expect(described.get("query", {}).get("grammar", {}).get("args", {}).get("name") == "collections"
           and described.get("import", {}).get("zone_rules", {}).get("package") == "tzdata",
           "store describe omitted query discovery or pinned import zone rules", described)
    sizes = {}
    for name in ("collections", "import"):
        chapter = await service.call("schema_memory", subject="query-engine", operation="inspect", name=name)
        sizes[name] = len(json.dumps(chapter, ensure_ascii=False, separators=(",", ":")).encode())
        # The query-engine discovery contract fixes the chapter ceiling at 64 KiB.
        from exomem.command_surface import find_rendering
        from exomem.commands import PRODUCT_COMMANDS

        route_tool = chapter["route"]["tool"]
        # Over MCP the route names the listed Records rendering for the served shape.
        found = find_rendering(route_tool, PRODUCT_COMMANDS)
        routes_records = route_tool == "record_memory" or (found is not None and found[0].name == "record_memory")
        expect(chapter["chapter"] == name and routes_records
               and sizes[name] <= 64 * 1024, "installed query grammar is absent or exceeds its byte bound", chapter)
    for mode in ("compose", "explain", "preview", "dry_run"):
        planned = await service.query({**SAMPLE_DAILY, "mode": mode, "execution_profile": "analytics"}, SAMPLES_ID)
        expect(planned["mode"] == mode and planned["execution_profile"] == "analytics"
               and "rows" not in planned and "groups" not in planned,
               f"installed {mode} did not plan without returning values", planned)
    analytical = await service.query({**SAMPLE_DAILY, "execution_profile": "analytics"}, SAMPLES_ID)
    interactive = await service.query(SAMPLE_DAILY, SAMPLES_ID)
    expect(analytical["groups"] == interactive["groups"] and analytical["execution_profile"] == "analytics",
           "explicit analytics changed the daily answer", analytical)
    tools = await service.client.list_tools()
    measured = importlib.util.spec_from_file_location(
        "measure_tool_schema_bytes", Path(__file__).with_name("measure-tool-schema-bytes.py"))
    measure = importlib.util.module_from_spec(measured)
    measured.loader.exec_module(measure)
    wires = [tool.model_dump(mode="json", by_alias=True) for tool in tools]
    schema_bytes = sum(row["total"] for row in measure.measure(
        [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]))
    bootstrap = await service.call("bootstrap", profile="compact")
    journey.measurements.update({"source": "installed stdio MCP discovery on the invented journey vault",
                            "query_chapter_bytes": sizes, "zone_rules": described["import"]["zone_rules"],
                            "installed_schema_bytes": schema_bytes,
                            "installed_surface": tool_surface.discovery_contract(wires),
                            "compact_core_bytes": len(json.dumps(bootstrap)),
                            "derived_source_rows": len(journey.sample_reference)})


async def protected_members(journey: Journey, owner: Session, recipient: Session) -> None:
    """Metrics stay useful while archive handles, unknown fields and locations stay private."""
    default = await recipient.query({"version": 1, "page": {"limit": 2}}, SAMPLES_ID)
    expect(default["rows"] and all(not {"place", "place_label", "extra"}.intersection(row)
                                   for row in default["rows"]),
           "derived default rows disclosed a protected field or hid all metrics", default)
    for field in ("place", "place_label", "extra", "absent_field"):
        await recipient.refused("QUERY_FIELD_UNAVAILABLE", "record_memory", action="query",
                                collection=SAMPLES_ID, query={"version": 1, "select": [field]})
    preview = await owner.import_job(SAMPLES_ID, mode="preview", **sample_import(journey))
    expect(preview["rows"]["sampled"] == 100, "the owner lost the protected re-import handle", preview)
    for path in (journey.member_receipt["path"], journey.member_receipt["path"] + ".absent"):
        await recipient.refused("IMPORT_SOURCE_NOT_FOUND", "record_memory", action="import",
                                collection=SAMPLES_ID,
                                import_request={"mode": "preview", **sample_import(journey), "source_ref": path})
    for path in (journey.member_receipt["path"], member_blob(journey).relative_to(journey.vault).as_posix()):
        for candidate in (path, path + ".absent"):
            try:
                await recipient.call("read_memory", path=candidate)
            except Refused as refusal:
                expect(refusal.message.removeprefix("Error calling tool 'read_memory': ") == "[withheld]",
                       "the member read disclosed a protected path or existence", refusal.message)
            else:
                raise RuntimeError("the recipient read a protected member or manifest")

LEASE_VAULT_ID = "s1-journey-vault"
LEASE_TOKEN = "s1-journey-writer-token"
OPERATOR_TOKEN = "s1-journey-operator-token"
LEASE_TTL = 4.0
RECIPIENT_ID = 20261007


class Refused(RuntimeError):
    """A tool call the service refused; ``code`` is the product's own refusal code."""

    def __init__(self, tool: str, code: str, message: str, *, error: dict[str, Any] | None = None) -> None:
        super().__init__(f"{tool} refused {code}: {message}")
        self.code = code
        self.message = message
        self.error = error


def expect(condition: object, message: str, observed: object = None) -> None:
    if not condition:
        detail = "" if observed is None else f"\nobserved: {json.dumps(observed, default=str)[:3000]}"
        raise RuntimeError(f"{message}{detail}")


def process_starttime(pid: int) -> int:
    """Read Linux's process identity without interpreting spaces or parentheses in its name."""
    # Fields after the final comm delimiter start at 3; Linux fixes starttime at field 22.
    fields = (Path("/proc") / str(pid) / "stat").read_text().rpartition(")")[2].split()
    return int(fields[19])


def process_memory(pid: int) -> dict[str, Any]:
    """Observe one process, including native allocations, with identity checks around the reads."""
    try:
        starttime = process_starttime(pid)
        memory = memory_profile.read_smaps_rollup(pid)
        peak = memory_profile.read_peak_rss(pid)
        if starttime != process_starttime(pid) or memory is None or peak is None:
            raise ValueError("process identity or memory observation unavailable")
        return {"status": "observed", "pid": pid, "starttime_ticks": starttime,
                "rss_bytes": memory["rss_bytes"], "peak_rss_bytes": peak,
                "monotonic_ns": time.monotonic_ns()}
    except (OSError, ValueError, IndexError, KeyError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}


def record_process_observation(path: Path, launch: str) -> None:
    """Publish launch metadata before the installed entry point; observation never controls startup."""
    record = {**process_memory(os.getpid()), "launch_id": launch}
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, path)


def memory_window(service, baseline, *, scope, progress) -> dict[str, Any]:
    """Keep separate process windows and their raw observations; do not sum their peaks."""
    final = service.memory_snapshot()
    result = {"scope": scope, "baseline": baseline, "final": final, "progress": progress,
              "method": "final process VmHWM minus baseline smaps_rollup Rss",
              "limits": "earlier peaks can inflate; kernel accounting is approximate; children and swap excluded"}
    if baseline["status"] != "observed" or final["status"] != "observed":
        return {**result, "status": "unavailable"}
    if (baseline["pid"], baseline["starttime_ticks"]) != (final["pid"], final["starttime_ticks"]):
        return {**result, "status": "unavailable", "reason": "process_identity_changed"}
    delta = final["peak_rss_bytes"] - baseline["rss_bytes"]
    if delta < 0:
        return {**result, "status": "unavailable", "reason": "peak_below_baseline"}
    return {**result, "status": "observed", "additional_reported_mib": delta / memory_profile.MIB}


class Session:
    """One MCP session on a running service."""

    def __init__(self, client, timeout: float, *, process_observation=None) -> None:
        self.client, self.timeout = client, timeout
        self.process_observation = process_observation

    def memory_snapshot(self, *, startup=False) -> dict[str, Any]:
        if self.process_observation is None:
            return {"status": "unavailable", "reason": "worker_not_observed"}
        path, launch = self.process_observation
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
            if recorded["launch_id"] != launch:
                raise ValueError("observation belongs to another launch")
            if recorded["status"] != "observed":
                return recorded
            current = process_memory(int(recorded["pid"]))
            if current["status"] != "observed":
                return current
            if current["starttime_ticks"] != recorded["starttime_ticks"]:
                raise ValueError("observation belongs to another process")
            return recorded if startup else current
        except (OSError, ValueError, IndexError, KeyError, TypeError) as error:
            return {"status": "unavailable", "reason": type(error).__name__}

    async def call(self, tool: str, **arguments: Any) -> Any:
        result = await asyncio.wait_for(
            self.client.call_tool(tool, arguments, raise_on_error=False), timeout=self.timeout
        )
        if getattr(result, "is_error", False):
            text = "".join(getattr(block, "text", "") for block in result.content)
            # This checks the product's fixed refusal-code format, never the meaning of user text.
            code = re.search(r"\b([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)\b", text)
            raise Refused(tool, code.group(1) if code else "UNKNOWN", text)
        data = getattr(result, "structured_content", None)
        if data is None:
            data = loop._result_data(result)
        elif isinstance(data, dict) and set(data) == {"result"}:
            # Unwrap only the SDK envelope; discovery chapters contain their own result grammar.
            data = data["result"]
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("success") is False and isinstance(error, dict):
            raise Refused(tool, str(error.get("code")), str(error.get("message")), error=error)
        return data

    async def mutate(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """A write that must commit; returns its full diagnostics."""
        return loop._mutation_diagnostics(
            await self.call(tool, **arguments, response_detail="full"), operation=tool
        )

    async def refused(self, code: str, tool: str, **arguments: Any) -> None:
        try:
            await self.call(tool, **arguments)
        except Refused as refusal:
            expect(refusal.code == code, f"{tool} refused {refusal.code}, expected {code}")
            return
        raise RuntimeError(f"{tool} succeeded, expected it to refuse {code}")

    async def records(self, **arguments: Any) -> Any:
        return await self.call("record_memory", **arguments)

    async def query(self, query: dict[str, Any], collection: str = WORKOUTS_ID) -> Any:
        started = time.monotonic()
        try:
            return await self.records(action="query", collection=collection, query=query)
        except Refused as refusal:
            # The query protocol permits the same read after this explicit transient refusal.
            if (refusal.code != "QUERY_TIMEOUT" or not isinstance(refusal.error, dict)
                    or refusal.error.get("retryable") is not True):
                raise
        print("s1-journey: query attempt=1 refused=QUERY_TIMEOUT retryable=true "
              f"roundtrip_ms={(time.monotonic() - started) * 1000:.3f}; retry=1/1", flush=True)
        outcome = "failed"
        try:
            result = await self.records(action="query", collection=collection, query=query)
            outcome = "complete"
            return result
        finally:
            print(f"s1-journey: query attempts=2 outcome={outcome} "
                  f"total_roundtrip_ms={(time.monotonic() - started) * 1000:.3f}", flush=True)

    async def import_job(self, collection: str, **request: Any) -> Any:
        return await self.records(action="import", collection=collection, import_request=request)


class RestOwner(Session):
    """The authenticated owner REST facade on the recipient's running writer."""

    def __init__(self, base_url: str, journey: Journey) -> None:
        super().__init__(None, journey.timeout)
        self.base_url, self.token = base_url, journey.rest_key

    async def call(self, tool: str, **arguments: Any) -> Any:
        status, payload = await asyncio.to_thread(
            loop._http_json, f"{self.base_url}/api/{tool}", method="POST",
            body=arguments, token=self.token, timeout=self.timeout,
        )
        error = payload.get("error")
        if payload.get("success") is False and isinstance(error, dict):
            raise Refused(tool, str(error.get("code")), str(error.get("message")), error=error)
        expect(status == 200 and payload.get("success") is True,
               f"owner REST {tool} failed", {"status": status, "payload": payload})
        return payload["data"]


class Journey:
    """One run's scratch paths, environments and per-step timings."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.executable, self.python = Path(args.executable), Path(args.python)
        self.older_python = getattr(args, "older_python", "")
        compatible = getattr(args, "compatible_python", "")
        self.compatible_python = Path(compatible).expanduser().absolute() if compatible else None
        self.vault, self.work, self.home = Path(args.vault), Path(args.work), Path(args.home)
        self.work.mkdir(parents=True, exist_ok=True)
        # The outer E2E collector rescues this directory before scratch cleanup.
        self.observer_log_root = self.home / "logs" / "query-observation"
        self.timeout = args.request_timeout
        self.initial_records_summary_enabled = loop._installed_records_summary_enabled(
            self.python, env=loop._clean_env(self.home, self.vault), cwd=self.work,
        )
        self.rows = export_rows()
        self.export = export_bytes(self.rows)
        self.timings: dict[str, float] = {}
        self.measurements: dict[str, Any] = {}
        self.coordinator_url = ""
        self.lease_vault_id = LEASE_VAULT_ID
        self.replica_id = "service"
        self.state_root = self.home / "state"
        self.lease_state = self.work / "lease-service"
        self.lease_state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file_collections: dict[str, Any] = {}
        self.source_receipt: dict[str, Any] = {}
        self.member_receipt: dict[str, Any] = {}
        self.documents, self.sample_reference = member_documents()
        self.rest_key = secrets.token_hex(32)

    @property
    def export_path(self) -> str:
        path = self.source_receipt.get("stored_path")
        if not isinstance(path, str) or not path:
            raise RuntimeError("the export has no preserved-source receipt")
        return path

    @property
    def import_request(self) -> dict[str, Any]:
        return {"source_ref": self.export_path, "format": "ndjson", "mapping": MAPPING}

    def lease_env(self) -> dict[str, str]:
        """The service's environment: a writer lease and no operator credential."""
        env = loop._clean_env(self.home, self.vault)
        env.update(
            {
                "EXOMEM_WRITER_LEASE_URL": self.coordinator_url,
                "EXOMEM_WRITER_LEASE_VAULT_ID": self.lease_vault_id,
                "EXOMEM_WRITER_LEASE_REPLICA_ID": self.replica_id,
                "EXOMEM_WRITER_LEASE_TOKEN": LEASE_TOKEN,
                "EXOMEM_WRITER_LEASE_TTL": str(LEASE_TTL),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(self.lease_state),
                "EXOMEM_STATE_ROOT": str(self.state_root),
                # The service takes the lease at start and takes it back after a handoff.
                "EXOMEM_WRITER_LEASE_PREFERRED": "1",
            }
        )
        return env

    @contextlib.contextmanager
    def step(self, name: str):
        started = time.monotonic()
        print(f"s1-journey: {name}", flush=True)
        try:
            yield
        finally:
            self.timings[name] = round(time.monotonic() - started, 1)

    @contextlib.asynccontextmanager
    async def service(self, label: str, *, measure_memory=False, python=None) -> AsyncIterator[Session]:
        """The installed stdio service on this vault, holding the writer lease while it runs."""
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        log = self.work / f"service-{label}.log"
        observation = ((self.observer_log_root / label / (secrets.token_hex(16) + ".json"), secrets.token_hex(16))
                       if measure_memory else None)
        server = self.observed_server(label, "--transport", "stdio", process_observation=observation)
        if python is not None:
            server[0] = str(python)
        client = Client(
            StdioTransport(
                command=server[0],
                args=server[1:],
                env=self.lease_env(),
                cwd=str(self.work),
                keep_alive=False,
                log_file=log,
            ),
            timeout=self.timeout,
            init_timeout=self.timeout,
        )
        async with contextlib.AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(client)
            except Exception as error:  # noqa: BLE001 - the service log holds the actual refusal
                tail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
                raise RuntimeError(
                    f"service '{label}' did not start ({error})\n--- {log.name} tail ---\n{tail}"
                ) from error
            yield Session(client, self.timeout, process_observation=observation)

    def observed_server(self, label: str, *arguments: str, process_observation=None) -> list[str]:
        """Launch the same installed module with temporary failure-only observation."""
        from _recipient_query_observer import LOG_NAME

        # Labels name the harness's observed process launches, not user categories.
        log_path = self.observer_log_root / label / LOG_NAME
        observation = ([] if process_observation is None else
                       ["--observe-process", str(process_observation[0]), process_observation[1]])
        return [str(self.python), str(Path(__file__).resolve()),
                "--observe-query-timeouts", str(log_path), *observation, *arguments]

    @contextlib.asynccontextmanager
    async def recipient_service(self) -> AsyncIterator[tuple[str, Session]]:
        """One installed writer: recipient MCP reads and owner REST release changes."""
        import httpx
        from fastmcp import Client

        (reservation,) = loop._reserve_port_reservations(1)
        port = int(reservation.getsockname()[1])
        base_url = f"http://127.0.0.1:{port}"
        env = self.lease_env()
        env.update({
            "EXOMEM_BASE_URL": base_url,
            "GITHUB_CLIENT_ID": "s1-test-client",
            "GITHUB_CLIENT_SECRET": "s1-test-secret",
            "EXOMEM_GITHUB_USERNAME": "s1-recipient",
            "EXOMEM_GITHUB_USER_ID": str(RECIPIENT_ID),
            "EXOMEM_JWT_SIGNING_KEY": secrets.token_hex(32),
            "EXOMEM_REST_API_KEY": self.rest_key,
            "EXOMEM_UPLOAD_TOKEN": self.rest_key,
            "EXOMEM_OAUTH_STORAGE_URL": self.coordinator_url,
            "EXOMEM_OAUTH_STORAGE_NAMESPACE": self.lease_vault_id,
            "EXOMEM_OAUTH_STORAGE_TOKEN": LEASE_TOKEN,
        })
        token_file = self.work / "recipient-token"
        try:
            loop._run([
                str(self.python), str(Path(__file__).resolve()),
                "--issue-recipient-session", str(token_file),
                "--python", str(self.python), "--executable", str(self.executable),
                "--vault", str(self.vault), "--work", str(self.work), "--home", str(self.home),
            ], env=env, cwd=self.work, timeout=self.timeout)
        finally:
            reservation.close()
        log = self.work / "service-recipient.log"
        with log.open("w", encoding="utf-8") as handle:
            process = subprocess.Popen(self.observed_server(
                "recipient", "--transport", "http",
                "--host", "127.0.0.1", "--port", str(port),
            ), cwd=self.work, env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
            try:
                loop._wait_http_ready(
                    lambda: loop._http_json(f"{base_url}/api/openapi.json", timeout=1)[0] == 200,
                    proc=process, deadline=time.monotonic() + self.timeout,
                    log=log, label="recipient service",
                )
                for bearer in (None, "invalid-s1-token"):
                    # HTTP authentication can refuse with an empty body; only its status is relevant here.
                    response = httpx.get(
                        f"{base_url}/mcp", timeout=self.timeout,
                        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
                    )
                    expect(response.status_code == 401, "unauthenticated MCP ingress was accepted", response.status_code)
                async with Client(
                    f"{base_url}/mcp", auth=token_file.read_text(encoding="utf-8"), timeout=self.timeout,
                ) as client:
                    yield base_url, Session(client, self.timeout)
            finally:
                loop._terminate(process, self.timeout)
                token_file.unlink(missing_ok=True)

    def owner_cli(self, *arguments: str, operator: bool = True) -> subprocess.CompletedProcess[str]:
        """The owner's offline `exomem collections ...`, with the operator credential in its shell."""
        env = self.lease_env()
        if operator:
            env["EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"] = OPERATOR_TOKEN
        return subprocess.run(
            [str(self.executable), "collections", *arguments, "--vault", str(self.vault)],
            env=env,
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=max(60.0, self.timeout * 3),
            check=False,
        )

    def owner_cli_after_stop(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Wait for the stopped service's lease before the owner's offline operation."""
        deadline = time.monotonic() + LEASE_TTL * 3 + 10
        while True:
            result = self.owner_cli(*arguments)
            # This protocol code refuses before effects; the stopped service's lease can outlive it.
            if SERVICE_ACTIVE not in result.stderr or time.monotonic() > deadline:
                return result
            time.sleep(0.5)

    def fork(self, label: str, vault: Path) -> Journey:
        child = Journey(argparse.Namespace(
            executable=str(self.executable), python=str(self.python), older_python=self.older_python,
            compatible_python=self.compatible_python,
            vault=str(vault), work=str(self.work / label / "work"), home=str(self.work / label / "home"),
            request_timeout=self.timeout,
        ))
        child.coordinator_url = self.coordinator_url
        child.lease_vault_id = f"{LEASE_VAULT_ID}-{label}"
        child.replica_id = label
        child.observer_log_root = self.observer_log_root
        child.file_collections = self.file_collections
        child.source_receipt = self.source_receipt
        child.member_receipt = self.member_receipt
        return child

    def installed_helper(self, option: str, target: Path, **extra: str) -> dict[str, Any]:
        command = [str(self.python), str(Path(__file__).resolve()), option, str(target),
                   "--python", str(self.python), "--executable", str(self.executable),
                   "--vault", str(self.vault), "--work", str(self.work), "--home", str(self.home)]
        for name, value in extra.items():
            command.extend([f"--{name.replace('_', '-')}", value])
        result = loop._run(command, env=self.lease_env(), cwd=self.work, timeout=max(90, self.timeout * 4))
        return json.loads(result.stdout.strip().splitlines()[-1])


def tree_digest(root: Path, relative: str) -> str:
    """One hash over every file under ``relative``: its path and its bytes."""
    digest = hashlib.sha256()
    for path in sorted(item for item in (root / relative).rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


async def until(check: Callable, *, within: float, what: str, pause: float = 0.1) -> Any:
    """Poll an async check until it returns something truthy."""
    deadline = time.monotonic() + within
    while True:
        found = await check()
        if found:
            return found
        expect(time.monotonic() < deadline, f"timed out after {within:.0f}s waiting for {what}")
        await asyncio.sleep(pause)


# --- the journey ------------------------------------------------------------------

ENROLMENT_REQUIRED = "COLLECTION_STORE_ENROLLMENT_REQUIRED"
SERVICE_ACTIVE = "COLLECTION_STORE_SERVICE_ACTIVE"
#: What the owner's offline enrolment leaves in the vault: the replica and the routing marker.
STORE_FILES = ("Knowledge Base/_Collections/collections.sqlite", "Knowledge Base/_Collections/mode.json")
PLAN_ITEMS = {
    "b4596ce9-10fd-4856-a26e-89d8be72b0db": {
        "title": "Reliable Planning delivery",
        "kind": "outcome",
        "status": "planned",
        "priority": "high",
        "commitment": "considering",
        "horizon": "year",
    },
    "95a51c93-7873-4262-a9ba-bc3ae7ed362b": {
        "title": "Investigate planning query edge case",
        "tags": ["bug"],
    },
}


async def create_file_collections(journey: Journey, service: Session) -> None:
    """A (Records) and B (Planning), authored as files before any store exists."""
    records = loop._write_records_fixture(journey.vault)
    planning = loop._write_planning_fixtures(journey.vault)["software"]
    await service.mutate(
        "record_memory",
        action="create",
        manifest_path=records["collection"],
        manifest_text=records["manifest_text"],
        why="file Records collection A, created before the store",
    )
    for title in ("Pull", "Push"):
        await service.mutate(
            "record_memory",
            action="append",
            collection=records["collection"],
            item={"occurred_on": "2026-08-04", "title": title, "status": "completed"},
            why="file Records row",
        )
    await service.mutate(
        "plan_memory",
        action="create",
        manifest_path=planning["collection"],
        manifest_text=planning["manifest_text"],
        why="file Planning collection B, created before the store",
    )
    for plan_id, item in PLAN_ITEMS.items():
        await service.mutate(
            "plan_memory",
            action="add",
            collection=planning["collection"],
            plan_id=plan_id,
            item=item,
            why="file Planning item",
        )
    journey.file_collections = {
        "records": records["collection"],
        "planning": planning["collection"],
        "digests": {},
    }
    journey.file_collections["digests"] = file_digests(journey)


def file_digests(journey: Journey) -> dict[str, str]:
    return {
        name: tree_digest(journey.vault, Path(journey.file_collections[name]).parent.as_posix())
        for name in ("records", "planning")
    }


async def file_collections_unchanged(journey: Journey, service: Session, when: str) -> None:
    """A and B hold the bytes they were created with, and both still answer."""
    expect(
        file_digests(journey) == journey.file_collections["digests"],
        f"file collections A and B changed their bytes {when}",
        {"before": journey.file_collections["digests"], "after": file_digests(journey)},
    )
    rows = await service.records(
        action="query",
        collection=journey.file_collections["records"],
        columns=["occurred_on", "title", "status"],
        limit=20,
    )
    titles = sorted(row["title"] for row in rows["rows"])
    expect(titles == ["Pull", "Push"], f"file Records collection A answered wrongly {when}", rows)
    plan = await service.call(
        "plan_memory",
        action="query",
        collection=journey.file_collections["planning"],
        limit=20,
        lifecycle="all",
    )
    expect(
        {row["plan_id"] for row in plan["rows"]} == set(PLAN_ITEMS),
        f"file Planning collection B answered wrongly {when}",
        plan,
    )


async def preserve_export(journey: Journey, service: Session) -> None:
    """S1.0: the owner keeps the raw export as Evidence; its receipt must match the bytes."""
    receipt = await service.mutate(
        "preserve_evidence",
        scope=EXPORT_SCOPE,
        category=EXPORT_CATEGORY,
        filename=EXPORT_FILENAME,
        content=journey.export.decode("utf-8"),
        description="invented workout-summary export",
        raw_protection=True,
    )
    journey.source_receipt = receipt
    digest = hashlib.sha256(journey.export).hexdigest()
    expect(
        receipt.get("state") == "stored"
        and Path(journey.export_path).parent
        == Path("Knowledge Base/Evidence") / EXPORT_SCOPE / EXPORT_CATEGORY
        and receipt.get("hash_algorithm") == "sha256"
        and receipt.get("hash") == digest
        and receipt.get("size") == len(journey.export),
        "the preserve receipt does not match the invented export's bytes",
        {"receipt": receipt, "expected_hash": digest, "expected_size": len(journey.export)},
    )
    expect(
        hashlib.sha256((journey.vault / journey.export_path).read_bytes()).hexdigest() == digest,
        "the preserved Evidence file is not the export's exact bytes",
    )


async def refuse_unenrolled(journey: Journey, service: Session) -> None:
    with journey.step("0. released phase: MCP create on an unenrolled vault refuses"):
        await service.refused(
            ENROLMENT_REQUIRED,
            "record_memory",
            action="create",
            manifest_path=WORKOUTS_PATH,
            manifest_text=summary_manifest(WORKOUTS_ID, "Workout summaries"),
            why="a NEW summary collection on an unenrolled vault",
        )
        await file_collections_unchanged(journey, service, "while the vault is unenrolled")


async def unenrolled_phase(journey: Journey) -> None:
    async with journey.service("unenrolled") as service:
        with journey.step("A/B: file Records and Planning collections, created before the store"):
            await create_file_collections(journey, service)
            await file_collections_unchanged(journey, service, "right after creation")
        with journey.step("1. preserve the invented export and check its receipt"):
            await preserve_export(journey, service)
        await refuse_unenrolled(journey, service)
    enrolment = journey.work / "enrolment.md"
    enrolment.write_text(summary_manifest(ENROLMENT_ID, "Enrolment"), encoding="utf-8")
    leftovers = [name for name in STORE_FILES if (journey.vault / name).exists()]
    expect(not leftovers, "the unenrolled phase left store files in the vault", leftovers)


def installed_capability_file(journey: Journey) -> Path:
    found = loop._run(
        [
            str(journey.python),
            "-c",
            "import importlib.util as u;"
            "print(u.find_spec('exomem.collection_store.capability').origin)",
        ],
        env=loop._clean_env(journey.home, journey.vault),
        cwd=journey.work,
        timeout=60,
    ).stdout.strip()
    path = Path(found)
    # Inspect the installed wheel's capability bytes, not a source checkout.
    expect("site-packages" in path.parts, "capability.py is not an installed-wheel file", found)
    return path


async def released_phase_enrol(journey: Journey) -> None:
    with journey.step("2. owner enrols the vault: offline `exomem collections create`"):
        enrolment = journey.work / "enrolment.md"
        enrolled = journey.owner_cli_after_stop(
            "create", "--manifest-path", ENROLMENT_PATH, "--manifest-file", str(enrolment),
            "--why", "enrol the vault's collection store",
        )  # fmt: skip
        expect(
            enrolled.returncode == 0,
            "the owner's offline create did not enrol the vault",
            {"returncode": enrolled.returncode, "stderr": enrolled.stderr[-1500:]},
        )
        result = json.loads(enrolled.stdout.strip().splitlines()[-1])
        expect(result.get("status") == "marker_admitted", "enrolment did not admit the store", result)
        missing = [name for name in STORE_FILES if not (journey.vault / name).exists()]
        expect(not missing, "enrolment did not leave the store files", missing)


def revised_manifest(recommended: dict[str, Any]) -> str:
    """The manifest with the preview's recommended declarations merged in, as the chapter teaches."""
    import yaml

    data = yaml.safe_load(summary_manifest(WORKOUTS_ID, "Workout summaries").split("---\n")[1])
    for name, flags in recommended["fields"].items():
        data["item_schema"]["fields"][name].update(flags)
    # The field-release proof selects one source row through a declared scalar predicate.
    data["item_schema"]["fields"]["exercise_id"]["filterable"] = True
    data["rollups"] = recommended["rollups"]
    return "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n"


async def create_and_start_import(journey: Journey) -> tuple[str, int]:
    """Steps 2 and 3 up to the interruption; returns the job token and the rows imported at the stop."""
    expected = len(reference_rows(journey.rows))
    async with journey.service("import", measure_memory=True) as service:
        await file_collections_unchanged(journey, service, "when the store first serves")
        with journey.step("2. record_memory creates the NEW summary collection"):
            created = await service.mutate(
                "record_memory",
                action="create",
                manifest_path=WORKOUTS_PATH,
                manifest_text=summary_manifest(WORKOUTS_ID, "Workout summaries"),
                why="NEW summary collection for the workout export",
            )
            receipt = created.get("receipt", {})
            expect(
                receipt.get("operation") == "create"
                and receipt.get("outcome") == "committed"
                and created.get("collection_id") == WORKOUTS_ID,
                "summary create did not commit",
                created,
            )
            inspected = await service.records(action="inspect", collection=WORKOUTS_ID)
            expect(
                inspected["contract"]["view_mode"] == "summary",
                "the created collection is not a summary collection",
                inspected.get("contract"),
            )
        with journey.step("3. import preview, one governed revise, start"):
            preview = await service.import_job(WORKOUTS_ID, mode="preview", **journey.import_request)
            recommended = preview["recommended_declarations"]
            # The fixture introduces unmapped context after the preview's first 100 rows.
            findings = preview["mapping"]["findings"]
            expect(
                preview["rows"]["sampled"] == 100
                and len(findings) == 1
                and findings[0]["code"] == "IMPORT_PATH_ABSENT"
                and findings[0]["at"] == "mapping.fields.extra_context"
                and "extra_context" not in preview["mapping"]["inferred"]
                and recommended["fields"]
                and recommended["rollups"]
                and recommended["omitted"] == [],
                "import preview did not recommend declarations for the daily rollup",
                preview,
            )
            guards = inspected["lifecycle_guards"]
            await service.mutate(
                "record_memory",
                action="revise",
                collection=WORKOUTS_ID,
                manifest_text=revised_manifest(recommended),
                why="declare the recommended daily rollups and the field-release lookup",
                **guards,
            )
            baseline = service.memory_snapshot()
            job = await service.import_job(WORKOUTS_ID, mode="start", **journey.import_request)
            expect(job["state"] == "running", "the import did not start running", job)
        with journey.step("3. stop the service mid-job"):
            async def advanced():
                status = await service.import_job(
                    WORKOUTS_ID, mode="status", continuation=job["continuation"]
                )
                expect(status["state"] == "running", "the import ended before it was stopped", status)
                return status if status["rows"]["imported"] > 0 else None

            at_stop = await until(advanced, within=60, what="the first import batch to commit", pause=0.02)
            imported = at_stop["rows"]["imported"]
            expect(
                0 < imported < expected,
                "the import was not mid-job when the service stopped; enlarge the export",
                at_stop,
            )
            journey.measurements.setdefault("import_memory", []).append(memory_window(
                service, baseline, scope="warm import start through the observed interrupted batch",
                progress=at_stop["rows"]))
    return job["continuation"], imported


async def resume_import(journey: Journey, continuation: str, imported_at_stop: int) -> None:
    rows = reference_rows(journey.rows)
    valid_lines = sum(1 for row in journey.rows if _is_valid(row))
    expected = {
        "imported": len(rows),
        "rejected": len(journey.rows) - valid_lines,
        "duplicates": valid_lines - len(rows),
    }
    async with journey.service("resume", measure_memory=True) as service:
        baseline = service.memory_snapshot(startup=True)
        with journey.step("3. restart: the job resumes and completes with exact counts"):
            first = await service.import_job(WORKOUTS_ID, mode="status", continuation=continuation)
            expect(
                first["state"] == "running"
                # `unverified` until this host re-proves the source bytes; `lost` would pause the job.
                and first["authority"] in ("current", "unverified")
                and first["rows"]["imported"] >= imported_at_stop,
                "the restarted service did not pick the interrupted job up",
                first,
            )

            async def finished():
                status = await service.import_job(
                    WORKOUTS_ID, mode="status", continuation=continuation
                )
                expect(status["state"] in ("running", "complete"), "the import stopped", status)
                return status if status["state"] == "complete" else None

            done = await until(finished, within=90, what="the resumed import to complete")
            counts = {name: done["rows"][name] for name in expected}
            expect(counts == expected, "the resumed import's counts differ from the export's", done)
            expect(
                done["rows"]["inserted"] == expected["imported"]
                and done["rows"]["updated"] == 0
                and done["rows"]["unchanged"] == 0
                and done["source_bytes"]["consumed"] == len(journey.export) == done["source_bytes"]["total"],
                "the resumed import did not insert each kept row once and consume the whole source",
                done,
            )
            # Status lists the first twenty rejections by source position (0-based).
            invalid = [position for position, row in enumerate(journey.rows) if not _is_valid(row)]
            expect(
                [rejection["row"] for rejection in done["rejections"]] == invalid[:20],
                "the import did not reject the export's invalid rows",
                done["rejections"],
            )
            journey.measurements.setdefault("import_memory", []).append(memory_window(
                service, baseline, scope="before installed entry point through resumed import completion",
                progress=done["rows"]))
        with journey.step("4. query and compare with the independent reference"):
            await compare_queries(journey, service)
        with journey.step("7. file collections A and B unchanged"):
            await file_collections_unchanged(journey, service, "after the journey")
    expect(
        hashlib.sha256((journey.vault / journey.export_path).read_bytes()).hexdigest()
        == hashlib.sha256(journey.export).hexdigest(),
        "the preserved export changed during the import",
    )


async def paged(service: Session, query: dict[str, Any], key: str, size: int | None,
                collection: str = WORKOUTS_ID) -> tuple[list, list]:
    """Every row or group of a query across its cursor pages, with each page's plan."""
    items, plans, after = [], [], None
    while True:
        # Row acceptance uses the product's default page instead of its maximum.
        paging = {**({"limit": size} if size is not None else {}), **({"after": after} if after else {})}
        page = await service.query({**query, "page": paging}, collection)
        items.extend(page[key])
        plans.append(page.get("plan"))
        after = page.get("next_cursor")
        if not page["has_more"]:
            return items, plans
        expect(after, "a query page reported more rows without a cursor", page)


async def compare_queries(journey: Journey, service: Session) -> None:
    rows = reference_rows(journey.rows)
    calories = reference_daily(journey.rows, "calories")
    distance = reference_daily(journey.rows, "distance_m")

    async def rolled_up():
        explained = await service.query({**DAILY, "mode": "explain"})
        return explained if explained["plan"]["strategy"] == "rollup" else None

    explained = await until(rolled_up, within=60, what="the daily rollup to be ready")
    expect(explained["plan"]["reason"] == "rollup_ready", "the rollup is not ready", explained["plan"])

    groups, plans = await paged(service, DAILY, "groups", 30)
    expect(
        all(plan and plan["strategy"] == "rollup" for plan in plans),
        "the daily query was not answered from the rollup",
        plans,
    )
    answered = {group["local_date"]: group for group in groups}
    expect(len(answered) == len(groups), "a local day came back in two groups", groups)
    expect(
        set(answered) == set(calories),
        "the answered days differ from the reference days",
        {"missing": sorted(set(calories) - set(answered)), "extra": sorted(set(answered) - set(calories))},
    )
    for day, expected in calories.items():
        group = answered[day]
        expect(
            group["calories_count"] == expected["count"]
            and group["calories_sum"] == expected["sum"]
            and math.isclose(group["calories_avg"], expected["avg"], rel_tol=1e-12)
            and group["distance_sum"] == distance[day]["sum"],
            f"daily count, sum or avg differs from the reference on {day}",
            {"answered": group, "expected": expected, "expected_distance": distance[day]},
        )
    total = sum(group["calories_count"] for group in groups)
    expect(total == len(rows), "the daily counts do not add up to the kept rows", [total, len(rows)])

    scan, _ = await paged(
        service,
        {"version": 1, "select": ROW_FIELDS, "order_by": [{"field": "local_date"}]},
        "rows",
        None,
    )
    ids = [row["exercise_id"] for row in scan]
    expect(len(ids) == len(set(ids)), "an exercise_id came back twice: the import duplicated a row")
    expect(set(ids) == set(rows), "the stored rows differ from the kept rows of the export")
    for row in scan:
        source = rows[row["exercise_id"]]
        expect(
            row["calories"] == source["metrics"]["calories"]
            and row["local_date"] == _local_day(source).isoformat(),
            f"stored row {row['exercise_id']} differs from the export's last valid occurrence",
            {"stored": row, "source": source},
        )

    day = "2026-03-11"  # holds an instant whose own offset moved it from UTC's 10 March
    on_day = {"version": 1, "select": ROW_FIELDS, "where": {"field": "local_date", "op": "eq", "value": day}}
    found, _ = await paged(service, on_day, "rows", None)
    expected_ids = {key for key, row in rows.items() if _local_day(row).isoformat() == day}
    expect(
        {row["exercise_id"] for row in found} == expected_ids and "wk-edge-0" in expected_ids,
        f"the row query for {day} differs from the reference",
        {"found": sorted(row["exercise_id"] for row in found), "expected": sorted(expected_ids)},
    )
    plan = (await service.query({**on_day, "mode": "explain"}))["plan"]
    expect(
        plan["access"] == "index" and plan["index"] == "field:local_date",
        "the one-day row query did not seek the declared local_date index",
        plan,
    )


async def field_release_journey(journey: Journey) -> None:
    """Step 5: one live recipient keeps metrics across the owner's release and revocation."""
    import yaml

    with journey.step("5. recipient metrics, owner field release and live revocation"):
        async with journey.recipient_service() as (base_url, recipient):
            owner = RestOwner(base_url, journey)
            await recipient.call("bootstrap")
            await samples_ready(journey, recipient)
            await compare_samples(journey, recipient)
            await protected_members(journey, owner, recipient)
            await compare_queries(journey, recipient)
            await file_collections_unchanged(journey, recipient, "before the field release")
            # S1 fixes this managed summary page name; this is the fixture's declared Items source.
            summary_path = (Path(WORKOUTS_PATH).parent / "Items" / "_summary.md").as_posix()

            async def shared_summary() -> dict[str, Any]:
                page = await recipient.call("read_memory", path=summary_path)
                metadata = page["frontmatter"]
                expect(metadata["metric"] == "released rows"
                       and metadata["value"] == len(reference_rows(journey.rows))
                       and metadata["window"] == "all released rows"
                       and metadata["source"] == "collection store"
                       and metadata["completeness"] == "complete at basis"
                       and set(metadata["basis"]) == {"released_rows"}
                       and metadata["basis"]["released_rows"]
                       and "exomem_view" not in metadata,
                       "the shared summary did not report its admitted count and basis", page)
                return page

            summary_before = await shared_summary()
            default = await recipient.query({"version": 1, "page": {"limit": 3}})
            expect(default["rows"], "default projection returned no admitted metrics", default)
            withheld = {"route", "place", "place_label", "extra_context"}
            for row in default["rows"]:
                expect(not withheld.intersection(row), "default projection disclosed a withheld field", row)
                source = reference_rows(journey.rows)[row["exercise_id"]]
                expect(row["calories"] == source["metrics"]["calories"],
                       "default projection changed the admitted metric", row)
            for field in ("route", "route.fixes", "place", "place.label", "place_label", "extra_context"):
                await recipient.refused("QUERY_FIELD_UNAVAILABLE", "record_memory", action="query",
                                        collection=WORKOUTS_ID, query={"version": 1, "select": [field]})
            for field in ("place_label", "absent_location_field"):
                for request in (
                    {"select": [field], "mode": "explain"},
                    {"select": ROW_FIELDS, "where": {"field": field, "op": "eq", "value": "Invented place 0"}},
                    {"select": ROW_FIELDS, "order_by": [{"field": field}]},
                    {"group_by": [{"field": field}], "aggregates": {"n": {"op": "count"}}},
                    {"aggregates": {"n": {"op": "count", "field": field}}},
                ):
                    await recipient.refused("QUERY_FIELD_UNAVAILABLE", "record_memory", action="query",
                                            collection=WORKOUTS_ID, query={"version": 1, **request})
            original = await owner.call("read_memory", path=journey.export_path)
            expect(original["body"].encode("utf-8") == journey.export
                   and original["content_hash"] == hashlib.sha256(journey.export).hexdigest(),
                   "the authenticated owner could not read the exact preserved original")

            async def raw_private() -> None:
                missing_path = journey.export_path + ".absent"
                expect(not (journey.vault / missing_path).exists(), "the missing RAW probe exists")
                messages = []
                for path in (journey.export_path, missing_path):
                    try:
                        await recipient.call("read_memory", path=path)
                    except Refused as refusal:
                        # The terminal RAW gate serves this fixed opaque token on MCP, including errors.
                        message = refusal.message.removeprefix("Error calling tool 'read_memory': ")
                        expect(refusal.code == "UNKNOWN" and message == "[withheld]",
                               "the RAW read did not return its opaque refusal", refusal.message)
                        messages.append(refusal.message)
                    else:
                        raise RuntimeError("the recipient read a protected or absent RAW original")
                expect(messages[0] == messages[1], "the RAW refusal disclosed existence")

            await raw_private()
            metric = {"version": 1, "select": ROW_FIELDS,
                      "order_by": [{"field": "local_date"}], "page": {"limit": 2}}
            first = await recipient.query(metric)
            expect(first["has_more"] and first["next_cursor"], "the metric query has no second page", first)
            second_query = {**metric, "page": {"limit": 2, "after": first["next_cursor"]}}
            second = await recipient.query(second_query)
            location_query = {"version": 1, "select": ["exercise_id", "route", "place", "place_label"],
                              "where": {"field": "exercise_id", "op": "eq", "value": "wk-00000"}}
            expected_location = reference_rows(journey.rows)["wk-00000"]

            def check_location(result: dict[str, Any]) -> None:
                expect(len(result["rows"]) == 1, "the released location query changed its row membership", result)
                row = result["rows"][0]
                expect(row["exercise_id"] == "wk-00000" and row["route"] == expected_location["route"]
                       and row["place"] == expected_location["place"]
                       and row["place_label"] == expected_location["place"]["label"],
                       "released location differs from the independent source", row)

            check_location(await owner.query(location_query))
            inspected = await owner.records(action="inspect", collection=WORKOUTS_ID)
            basis = inspected["field_release_basis"]
            expect(basis["collection_id"] == WORKOUTS_ID and basis["ref"] == basis["path"],
                   "owner discovery returned a different canonical collection", basis)
            issuer_key = hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()
            audience_key = hashlib.sha256(f"{base_url.rstrip('/')}\0{RECIPIENT_ID}".encode()).hexdigest()
            grant_id = "01J000000000000000000000S1"  # Invented immutable ULID in this fresh vault.
            document = {
                "governance_version": 1, "kind": "collection-fields", "id": grant_id,
                "ref": basis["ref"], "path": basis["path"], "content_hash": basis["classification_basis"],
                "to_audience": f"principal:{audience_key}", "released_at": datetime.now(UTC).isoformat(),
                "why": "Release the invented location fields to the authenticated test recipient",
                "field_release": {
                    "version": 1, "store_id": basis["store_id"], "collection_id": basis["collection_id"],
                    "classification_basis": basis["classification_basis"],
                    "paths": [{"path": "route", "subtree": True}, {"path": "place", "subtree": True},
                              {"path": "place_label", "subtree": False}],
                    "surface": "mcp", "issuer_family": f"mcp-oauth:{issuer_key}", "purpose": None,
                    "release_version": 1,
                },
            }
            proposal_args = {
                "documents": {"grants/s1-installed-location.yaml": yaml.safe_dump(document, sort_keys=False)},
                "selector_paths": [basis["path"]], "target_ceiling": 6, "duration": "standing",
                "intent": "Release only the invented route, place and derived label to this MCP recipient",
            }
            await recipient.refused("GOVERNANCE_OWNER_REQUIRED", "govern_memory",
                                    operation="propose", **proposal_args)
            proposal = await owner.mutate("govern_memory", operation="propose", **proposal_args)
            committed = await owner.mutate("govern_memory", operation="commit", proposal_id=proposal["proposal_id"])
            expect(committed["status"] == "committed", "the owner field release did not commit", committed)
            check_location(await recipient.query(location_query))
            expect((await shared_summary())["frontmatter"] == summary_before["frontmatter"],
                   "a field release changed the summary's row membership basis")
            expect((await recipient.query(second_query))["rows"] == second["rows"],
                   "an unrelated location release invalidated or changed the second metric page")
            await raw_private()
            location_page = {"version": 1, "select": ["exercise_id", "place_label"], "page": {"limit": 2}}
            location_first = await recipient.query(location_page)
            expect(location_first["has_more"] and location_first["next_cursor"],
                   "the released location query has no second page", location_first)
            revoked = await owner.mutate("govern_memory", operation="revoke", scope="standing", grant_id=grant_id)
            expect(revoked["status"] == "committed", "the owner field revocation did not commit", revoked)
            expect((await shared_summary())["frontmatter"] == summary_before["frontmatter"],
                   "a field revocation changed the summary's row membership basis")
            await recipient.refused("QUERY_FIELD_UNAVAILABLE", "record_memory", action="query",
                                    collection=WORKOUTS_ID, query=location_query)
            await recipient.refused("QUERY_FIELD_UNAVAILABLE", "record_memory", action="query",
                                    collection=WORKOUTS_ID, query={**location_page,
                                        "page": {"limit": 2, "after": location_first["next_cursor"]}})
            expect((await recipient.query(second_query))["rows"] == second["rows"],
                   "an unrelated location revocation invalidated or changed the second metric page")
            await compare_queries(journey, recipient)
            check_location(await owner.query(location_query))
            owner_summary = await owner.call("read_memory", path=summary_path)
            expect("exomem_view" in owner_summary["frontmatter"],
                   "the owner summary lost its full-state view stamp", owner_summary)
            await raw_private()
            await file_collections_unchanged(journey, recipient, "after the field revocation")


def adopt_fresh(journey: Journey) -> None:
    """Use the owner's public preview/apply route on a copy with no live store."""
    expect(not list(journey.state_root.rglob("collections.sqlite")), "the fresh copy already has a live store")
    preview = journey.owner_cli("adopt-local", "--dry-run", "--why", "adopt the copied fixture")
    expect(preview.returncode == 0, "copy adoption preview refused", preview.stderr)
    sealed = json.loads(preview.stdout.strip().splitlines()[-1])
    expect(not list(journey.state_root.rglob("collections.sqlite")), "preview installed a live store")
    applied = journey.owner_cli("adopt-local", "--preview-id", sealed["preview_id"],
                                "--why", "adopt the copied fixture")
    expect(applied.returncode == 0, "copy adoption refused", applied.stderr)
    result = json.loads(applied.stdout.strip().splitlines()[-1])
    expect(result["status"] == "admitted", "copy adoption did not admit the store", result)


async def copy_and_adopt(journey: Journey) -> Journey:
    """Step 6: fresh-state adoption of a copy of the mixed vault; returns the adopted copy."""
    with journey.step("6. copy the stopped vault and adopt its published replica"):
        copied_root = journey.work / "copied-vault"
        shutil.copytree(journey.vault, copied_root)
        copied = journey.fork("copy", copied_root)
        adopt_fresh(copied)
        async with copied.service("copied") as service:
            await samples_ready(copied, service)
            await compare_samples(copied, service)
            await compare_queries(copied, service)
            await file_collections_unchanged(copied, service, "after fresh-root copy")
    return copied


async def unsupported_launcher(journey: Journey) -> None:
    """The store fence must refuse an actual older reader without a RAW fence masking it."""
    from exomem.governance import raw_protection

    with journey.step("6. refuse an actual unsupported interpreter through the launcher"):
        expect(journey.older_python, "the launcher proof needs --older-python")
        fixture = journey.fork("older-reader", journey.work / "older-reader-vault")
        # Exclude RAW-marked paths only in this refusal copy, so RAW cannot mask the store fence.
        shutil.copytree(
            journey.vault, fixture.vault,
            ignore=lambda _directory, names: [name for name in names if raw_protection.marked(name)],
        )
        result = fixture.installed_helper("--unsupported-launcher", fixture.vault,
                                          older_python=journey.older_python)
        expect(result["store_fence_refused"] is True, "the unsupported launcher accepted the copy", result)


def snapshot_state(journey: Journey, label: str) -> dict[str, Any]:
    target = journey.work / f"{label}.sqlite"
    backup = journey.owner_cli("backup", "--to", str(target))
    expect(backup.returncode == 0, "the integrity-checked backup refused", backup.stderr)
    return journey.installed_helper("--snapshot-state", target)


async def narrowing_rollback(journey: Journey) -> None:
    """Disable this vault through its public route, then prove retained data and paused work."""
    with journey.step("6. narrow the vault capability and keep A/B usable"):
        async with journey.service("before-rollback") as service:
            await discover_and_plan(journey, service)
            # Identical starts return the completed job; a distinct source gives rollback live work to stop.
            stoppable_export = journey.export + b"\n"
            receipt = await service.mutate(
                "preserve_evidence", scope=EXPORT_SCOPE, category=EXPORT_CATEGORY,
                filename="rollback-workouts.ndjson", content=stoppable_export.decode("utf-8"),
                description="invented export for the interrupted rollback job", raw_protection=True,
            )
            expect(receipt.get("state") == "stored"
                   and receipt.get("hash") == hashlib.sha256(stoppable_export).hexdigest(),
                   "rollback source preservation did not retain its exact bytes", receipt)
            pending = await service.import_job(
                WORKOUTS_ID, mode="start", source_ref=receipt["stored_path"], format="ndjson", mapping=MAPPING,
            )
            expect(pending["state"] == "running", "rollback has no active import to stop", pending)
        before = snapshot_state(journey, "before-rollback")
        marker = (journey.vault / STORE_FILES[1]).read_bytes()
        rollback = journey.owner_cli_after_stop("rollback")
        expect(rollback.returncode == 0, "the public vault rollback refused", rollback.stderr)
        expect(json.loads(rollback.stdout.strip().splitlines()[-1])["status"] == "disabled",
               "rollback did not disable the vault", rollback.stdout)
        async with journey.service("after-rollback") as service:
            await service.refused("COLLECTION_STORE_DISABLED", "record_memory", action="query", collection=WORKOUTS_ID)
            await file_collections_unchanged(journey, service, "after vault rollback")
            await service.mutate("record_memory", action="append", collection=journey.file_collections["records"],
                                 item={"occurred_on": "2026-08-05", "title": "After rollback", "status": "completed"},
                                 why="prove file Records stays writable")
            await service.mutate("plan_memory", action="add", collection=journey.file_collections["planning"],
                                 plan_id=str(uuid.uuid4()), item={"title": "After rollback", "tags": ["bug"]},
                                 why="prove file Planning stays writable")
            await asyncio.sleep(1.2)  # Cross a normal background tick while the import remains disabled.
            after = snapshot_state(journey, "after-rollback")
        expect(after == before, "rollback or disabled ticks changed canonical rows, history or import checkpoints",
               {"before": before, "after": after})
        expect((journey.vault / STORE_FILES[1]).read_bytes() == marker, "rollback changed the routing marker")
        expect((journey.vault / journey.export_path).read_bytes() == journey.export, "rollback changed preserved raw bytes")
        expect((journey.vault / STORE_FILES[0]).is_file(), "rollback removed the published replica")


# --- the copy's file collections move into the store (OpenSpec P3.6, the GA gate) ---

#: A file Records row with nested movements, appended before the move so that A has history.
MOVED_RECORDS_ROW = {
    "occurred_on": "2026-08-05", "title": "Pull with bands", "status": "partial",
    "movements": [{"movement": "Row", "band": "red", "repetitions": "10"},
                  {"movement": "Press", "band": "blue", "repetitions": "8"}],
}
#: A row appended and corrected after migration.
NATIVE_RECORDS_ROW = {"occurred_on": "2026-08-06", "title": "Native row", "status": "partial"}
NATIVE_RECORDS_KEY = "5a1f6c0e-3d2b-4b7a-9c18-2e4d7f6a8b90"
#: The wire fields that say how a collection is stored, not what it holds. The store hashes its own
#: rows and renders its own views, so file mode and store mode differ in these by design.
STORAGE_IDENTITY = frozenset({
    "generated_at", "snapshot", "source_versions", "item_version", "rendered", "expected_container_hash",
    "projection",
})
#: Bound for the service's background view work (an item's view file, the start census adopting an edit).
EDIT_BACK_WINDOW = 30.0


def owner_report(result: subprocess.CompletedProcess[str], what: str) -> dict[str, Any]:
    expect(result.returncode == 0, f"{what} failed",
           {"returncode": result.returncode, "stdout": result.stdout[-1500:], "stderr": result.stderr[-1500:]})
    return json.loads(result.stdout.strip().splitlines()[-1])


def marker_entries(journey: Journey) -> dict[str, dict[str, Any]]:
    """The routing marker's collections by manifest path."""
    marker = json.loads((journey.vault / STORE_FILES[1]).read_text(encoding="utf-8"))
    return {entry["manifest_path"]: entry for entry in marker["collections"]}


def without_storage_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_storage_identity(item) for key, item in value.items() if key not in STORAGE_IDENTITY}
    if isinstance(value, list):
        return [without_storage_identity(item) for item in value]
    return value


async def file_answers(journey: Journey, service: Session) -> dict[str, Any]:
    """What the service answers about A and B: the full Records inspection, two Records queries,
    and a Planning query and view."""
    records, planning = journey.file_collections["records"], journey.file_collections["planning"]
    return {
        "records inspect": await service.records(action="inspect", collection=records),
        "records columns": await service.records(
            action="query", collection=records, columns=["occurred_on", "title", "status", "movements"], limit=20),
        "records view": await service.records(action="query", collection=records, view="completed-sessions"),
        "planning query": await service.call("plan_memory", action="query", collection=planning,
                                             limit=20, lifecycle="all"),
        "planning view": await service.call("plan_memory", action="query", collection=planning, view="year"),
    }


async def store_state(journey: Journey, service: Session) -> dict[str, Any]:
    """What a restart must keep: A's rows, title and agent history, B's priorities and the summary."""
    records, planning = journey.file_collections["records"], journey.file_collections["planning"]
    rows = await service.records(action="query", collection=records, columns=["occurred_on", "title", "status"],
                                 limit=20)
    inspected = await service.records(action="inspect", collection=records)
    history = await service.records(action="query", collection=records, view="completed-sessions",
                                    include_agent_history=True)
    plan = await service.call("plan_memory", action="query", collection=planning, limit=20, lifecycle="all")
    summary, _ = await paged(service, DAILY, "groups", 30)
    return {
        "records": sorted((row["record_id"], row["title"], row["status"]) for row in rows["rows"]),
        "records title": inspected["contract"]["title"],
        "records history": [(event["operation"], event["item_key"]) for event in history["agent_history"]["events"]],
        "records transitions": [event["transition_id"] for event in history["agent_history"]["events"]],
        "planning": sorted((row["plan_id"], row["priority"]) for row in plan["rows"]),
        "summary": summary,
    }


async def migrate_copy(copied: Journey) -> dict[str, Any]:
    """Step 7: the owner moves the adopted copy's file Records and Planning collections into its
    live S1 store, then Records and Planning work in store mode. The copy looks like a real vault:
    a live summary store beside file collections that hold items."""
    records, planning = copied.file_collections["records"], copied.file_collections["planning"]
    async with copied.service("before-migration") as service:
        with copied.step("7. file-mode references for A, B and the summary query"):
            migrated = await service.mutate("record_memory", action="append", collection=records, item=MOVED_RECORDS_ROW,
                                           why="file Records row with nested movements")
            references = await file_answers(copied, service)
            history = await service.records(action="query", collection=records, view="completed-sessions",
                                            include_agent_history=True)
            legacy_transitions = [event["transition_id"] for event in history["agent_history"]["events"]]
            expect(len(legacy_transitions) == 4, "the file reference lacks the create and three appends", history)
            summary_before, _ = await paged(service, DAILY, "groups", 30)
    digests = file_digests(copied)
    routed_before = marker_entries(copied)

    with copied.step("7. owner dry run: migratable, every proof check green"):
        dry = owner_report(copied.owner_cli_after_stop("migrate", "--dry-run"), "migrate --dry-run")
        proved = {row["path"]: row for row in dry["collections"]}
        expect(
            dry["verdict"] == "migratable" and dry["blocked"] == [] and dry["dry_run"] is True
            and dry["coordinator"]["status"] == "supported"
            and set(proved) == {records, planning}
            and all(row["status"] == "ready" and row["proof"] == "passed" and row["legacy_audit_status"] == "ok"
                    for row in proved.values())
            and proved[records]["rows"] == 3 and proved[planning]["rows"] == len(PLAN_ITEMS)
            and dry["store_collections"] == len(routed_before),
            "the dry run did not report the copy migratable with every proof green", dry)
        expect(file_digests(copied) == digests, "the dry run changed the file collections")

    with copied.step("7. owner apply: copy-then-swap into the live store"):
        applied = owner_report(copied.owner_cli_after_stop("migrate", "--apply"), "migrate --apply")
        expect(applied.get("status") == "marker_admitted", "the migration did not admit the marker", applied)
        routed = marker_entries(copied)
        expect(
            {path for path, entry in routed.items() if entry["authority"] == "store"}
            == set(routed_before) | {records, planning}
            and len({entry["store_id"] for entry in routed.values()}) == 1,
            "the marker does not route A and B to the one store beside the summary collections", routed)
        expect(file_digests(copied) == digests, "the migration rewrote the file collections")
        after = owner_report(copied.owner_cli_after_stop("migrate", "--dry-run"), "migrate --dry-run after the move")
        expect(after["verdict"] == "nothing_to_migrate" and after["store_collections"] == len(routed),
               "the preflight does not report A and B as store collections", after)

    async with copied.service("migrated") as service:
        await same_answers(copied, service, references, summary_before)
        imported = await store_mode_writes(copied, service, summary_before, migrated, legacy_transitions)
    with copied.step("7. owner edits a migrated item's view while the service is stopped"):
        view = copied.vault / imported["path"]
        text = view.read_text(encoding="utf-8")
        edited = text.replace(f"status: {MOVED_RECORDS_ROW['status']}", "status: aborted", 1)
        expect(edited != text, "the item view has no status line to edit", text)
        view.write_text(edited, encoding="utf-8")
    async with copied.service("view-edited") as service:
        settled = await view_edit_adopted(copied, service, imported, legacy_transitions)
    async with copied.service("restarted") as service:
        with copied.step("7. a further restart keeps every committed change"):
            expect(await store_state(copied, service) == settled,
                   "a restart lost or changed a committed change", settled)
            expect(marker_entries(copied) == routed, "a restart changed the routing marker")
    return settled


async def compatible_rollback(copied: Journey, settled: dict[str, Any]) -> dict[str, Any]:
    """Run the earlier wheel on the same migrated state, keeping SQLite and its original archives."""
    if copied.compatible_python is None:
        return {"status": "unverified", "reason": "no compatible application supplied"}
    with copied.step("7. compatible application rollback keeps SQLite authority and archives"):
        admission = copied.installed_helper("--compatible-launcher", copied.vault,
                                           compatible_python=str(copied.compatible_python))
        before = snapshot_state(copied, "before-compatible-rollback")
        expect(before["legacy_originals"]["rows"] > 0, "the migrated fixture has no original-file archive")
        expect(before["store"]["store_id"] == admission["fence"]["store_id"],
               "the admitted fence names another store")
        routed = marker_entries(copied)
        records, planning = copied.file_collections["records"], copied.file_collections["planning"]
        async with copied.service("compatible", python=copied.compatible_python) as service:
            expect(await store_state(copied, service) == settled, "the earlier application changed existing answers")
            rows = await service.records(action="query", collection=records,
                                         columns=["occurred_on", "title", "status"], limit=20)
            row = next(row for row in rows["rows"] if row["record_id"] == NATIVE_RECORDS_KEY)
            title = row["title"] + " (rollback)"
            changed = await service.mutate(
                "record_memory", action="update", collection=records, item_key=NATIVE_RECORDS_KEY,
                changes={"title": title}, expected_container_hash=rows["snapshot"],
                expected_item_version=row["item_version"], why="verify the compatible application write")
            plan = await service.call("plan_memory", action="query", collection=planning, limit=20, lifecycle="all")
            item = next(key for key, values in PLAN_ITEMS.items() if "priority" in values)
            row = next(row for row in plan["rows"] if row["plan_id"] == item)
            priority = PLAN_ITEMS[item]["priority"]
            expect(row["priority"] != priority, "the rollback fixture has no distinct Planning update")
            await service.mutate(
                "plan_memory", action="update", collection=planning, plan_id=item, changes={"priority": priority},
                expected_container_hash=plan["snapshot"], expected_item_version=row["item_version"],
                why="verify the compatible application Planning write")
            expected = {**settled,
                "records": [(key, title if key == NATIVE_RECORDS_KEY else value, status)
                            for key, value, status in settled["records"]],
                "planning": [(key, priority if key == item else value) for key, value in settled["planning"]],
                "records history": [("update", NATIVE_RECORDS_KEY), *settled["records history"]],
                "records transitions": [changed["audit_correlation"], *settled["records transitions"]]}

            async def retained() -> bool:
                return await store_state(copied, service) == expected

            await until(retained, within=EDIT_BACK_WINDOW, what="both compatible writes and their published history")
        async with copied.service("compatible-restarted", python=copied.compatible_python) as service:
            expect(await store_state(copied, service) == expected, "the earlier application lost changes after restart")
        after = snapshot_state(copied, "after-compatible-rollback")
        expect(after["legacy_originals"] == before["legacy_originals"], "the earlier application changed exact archives")
        expect(after["store"] == before["store"] and marker_entries(copied) == routed,
               "the earlier application changed SQLite authority")
        verified = copied.installed_helper("--compatible-launcher", copied.vault,
                                          compatible_python=str(copied.compatible_python))
        expect(verified == admission, "the earlier application, its admission or its fence changed")
    return {"status": "passed", "application": admission, "archives": after["legacy_originals"],
            "release_origin": "unverified; the caller must bind the wheel digest to the official release"}


async def same_answers(journey: Journey, service: Session, references: dict[str, Any], summary_before: list) -> None:
    import yaml

    with journey.step("7. store mode answers as file mode did"):
        answers = await file_answers(journey, service)
        path = journey.file_collections["records"]
        declared = yaml.safe_load((journey.vault / path).read_text().split("---\n")[1])
        answers["records inspect"] = shared_inspection(
            answers["records inspect"], store_id=marker_entries(journey)[path]["store_id"],
            fields=list(declared["item_schema"]["fields"]))
        for name, reference in references.items():
            expect(without_storage_identity(answers[name]) == without_storage_identity(reference),
                   f"{name} differs between file mode and store mode", {"file": reference, "store": answers[name]})
        expect(answers["records inspect"].get("projection") is not None,
               "Records inspection does not report the store's view projection", answers["records inspect"])
        summary, _ = await paged(service, DAILY, "groups", 30)
        expect(summary == summary_before, "the summary collection answers differently after the move")


def shared_inspection(inspection: dict[str, Any], *, store_id: str, fields: list[str]) -> dict[str, Any]:
    """Check store-only owner discovery separately from the shared collection answers."""
    basis, contract = inspection.get("field_release_basis"), inspection["contract"]
    expect(isinstance(basis, dict) and basis.get("version") == 1
           and basis.get("store_id") == store_id and basis.get("collection_id") == contract["collection_id"]
           and basis.get("path") == basis.get("ref") == contract["path"] and basis.get("fields") == fields,
           "inspection returned a different canonical field-release basis", basis)
    return {key: value for key, value in inspection.items() if key != "field_release_basis"}


async def store_mode_writes(journey: Journey, service: Session, summary_before: list,
                            migrated: dict[str, Any], legacy_transitions: list[str]) -> dict[str, Any]:
    """Guarded Records append, update and manifest revise, a Planning update, and the history that
    shows them. Returns a migrated item's key and its stamped view path."""
    records, planning = journey.file_collections["records"], journey.file_collections["planning"]
    with journey.step("7. Records in store mode: guarded append, update and manifest revise"):
        guards = (await service.records(action="inspect", collection=records))["lifecycle_guards"]
        appended = await service.mutate(
            "record_memory", action="append", collection=records, item=NATIVE_RECORDS_ROW,
            item_key=NATIVE_RECORDS_KEY, expected_container_hash=guards["expected_container_hash"],
            why="Records row written in store mode")
        expect(appended["operation"] == "append" and appended["item_key"] == NATIVE_RECORDS_KEY,
               "the store-mode append did not commit the keyed row", appended)
        (path,) = appended["affected_paths"]
        rows = await service.records(action="query", collection=records,
                                     columns=["occurred_on", "title", "status"], limit=20)
        row = next(row for row in rows["rows"] if row["record_id"] == NATIVE_RECORDS_KEY)
        updated = await service.mutate(
            "record_memory", action="update", collection=records, item_key=NATIVE_RECORDS_KEY,
            changes={"title": "Native row corrected"}, expected_container_hash=rows["snapshot"],
            expected_item_version=row["item_version"], why="Records correction written in store mode")
        expect(updated["operation"] == "update", "the store-mode update did not commit", updated)
        inspected = await service.records(action="inspect", collection=records)
        title = inspected["contract"]["title"]
        manifest = (journey.vault / records).read_text(encoding="utf-8")
        expect(f"title: {title}\n" in manifest, "the migrated manifest does not hold the contract title", title)
        proposal = manifest.replace(f"title: {title}\n", f"title: {title} (revised)\n", 1)
        validated = await service.records(action="validate", collection=records, manifest_text=proposal)
        expect(validated["valid"] is True, "the store-mode revision did not validate", validated)
        revised = await service.mutate(
            "record_memory", action="revise", collection=records,
            manifest_text=proposal, **validated["lifecycle_guards"], why="manifest revised in store mode")
        expect(revised["operation"] == "revise", "the store-mode manifest revise did not commit", revised)
        state = await store_state(journey, service)
        expect(state["records title"] == f"{title} (revised)", "the revised manifest is not what inspect reports", state)
        expect((NATIVE_RECORDS_KEY, "Native row corrected", "partial") in state["records"],
               "the store-mode update is not in the Records query", state)
        expect(state["summary"] == summary_before, "store-mode writes changed the summary collection's answer")
        expected = [revised["audit_correlation"], updated["audit_correlation"], appended["audit_correlation"],
                    *legacy_transitions]
        history = {}

        async def history_published() -> bool:
            nonlocal history
            history = await service.records(action="query", collection=records, view="completed-sessions",
                                            include_agent_history=True)
            return [event["transition_id"] for event in history["agent_history"]["events"]] == expected

        # Egress releases an event's path only after its asynchronous view publishes.
        try:
            await until(history_published, within=EDIT_BACK_WINDOW, what="the native and exact imported history")
        except RuntimeError:
            expect(False, "history lacks the native entries or exact imported events", history)

    with journey.step("7. Planning in store mode: one update"):
        item = "b4596ce9-10fd-4856-a26e-89d8be72b0db"
        plan = await service.call("plan_memory", action="query", collection=planning, limit=20, lifecycle="all")
        row = next(row for row in plan["rows"] if row["plan_id"] == item)
        updated = await service.mutate(
            "plan_memory", action="update", collection=planning, plan_id=item, changes={"priority": "low"},
            expected_container_hash=plan["snapshot"], expected_item_version=row["item_version"],
            why="Planning update written in store mode")
        expect(updated["operation"] == "update", "the store-mode Planning update did not commit", updated)
        after = await service.call("plan_memory", action="query", collection=planning, limit=20, lifecycle="all")
        expect({row["plan_id"]: row["priority"] for row in after["rows"]}[item] == "low",
               "the Planning update is not in the query", after)

    with journey.step("7. the appended item's view file appears"):
        async def written() -> bool:
            view = journey.vault / path
            return view.is_file() and "title: Native row corrected" in view.read_text(encoding="utf-8")

        await until(written, within=EDIT_BACK_WINDOW, what="the appended item's view file with its update")
    with journey.step("7. the migrated item's view receives its first stamp"):
        (migrated_path,) = migrated["affected_paths"]

        async def stamped() -> bool:
            return "exomem_view:" in (journey.vault / migrated_path).read_text(encoding="utf-8")

        await until(stamped, within=EDIT_BACK_WINDOW, what="the migrated item's stamped view")
    return {"key": migrated["item_key"], "path": migrated_path, "title": MOVED_RECORDS_ROW["title"]}


async def view_edit_adopted(journey: Journey, service: Session, imported: dict[str, Any],
                           legacy_transitions: list[str]) -> dict[str, Any]:
    """The owner's edit of the view file becomes a governed update after the start census."""
    records = journey.file_collections["records"]
    with journey.step("7. the start census adopts the owner's view edit as a governed update"):
        async def adopted() -> dict[str, Any] | None:
            state = await store_state(journey, service)
            return state if (imported["key"], imported["title"], "aborted") in state["records"] else None

        state = await until(adopted, within=EDIT_BACK_WINDOW, what="the owner's view edit as a governed update")
        operations = [operation for operation, _ in state["records history"]]
        expect(operations[:4] == ["update", "revise", "update", "append"]
               and state["records transitions"][4:] == legacy_transitions,
               "history does not show the adopted edit and exact imported events", state)
        held = (await service.records(action="inspect", collection=records))["projection"]
        expect(held["held_view_corrections"] == 0, "the owner's edit was held instead of applied", held)
    return state


# --- entry point --------------------------------------------------------------


def inspect_unsupported_launcher(args: argparse.Namespace) -> int:
    """Probe the real older installation through production candidate admission."""
    from exomem.collection_store import authority, schema
    from exomem.governance import raw_protection
    from exomem.service_manager import WorkerRuntime
    from exomem.state_migration import COLLECTION_MARKER_COMPATIBILITY_ID

    root = args.unsupported_launcher.resolve()
    expect(not raw_protection.required_compatibility(root), "RAW would mask the store fence in this fixture")
    expect(authority.required_state_compatibility_ids(root) == frozenset({
        "collections-store-v1", COLLECTION_MARKER_COMPATIBILITY_ID, *schema.compatibility_ids(),
    }), "the compatibility fixture does not require the current store, marker and schema formats")
    os.environ["EXOMEM_VAULT_PATH"] = str(root)
    fresh_state = Path(args.work) / "unsupported-state"
    os.environ["EXOMEM_STATE_ROOT"] = str(fresh_state)
    old = loop._run([args.older_python, "-I", "-c",
                     "from importlib.metadata import version; print(version('exomem'))"],
                    env=os.environ.copy(), cwd=Path(args.work), timeout=20).stdout.strip()
    runtime = WorkerRuntime(Path(args.work) / "unsupported.sock", host="127.0.0.1", port=0)
    target = asyncio.run(runtime.inspect({"python": args.older_python, "version": old}))
    try:
        runtime.migration_required(target)
    except ValueError as error:
        expect(str(error) == "candidate does not support required state compatibility",
               "the launcher refused for another reason", str(error))
    else:
        raise RuntimeError("the launcher admitted the unsupported store reader")
    expect(runtime.child is None and runtime.standby is None, "rejected candidate started a worker")
    expect(not fresh_state.exists(), "rejected candidate created external state")
    print(json.dumps({"store_fence_refused": True, "older_version": old}))
    return 0


def inspect_snapshot_state(path: Path) -> int:
    """Compare business state and job checkpoints in private, integrity-checked backup files."""
    from contextlib import closing

    from exomem.collection_store import legacy_originals, schema
    from exomem.collection_store.connection import open_reader

    with closing(open_reader(path)) as reader:
        expect(reader.execute("PRAGMA integrity_check").fetchone() == ("ok",), "the backup fails integrity")
        # These are fixed schema tables, not customer-defined fields or vocabularies.
        tables = {"items": "row_id", "txns": "commit_seq", "collection_manifests": "collection_id,manifest_version",
                  "import_jobs": "job_id"}
        proof = {}
        for table, order in tables.items():
            rows = reader.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            encoded = json.dumps(rows, separators=(",", ":"), default=repr).encode()
            proof[table] = {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
        metadata = legacy_originals.metadata(reader)
        proof["store"] = {"store_id": metadata[schema.META_STORE_ID],
                          "schema_version": metadata[schema.META_SCHEMA_VERSION]}
        archive_hash, count = hashlib.sha256(), 0
        for row in reader.execute("SELECT key,value FROM store_meta WHERE substr(key,1,?)=? ORDER BY key",
                                  (len(legacy_originals.PREFIX), legacy_originals.PREFIX)):
            encoded = json.dumps(row, separators=(",", ":")).encode()
            archive_hash.update(len(encoded).to_bytes(8, "big"))
            archive_hash.update(encoded)
            count += 1
        proof["legacy_originals"] = {"rows": count, "sha256": archive_hash.hexdigest()}
    print(json.dumps(proof))
    return 0


def inspect_compatible_launcher(args: argparse.Namespace) -> int:
    """Bind rollback behavior to an intact wheel and assess the existing managed admission boundary."""
    from dataclasses import asdict
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    from exomem import service_upgrade, writer_lease
    from exomem.service_manager import WorkerRuntime

    python = Path(args.compatible_python).expanduser().absolute()
    identity = service_upgrade._staged_identity(python, wheel=True)
    source = identity.get("direct_url")
    # The qualification uses wheel provenance so the maintained verifier binds the installed bytes.
    expect(isinstance(source, dict) and isinstance(source.get("url"), str), "the compatible wheel has no provenance")
    location = urlsplit(source["url"])
    expect(location.scheme == "file" and not location.netloc, "the compatible wheel snapshot is not local")
    wheel = Path(url2pathname(location.path))
    digest = service_upgrade._sha256_file(wheel)
    service_upgrade._verify_wheel_install(identity, wheel, digest, identity["version"])
    expect(service_upgrade._sha256_file(wheel) == digest, "the compatible wheel changed during verification")
    runtime = WorkerRuntime(Path(args.work) / "compatible.sock", host="127.0.0.1", port=0)
    target = asyncio.run(runtime.inspect({"python": str(python), "version": identity["version"]}))
    migrate, reason = runtime.migration_required(target)
    expect(not migrate, "the earlier application requires a state migration", reason)
    expect(runtime._collections_declared(target) == "none", "the supplied application declares general migration")
    expect(runtime.child is None and runtime.standby is None, "candidate inspection started an application")
    fence = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env()).collection_store_fence()
    expect(fence.enrolled, "the compatible rollback retired the store fence")
    print(json.dumps({"version": identity["version"], "python": str(python), "wheel_sha256": digest,
                      "installed_record_hashes": len(identity["record_hashes"]), "fence": asdict(fence)}))
    return 0


def issue_recipient_session(path: Path) -> int:
    """Seed synthetic login identity through the installed durable session authority."""
    from exomem.auth_sessions import SessionIdentity
    from exomem.server_auth import build_session_authority

    async def issue() -> str:
        authority = build_session_authority(base_url=os.environ["EXOMEM_BASE_URL"])
        bearer, _record = await authority.issue(
            client_id="s1-recipient", scopes=["exomem:read", "exomem:write"],
            identity=SessionIdentity(github_user_id=RECIPIENT_ID, github_login="s1-recipient"),
        )
        return bearer

    bearer = asyncio.run(issue())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(bearer)
    return 0


def start_coordinator(journey: Journey) -> tuple[subprocess.Popen, Any]:
    """The writer-lease coordinator, a real local HTTP process of the installed wheel."""
    (reservation,) = loop._reserve_port_reservations(1)
    port = int(reservation.getsockname()[1])
    journey.coordinator_url = f"http://127.0.0.1:{port}"
    env = loop._clean_env(journey.home, journey.vault)
    env["EXOMEM_LEASE_COORDINATOR_TOKEN"] = LEASE_TOKEN
    env["EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"] = OPERATOR_TOKEN
    log_path = journey.work / "coordinator.log"
    handle = log_path.open("w", encoding="utf-8")
    reservation.close()
    process = subprocess.Popen(
        [
            str(journey.python), "-m", "exomem.lease_coordinator",
            "--host", "127.0.0.1", "--port", str(port),
            "--database", str(journey.work / "writer-leases.sqlite"),
        ],  # fmt: skip
        cwd=journey.work,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    loop._wait_http_ready(
        lambda: loop._http_json(
            f"{journey.coordinator_url}/v1/vaults/{LEASE_VAULT_ID}/lease",
            token=LEASE_TOKEN,
            timeout=1.0,
        )[0]
        == 200,
        proc=process,
        deadline=time.monotonic() + journey.timeout,
        log=log_path,
        label="lease coordinator",
    )
    return process, handle


def print_log_tails(work: Path, chars: int = 1500, *, observer_log_root: Path | None = None) -> None:
    """The services' own logs, which the scratch root deletes with everything else."""
    from _recipient_query_observer import LOG_NAME

    observer_logs = observer_log_root if observer_log_root is not None else work
    for log in sorted(observer_logs.rglob(LOG_NAME)):
        with contextlib.suppress(OSError):
            record = log.read_text(encoding="utf-8")
            print(f"--- s1-journey {log.relative_to(observer_logs)} (complete aggregate) ---\n{record}", file=sys.stderr)
    # Forked services keep their captured output below this work root.
    for log in sorted(work.rglob("*.log")):
        with contextlib.suppress(OSError):
            tail = log.read_text(encoding="utf-8", errors="replace")[-chars:]
            print(f"--- s1-journey {log.relative_to(work)} (last {chars} chars) ---\n{tail}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    journey = Journey(args)
    expect(journey.initial_records_summary_enabled, "the installed release does not enable summary collections")
    capability_file = installed_capability_file(journey)
    capability_before = capability_file.read_bytes()
    journey.measurements["installed_capability"] = {
        "initially_enabled": journey.initial_records_summary_enabled,
        "initial_sha256": hashlib.sha256(capability_before).hexdigest(),
    }
    with journey.step("setup: init the vault, start the lease coordinator"):
        loop._run(
            [str(journey.executable), "init", "--vault", str(journey.vault)],
            env=loop._clean_env(journey.home, journey.vault),
            cwd=journey.work,
            timeout=60,
        )
        coordinator, handle = start_coordinator(journey)
    try:
        asyncio.run(unenrolled_phase(journey))
        asyncio.run(released_phase_enrol(journey))
        continuation, imported = asyncio.run(create_and_start_import(journey))
        asyncio.run(resume_import(journey, continuation, imported))
        asyncio.run(import_samples(journey))
        asyncio.run(field_release_journey(journey))
        copied = asyncio.run(copy_and_adopt(journey))
        with journey.step("7. migrate the adopted copy and prove store-mode workflows"):
            settled = asyncio.run(migrate_copy(copied))
            journey.measurements["compatible_rollback"] = asyncio.run(compatible_rollback(copied, settled))
        asyncio.run(unsupported_launcher(journey))
        asyncio.run(narrowing_rollback(journey))
        capability_after = capability_file.read_bytes()
        expect(capability_after == capability_before, "the journey changed installed capability bytes")
        journey.measurements["installed_capability"]["final_sha256"] = hashlib.sha256(capability_after).hexdigest()
    except BaseException:
        print_log_tails(journey.work, observer_log_root=journey.observer_log_root)
        raise
    finally:
        loop._terminate(coordinator, 10)
        handle.close()
    total = round(time.monotonic() - started, 1)
    print(json.dumps({"success": True, "journey": "s1-summary", "seconds": total,
                      "steps": journey.timings, "measurements": journey.measurements}))
    return 0


def main() -> int:
    if sys.argv[1:2] == ["--observe-query-timeouts"]:
        import runpy

        from _recipient_query_observer import observe_query_timeouts

        log_path = Path(sys.argv[2])
        observation_parser = argparse.ArgumentParser(add_help=False)
        observation_parser.add_argument("--observe-process", nargs=2)
        observed, arguments = observation_parser.parse_known_args(sys.argv[3:])
        sys.argv = ["exomem", *arguments]
        with observe_query_timeouts(log_path):
            if observed.observe_process:
                path, launch = observed.observe_process
                # runpy preserves this address space; PID/starttime cannot detect a future exec replacement.
                record_process_observation(Path(path), launch)
            runpy.run_module("exomem", run_name="__main__", alter_sys=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="the installed wheel's python")
    parser.add_argument("--executable", required=True, help="the installed wheel's exomem")
    parser.add_argument("--older-python", default="", help="the actual unsupported installed reader for the launcher proof")
    parser.add_argument("--compatible-python", default="", help="the earlier wheel installation for compatible SQLite rollback")
    parser.add_argument("--vault", required=True, help="a vault path for this journey; created by init")
    parser.add_argument("--work", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--issue-recipient-session", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--unsupported-launcher", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-state", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--compatible-launcher", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.issue_recipient_session:
        return issue_recipient_session(args.issue_recipient_session)
    if args.unsupported_launcher:
        return inspect_unsupported_launcher(args)
    if args.snapshot_state:
        return inspect_snapshot_state(args.snapshot_state)
    if args.compatible_launcher:
        return inspect_compatible_launcher(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
