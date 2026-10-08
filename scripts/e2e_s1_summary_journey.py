#!/usr/bin/env python
"""Installed-wheel owner journey for the S1 Records summary collection.

OpenSpec add-collection-query-engine task S1.7 (installed MCP owner journey,
independent expected rows and daily results) over the parent change
move-structured-collections-to-sqlite. It runs as one step of the product E2E
(`scripts/e2e_product_loop.py`) on the wheel that run already built and installed.

The journey runs twice on the same installed bytes:

* Dark phase: the wheel as built. `capability.RELEASED` is empty, so creating a
  summary collection refuses `RECORDS_SUMMARY_UNAVAILABLE`, as does the owner's
  offline enrolment, and no import has a store collection to run into.
* Released phase: the one line of `capability.py` in site-packages that holds
  `RELEASED` is rewritten to enable `records-summary-v1`, and the service restarts.
  Those are the bytes the S1.8 release would ship. No environment variable,
  argument or vault state can turn the capability on, so nothing else is set.

All data is invented and generated here from a fixed seed. The expected values
come from `reference_daily` below, written from the raw rows with `datetime`
arithmetic; nothing in it reads a response from the product.

The recipient reads metrics through authenticated MCP. The owner releases and
revokes the invented location fields through REST on the same running writer.
Field release never releases the preserved original.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import hashlib
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

import e2e_product_loop as loop  # noqa: E402

# --- the invented export and its independent reference -------------------------

#: Enough rows for ten import batches, so the service stops well before the job ends.
EXPORT_ROWS = 5000
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
        if index >= 4200:
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

LEASE_VAULT_ID = "s1-journey-vault"
LEASE_TOKEN = "s1-journey-writer-token"
OPERATOR_TOKEN = "s1-journey-operator-token"
LEASE_TTL = 4.0
RECIPIENT_ID = 20261007
CAPABILITY_OFF = "RELEASED: frozenset[str] = frozenset()"
CAPABILITY_ON = "RELEASED: frozenset[str] = frozenset({RECORDS_SUMMARY_V1})"


class Refused(RuntimeError):
    """A tool call the service refused; ``code`` is the product's own refusal code."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool} refused {code}: {message}")
        self.code = code
        self.message = message


def expect(condition: object, message: str, observed: object = None) -> None:
    if not condition:
        detail = "" if observed is None else f"\nobserved: {json.dumps(observed, default=str)[:3000]}"
        raise RuntimeError(f"{message}{detail}")


class Session:
    """One MCP session on a running service."""

    def __init__(self, client, timeout: float) -> None:
        self.client, self.timeout = client, timeout

    async def call(self, tool: str, **arguments: Any) -> Any:
        result = await asyncio.wait_for(
            self.client.call_tool(tool, arguments, raise_on_error=False), timeout=self.timeout
        )
        if getattr(result, "is_error", False):
            text = "".join(getattr(block, "text", "") for block in result.content)
            # This checks the product's fixed refusal-code format, never the meaning of user text.
            code = re.search(r"\b([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)\b", text)
            raise Refused(tool, code.group(1) if code else "UNKNOWN", text)
        data = loop._result_data(result)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("success") is False and isinstance(error, dict):
            raise Refused(tool, str(error.get("code")), str(error.get("message")))
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
        return await self.records(action="query", collection=collection, query=query)

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
            raise Refused(tool, str(error.get("code")), str(error.get("message")))
        expect(status == 200 and payload.get("success") is True,
               f"owner REST {tool} failed", {"status": status, "payload": payload})
        return payload["data"]


