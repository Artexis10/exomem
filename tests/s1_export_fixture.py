"""Invented wearable-style export for the S1 summary-collection journeys.

The real private export's shape is frozen only at S1.0 from its owner-local
preview. Until then S1.3/S1.5/S1.7 use this deterministic stand-in. It carries
the traits the design calls out (add-collection-query-engine design §3, §12):
per-record UTC offsets that move a session across midnight, offset-bearing and
``Z``-plus-offset-field instants, date-only daily summaries, unzoned and absent
time bases that must be flagged rather than guessed, nested GPS routes and place
names classified as location, and numeric values whose int/float subtype and
signed zero must survive storage.

``expected_daily`` is the independent reference. It is written here from the
fixture's own fields with ``datetime`` arithmetic and shares no code with the
product's importer, query compiler or rollups.
"""

from __future__ import annotations

import csv
import io
import json
import random
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction

KINDS = ("running", "walking", "cycling")
PLACES = ("Riverside Park", "Harbour Loop", "Old Town Square", "Hill Trail")
START_DAY = date(2026, 3, 1)


def _offset_text(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def iter_exercises(count: int = 96, seed: int = 20261006) -> Iterator[dict]:
    """Yield ``count`` exercise sessions lazily, so million-row callers stream."""
    rng = random.Random(seed)
    for index in range(count):
        offset = rng.choice((-300, 0, 60, 120, 330))
        utc_start = datetime.combine(START_DAY, datetime.min.time(), UTC) + timedelta(
            hours=index * 7 + rng.randint(0, 5), minutes=rng.choice((0, 15, 30, 45))
        )
        local = utc_start.astimezone(timezone(timedelta(minutes=offset)))
        record: dict = {
            "id": f"ex-{index + 1:06d}",
            "kind": KINDS[index % len(KINDS)],
            "duration_s": rng.randint(900, 5400),
            "metrics": {
                "heart_rate_avg": rng.randint(95, 165),
                # Alternate int and float so 5000 and 5000.0 both occur.
                "distance_m": rng.randint(1, 12) * 1000
                if index % 2
                else float(rng.randint(1, 12) * 1000),
                "calories": rng.randint(120, 900),
                "elevation_gain_m": -0.0 if index % 17 == 0 else rng.randint(0, 300),
            },
            "route": [
                {
                    "lat": round(59.40 + rng.random() / 50, 6),
                    "lon": round(24.70 + rng.random() / 50, 6),
                    "t": step * 60,
                }
                for step in range(3)
            ],
            "place": {
                "name": PLACES[index % len(PLACES)],
                "address": f"{index % 40 + 1} Example Street",
            },
        }
        if index % 23 == 5:
            # Unzoned: flagged, never guessed.
            record["start"] = local.replace(tzinfo=None).isoformat()
        elif index % 29 == 7:
            pass  # no time basis at all: flagged
        elif index % 3 == 0:
            record["start_utc"] = utc_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            record["utc_offset"] = _offset_text(offset)
        else:
            record["start"] = local.isoformat()
        yield record


def daily_summaries(days: int = 10, seed: int = 20261006) -> list[dict]:
    rng = random.Random(seed + 1)
    rows = []
    for day in range(days):
        row = {
            "date": (START_DAY + timedelta(days=day)).isoformat(),
            "steps": rng.randint(2000, 16000),
        }
        row["resting_hr"] = rng.randint(48, 62)
        rows.append(row)
    return rows


def exercises_ndjson(count: int = 96, seed: int = 20261006) -> bytes:
    return b"".join(
        json.dumps(row, sort_keys=True).encode() + b"\n" for row in iter_exercises(count, seed)
    )


def exercises_json_array(count: int = 96, seed: int = 20261006) -> bytes:
    return json.dumps(list(iter_exercises(count, seed)), sort_keys=True).encode()


def daily_csv(days: int = 10, seed: int = 20261006) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["date", "steps", "resting_hr"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(daily_summaries(days, seed))
    return buffer.getvalue().encode()


def local_day(record: dict) -> date | None:
    """Return the source-local calendar day, or None for a flagged time basis."""
    if "date" in record:
        return date.fromisoformat(record["date"])
    if "start_utc" in record and "utc_offset" in record:
        utc = datetime.fromisoformat(record["start_utc"].replace("Z", "+00:00"))
        sign = 1 if record["utc_offset"][0] == "+" else -1
        hours, minutes = record["utc_offset"][1:].split(":")
        return (utc + sign * timedelta(hours=int(hours), minutes=int(minutes))).date()
    start = record.get("start")
    if start is None:
        return None
    parsed = datetime.fromisoformat(start)
    if parsed.tzinfo is None:
        return None
    return parsed.date()


def _sum_and_mean(values: list) -> tuple[int | float, float]:
    """The exact sum and mean, each rounded once: an integer sum stays exact at any size."""
    exact = sum(map(Fraction, values), Fraction(0))
    total = int(exact) if all(type(value) is int for value in values) else float(exact)
    return total, float(exact / len(values))


def expected_daily(records: list[dict], metric: str) -> tuple[dict[str, dict], list[str]]:
    """Return ``{local_day: {count, sum, avg}}`` for ``metric`` plus flagged ids.

    ``metric`` is a dotted path such as ``metrics.calories`` or ``steps``.
    A record whose metric is absent is counted in no bucket; a flagged time
    basis is reported, never assigned to a day.
    """
    buckets: dict[str, list] = {}
    flagged: list[str] = []
    for record in records:
        day = local_day(record)
        if day is None:
            flagged.append(record.get("id") or record.get("date", "?"))
            continue
        value: object = record
        for part in metric.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            continue
        buckets.setdefault(day.isoformat(), []).append(value)
    result = {}
    for key, values in sorted(buckets.items()):
        total, mean = _sum_and_mean(values)
        result[key] = {"count": len(values), "sum": total, "avg": mean}
    return result, flagged