class Journey:
    """One run's scratch paths, environments and per-step timings."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.executable, self.python = Path(args.executable), Path(args.python)
        self.older_python = getattr(args, "older_python", "")
        self.vault, self.work, self.home = Path(args.vault), Path(args.work), Path(args.home)
        self.work.mkdir(parents=True, exist_ok=True)
        self.timeout = args.request_timeout
        self.rows = export_rows()
        self.export = export_bytes(self.rows)
        self.timings: dict[str, float] = {}
        self.coordinator_url = ""
        self.lease_vault_id = LEASE_VAULT_ID
        self.replica_id = "service"
        self.state_root = self.home / "state"
        self.lease_state = self.work / "lease-service"
        self.lease_state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file_collections: dict[str, Any] = {}
        self.source_receipt: dict[str, Any] = {}
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
    async def service(self, label: str) -> AsyncIterator[Session]:
        """The installed stdio service on this vault, holding the writer lease while it runs."""
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        log = self.work / f"service-{label}.log"
        client = Client(
            StdioTransport(
                command=str(self.executable),
                args=["--transport", "stdio"],
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
            yield Session(client, self.timeout)

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
            process = subprocess.Popen([
                str(self.python), "-m", "exomem", "--transport", "http",
                "--host", "127.0.0.1", "--port", str(port),
            ], cwd=self.work, env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
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

    def fork(self, label: str, vault: Path) -> Journey:
        child = Journey(argparse.Namespace(
            executable=str(self.executable), python=str(self.python), older_python=self.older_python,
            vault=str(vault), work=str(self.work / label / "work"), home=str(self.work / label / "home"),
            request_timeout=self.timeout,
        ))
        child.coordinator_url = self.coordinator_url
        child.lease_vault_id = f"{LEASE_VAULT_ID}-{label}"
        child.replica_id = label
        child.file_collections = self.file_collections
        child.source_receipt = self.source_receipt
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


def release_records_summary(capability_file: Path) -> None:
    """Rewrite the one `RELEASED` line, as the S1.8 release would, and prove nothing else moved."""
    before = capability_file.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [index for index, line in enumerate(before) if line.rstrip("\r\n") == CAPABILITY_OFF]
    expect(len(hits) == 1, f"{capability_file} must hold exactly one line '{CAPABILITY_OFF}'", hits)
    after = list(before)
    ending = before[hits[0]][len(CAPABILITY_OFF):]
    after[hits[0]] = CAPABILITY_ON + ending
    # Replace the file, never write through it: uv hard-links installed files to its wheel
    # cache, and an in-place write would change the cached wheel for every later install.
    staged = capability_file.with_name(capability_file.name + ".release")
    staged.write_text("".join(after), encoding="utf-8")
    shutil.copymode(capability_file, staged)
    os.replace(staged, capability_file)
    written = capability_file.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = [
        line
        for line in difflib.unified_diff(before, written, n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    expect(
        changed == [f"-{before[hits[0]]}", f"+{CAPABILITY_ON}{ending}"],
        "the released-phase rewrite must change exactly the one RELEASED line",
        changed,
    )


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

UNAVAILABLE = "RECORDS_SUMMARY_UNAVAILABLE"
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


async def dark_phase(journey: Journey) -> None:
    async with journey.service("dark") as service:
        with journey.step("A/B: file Records and Planning collections, created before the store"):
            await create_file_collections(journey, service)
            await file_collections_unchanged(journey, service, "right after creation")
        with journey.step("1. preserve the invented export and check its receipt"):
            await preserve_export(journey, service)
        with journey.step("0. dark phase: create and import refuse"):
            await service.refused(
                UNAVAILABLE,
                "record_memory",
                action="create",
                manifest_path=WORKOUTS_PATH,
                manifest_text=summary_manifest(WORKOUTS_ID, "Workout summaries"),
                why="a NEW summary collection, before the release",
            )
            # With no store in the vault, an import has no store collection to run into. It
            # refuses as the file collection or the absent one, not RECORDS_SUMMARY_UNAVAILABLE,
            # which an enrolled vault under a dark release answers (test_collection_store_service).
            for target, code in (
                (journey.file_collections["records"], "IMPORT_UNAVAILABLE"),
                (WORKOUTS_ID, "COLLECTION_NOT_FOUND"),
            ):
                await service.refused(
                    code,
                    "record_memory",
                    action="import",
                    collection=target,
                    import_request={"mode": "preview", **journey.import_request},
                )
    enrolment = journey.work / "enrolment.md"
    enrolment.write_text(summary_manifest(ENROLMENT_ID, "Enrolment"), encoding="utf-8")
    refused = journey.owner_cli(
        "create", "--manifest-path", ENROLMENT_PATH, "--manifest-file", str(enrolment),
        "--why", "enrol before the release",
    )  # fmt: skip
    expect(
        refused.returncode == 1 and UNAVAILABLE in refused.stderr,
        "the owner's offline create did not refuse in the dark phase",
        {"returncode": refused.returncode, "stderr": refused.stderr[-800:]},
    )
    leftovers = [name for name in STORE_FILES if (journey.vault / name).exists()]
    expect(not leftovers, "the dark phase left store files in the vault", leftovers)


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
    # The rewrite is of the installed wheel only; never of a source checkout.
    expect("site-packages" in path.parts, "capability.py is not an installed-wheel file", found)
    return path


async def released_phase_enrol(journey: Journey) -> None:
    with journey.step("0. released phase: rewrite the RELEASED line in site-packages"):
        release_records_summary(installed_capability_file(journey))
    with journey.step("0. released phase: MCP create on an unenrolled vault refuses"):
        async with journey.service("unenrolled") as service:
            await service.refused(
                ENROLMENT_REQUIRED,
                "record_memory",
                action="create",
                manifest_path=WORKOUTS_PATH,
                manifest_text=summary_manifest(WORKOUTS_ID, "Workout summaries"),
                why="a NEW summary collection on an unenrolled vault",
            )
            await file_collections_unchanged(journey, service, "while the vault is unenrolled")
    with journey.step("2. owner enrols the vault: offline `exomem collections create`"):
        enrolment = journey.work / "enrolment.md"
        deadline = time.monotonic() + LEASE_TTL * 3 + 10
        while True:
            enrolled = journey.owner_cli(
                "create", "--manifest-path", ENROLMENT_PATH, "--manifest-file", str(enrolment),
                "--why", "enrol the vault's collection store",
            )  # fmt: skip
            # The service's lease may outlive its stop by up to one TTL; the refusal says to retry.
            if SERVICE_ACTIVE not in enrolled.stderr or time.monotonic() > deadline:
                break
            time.sleep(0.5)
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
    async with journey.service("import") as service:
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
    return job["continuation"], imported


async def resume_import(journey: Journey, continuation: str, imported_at_stop: int) -> None:
    rows = reference_rows(journey.rows)
    valid_lines = sum(1 for row in journey.rows if _is_valid(row))
    expected = {
        "imported": len(rows),
        "rejected": len(journey.rows) - valid_lines,
        "duplicates": valid_lines - len(rows),
    }
    async with journey.service("resume") as service:
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
        with journey.step("4. query and compare with the independent reference"):
            await compare_queries(journey, service)
        with journey.step("7. file collections A and B unchanged"):
            await file_collections_unchanged(journey, service, "after the journey")
    expect(
        hashlib.sha256((journey.vault / journey.export_path).read_bytes()).hexdigest()
        == hashlib.sha256(journey.export).hexdigest(),
        "the preserved export changed during the import",
    )


async def paged(service: Session, query: dict[str, Any], key: str, size: int) -> tuple[list, list]:
    """Every row or group of a query across its cursor pages, with each page's plan."""
    items, plans, after = [], [], None
    while True:
        page = await service.query({**query, "page": {"limit": size, **({"after": after} if after else {})}})
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
        1000,
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
    found, _ = await paged(service, on_day, "rows", 1000)
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


async def copy_and_restore(journey: Journey) -> None:
    """Step 6: fresh-state adoption and actual staged restore of the mixed vault."""
    with journey.step("6. copy the stopped vault and adopt its published replica"):
        copied_root = journey.work / "copied-vault"
        shutil.copytree(journey.vault, copied_root)
        copied = journey.fork("copy", copied_root)
        adopt_fresh(copied)
        async with copied.service("copied") as service:
            await compare_queries(copied, service)
            await file_collections_unchanged(copied, service, "after fresh-root copy")
    with journey.step("6. export, stage and activate the snapshot restore"):
        parent = journey.work / "restore"
        result = journey.installed_helper("--snapshot-restore", parent)
        expect(result["code"] == "HOSTED_RESTORE_CANDIDATE_READY"
               and result["journal_phase"] == "complete" and result["credential_revision"] > 0,
               "installed staged restore did not finish", result)
        restored = journey.fork("restore-client", parent / "vault")
        restored.state_root = parent / "state" / "vault-state"
        expect((restored.vault / journey.export_path).read_bytes() == journey.export,
               "the restored preservation changed the raw bytes")
        expect((restored.vault / STORE_FILES[1]).read_bytes() == (journey.vault / STORE_FILES[1]).read_bytes(),
               "the staged restore changed the routing marker")
        adopt_fresh(restored)
        async with restored.service("restored") as service:
            await compare_queries(restored, service)
            await file_collections_unchanged(restored, service, "after snapshot restore")


async def unsupported_launcher(journey: Journey) -> None:
    """The store fence must refuse an actual older reader without a RAW fence masking it."""
    with journey.step("6. refuse an actual unsupported interpreter through the launcher"):
        expect(journey.older_python, "the launcher proof needs --older-python")
        fixture = journey.fork("older-reader", journey.work / "older-reader-vault")
        loop._run([str(fixture.executable), "init", "--vault", str(fixture.vault)],
                  env=fixture.lease_env(), cwd=fixture.work, timeout=60)
        async with fixture.service("files") as service:
            await create_file_collections(fixture, service)
        manifest = fixture.work / "summary.md"
        manifest.write_text(summary_manifest(WORKOUTS_ID, "Reader compatibility fixture"), encoding="utf-8")
        created = fixture.owner_cli("create", "--manifest-path", WORKOUTS_PATH,
                                    "--manifest-file", str(manifest), "--why", "create compatibility fixture")
        expect(created.returncode == 0, "compatibility fixture create refused", created.stderr)
        async with fixture.service("summary") as service:
            await service.mutate("record_memory", action="append", collection=WORKOUTS_ID,
                                 item={"exercise_id": "reader-proof", "calories": 7, "local_date": "2026-03-11"},
                                 why="compatibility proof row")
        copied_root = fixture.work / "copied-vault"
        shutil.copytree(fixture.vault, copied_root)
        result = fixture.installed_helper("--unsupported-launcher", copied_root,
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
        rollback = journey.owner_cli("rollback")
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


# --- entry point --------------------------------------------------------------


def snapshot_restore(args: argparse.Namespace) -> int:
    """Run the installed export and candidate restore with real private credential state."""
    from exomem import __version__, hosted_portability, hosted_restore, state_migration
    from exomem.hosted_runtime import HOSTED_PROTOCOL_VERSION
    from exomem.hosted_security import CredentialBundle, HostedSecurityAuthority

    parent = args.snapshot_restore.resolve()
    parent.mkdir(mode=0o700, parents=True)
    exported = hosted_portability.export_quiesced_vault(
        Path(args.vault), parent / "artifacts", context=hosted_portability.PortabilityContext(
            cell_id="s1-source", vault_id="s1-vault", operation_id="s1-export",
            created_at=datetime.now(UTC).isoformat(), operator_authorized=True,
            lifecycle_state="quiesced", routing_stopped=True, active_mutations=0,
            background_writers_stopped=True, reads_allowed=True,
        ),
    )
    with zipfile.ZipFile(exported.archive_path) as archive:
        members = archive.namelist()
        expect(all(any(member.endswith(path) for member in members) for path in STORE_FILES),
               "the archive omitted the routing marker or published replica", members)
        expect(not any(member.endswith(("collections.sqlite-wal", "collections.sqlite-shm")) for member in members),
               "the archive carried live SQLite companions", members)
    secret = secrets.token_urlsafe(32)
    bundle = CredentialBundle({"s1-fixture": secret})

    def security(binding):
        return HostedSecurityAuthority(
            binding.state_root, cell_id=binding.cell_id, vault_id=binding.vault_id,
            expected_uid=binding.runtime_uid, expected_gid=binding.runtime_gid, bundle_loader=lambda: bundle,
        )

    def bootstrap_security(*, binding, active_credential_version, operation_id, request_digest):
        return security(binding).bootstrap(active_version=active_credential_version,
                                          operation_id=operation_id, request_digest=request_digest)

    request = {
        "request_id": "351a1841-716e-45c7-a4f8-6f54654c2b1e", "operation_id": "s1-restore",
        "artifact_reference": exported.artifact_reference, "archive_path": str(exported.archive_path),
        "expected_archive_sha256": exported.archive_sha256,
        "source_cell_id": "s1-source", "source_vault_id": "s1-vault",
        "target_cell_id": "s1-target", "target_vault_id": "s1-vault",
        "target_vault_root": str(parent / "vault"), "target_state_root": str(parent / "state"),
        "target_log_root": str(parent / "logs"), "expected_release": __version__,
        "expected_protocol": HOSTED_PROTOCOL_VERSION, "runtime_uid": os.getuid(), "runtime_gid": os.getgid(),
        "active_credential_version": "s1-fixture", "routing_stopped": True, "workload_stopped": True,
    }
    code, result = hosted_restore.execute_restore_candidate(request, bootstrap_security=bootstrap_security)
    from exomem.hosted_runtime import HostedBindingV2

    binding = HostedBindingV2(
        cell_id="s1-target", vault_id="s1-vault", vault_root=parent / "vault",
        state_root=parent / "state", log_root=parent / "logs", runtime_uid=os.getuid(), runtime_gid=os.getgid(),
    )
    credential = security(binding).authenticate(secret)
    expect(credential is not None and credential.security_revision == result["credential_revision"],
           "the restored credential did not authenticate at the reported revision")
    os.environ["EXOMEM_STATE_ROOT"] = str(parent / "state" / "vault-state")
    os.environ["EXOMEM_HOSTED_STATE_ROOT"] = str(parent / "state")
    state_migration.require_vault_state_ready(parent / "vault")
    print(json.dumps({"code": code, "journal_phase": result["journal_phase"],
                      "credential_revision": credential.security_revision}))
    return 0


def inspect_unsupported_launcher(args: argparse.Namespace) -> int:
    """Probe the real older installation through production candidate admission."""
    from exomem.collection_store import authority
    from exomem.governance import raw_protection
    from exomem.service_manager import WorkerRuntime
    from exomem.state_migration import COLLECTION_MARKER_COMPATIBILITY_ID

    root = args.unsupported_launcher.resolve()
    expect(not raw_protection.required_compatibility(root), "RAW would mask the store fence in this fixture")
    expect(authority.required_state_compatibility_ids(root) == frozenset({
        "collections-store-v1", COLLECTION_MARKER_COMPATIBILITY_ID,
    }), "the compatibility fixture does not require the store and marker formats")
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
    print(json.dumps(proof))
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


def print_log_tails(work: Path, chars: int = 1500) -> None:
    """The services' own logs, which the scratch root deletes with everything else."""
    for log in sorted(work.glob("*.log")):
        with contextlib.suppress(OSError):
            tail = log.read_text(encoding="utf-8", errors="replace")[-chars:]
            print(f"--- s1-journey {log.name} (last {chars} chars) ---\n{tail}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    journey = Journey(args)
    started = time.monotonic()
    with journey.step("setup: init the vault, start the lease coordinator"):
        loop._run(
            [str(journey.executable), "init", "--vault", str(journey.vault)],
            env=loop._clean_env(journey.home, journey.vault),
            cwd=journey.work,
            timeout=60,
        )
        coordinator, handle = start_coordinator(journey)
    try:
        asyncio.run(dark_phase(journey))
        asyncio.run(released_phase_enrol(journey))
        continuation, imported = asyncio.run(create_and_start_import(journey))
        asyncio.run(resume_import(journey, continuation, imported))
        asyncio.run(field_release_journey(journey))
        asyncio.run(copy_and_restore(journey))
        asyncio.run(unsupported_launcher(journey))
        asyncio.run(narrowing_rollback(journey))
    except BaseException:
        print_log_tails(journey.work)
        raise
    finally:
        loop._terminate(coordinator, 10)
        handle.close()
    total = round(time.monotonic() - started, 1)
    print(json.dumps({"success": True, "journey": "s1-summary", "seconds": total, "steps": journey.timings}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="the installed wheel's python")
    parser.add_argument("--executable", required=True, help="the installed wheel's exomem")
    parser.add_argument("--older-python", default="", help="the actual unsupported installed reader for the launcher proof")
    parser.add_argument("--vault", required=True, help="a vault path for this journey; created by init")
    parser.add_argument("--work", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--issue-recipient-session", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-restore", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--unsupported-launcher", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.issue_recipient_session:
        return issue_recipient_session(args.issue_recipient_session)
    if args.snapshot_restore:
        return snapshot_restore(args)
    if args.unsupported_launcher:
        return inspect_unsupported_launcher(args)
    if args.snapshot_state:
        return inspect_snapshot_state(args.snapshot_state)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
