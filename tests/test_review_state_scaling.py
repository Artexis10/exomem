"""The review-state store's scaling answer: schema, retention, compaction, a gate.

`.review-state.json` is a whole-file JSON rewrite per decision behind a fixed
read limit. "It grows with decisions" stopped being an answer once first-surfaced
records and dispositions joined it, so this module pins the sectioned schema, the
forward migration, the retention windows, compaction, the raised ceiling, and the
stress gate whose failure is the declared trigger for the append-plus-compaction
or SQLite migration the roadmap keeps in reserve.

The first test is the GAP PROOF: today there is one flat section, no retention,
no compaction, and a 4 MiB ceiling.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pytest

from exomem import review_state

# --------------------------------------------------------------------------
# Stress-gate budgets — MEASURED on the implementation lane, not hand-tuned.
#
# Two consecutive runs on this lane (WSL2 on ext4, CPython 3.13), over a store
# of 50,000 decision records and 150,000 ledger entries built by
# `_stress_payload` below:
#
#                     run 1     run 2
#   encoded store     41.28 MiB 41.28 MiB   (as written, indented)
#   load()             0.304 s   0.275 s
#   one lookup         0.0230 ms 0.0317 ms  (dict hit on a preloaded payload)
#   one compact()      0.440 s   0.379 s    (drops 16,666 records, 75,000 entries)
#   compacted file    23.44 MiB 23.44 MiB
#   one apply()        0.285 s   0.258 s    (load, mutate, encode, fsync, replace)
#
# The budgets below are those numbers with roughly 4-6x margin, which is the
# spread this box shows between a quiesced run and one taken while the rest of
# the suite runs. They are a CEILING on a decade of heavy use, not a performance
# target: the point of the gate is that the whole-file rewrite is still viable
# at that cardinality, and the day it is not, THIS FAILING is the trigger for
# the append-plus-compaction or SQLite migration the roadmap keeps in reserve.
#
# The lookup budget is deliberately tight rather than generous. At 2 ms it is
# ~60x the measured dict hit and still below what a linear scan over 50,000
# records costs, so it fails if the lookup ever stops being a hash lookup —
# which is the only regression this particular number can catch.
LOAD_BUDGET_SECONDS = 2.0
LOOKUP_BUDGET_SECONDS = 0.002
APPLY_BUDGET_SECONDS = 1.5
COMPACT_BUDGET_SECONDS = 2.0
STRESS_RECORDS = 50_000
STRESS_LEDGER_ENTRIES = 150_000


def _now() -> dt.datetime:
    return dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def _stamp(days_ago: int) -> str:
    return (_now() - dt.timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


# ==========================================================================
# THE GAP PROOF
# ==========================================================================


def test_the_store_has_no_schema_retention_or_compaction_today(vault: Path) -> None:
    """RED-FIRST. One flat section, a 4 MiB ceiling, and nothing that prunes."""
    store = review_state.ReviewStateStore(vault)
    store.apply("a" * 24, "b" * 24, action="dismiss", why="handled: done")
    payload = store.load()

    missing: list[str] = []
    for section in ("dispositions", "surfaced", "stats"):
        if section not in payload:
            missing.append(f"no `{section}` section")
    if payload.get("version") != 2:
        missing.append(f"schema version is {payload.get('version')}, not 2")
    if not hasattr(store, "compact"):
        missing.append("the store cannot compact")
    if review_state._STATE_READ_LIMIT <= 4 * 1024 * 1024:
        missing.append(
            f"the read limit is still {review_state._STATE_READ_LIMIT} bytes"
        )

    assert missing == [], "; ".join(missing)


# ==========================================================================
# schema and migration
# ==========================================================================


def test_a_previous_schema_store_keeps_its_decisions(vault: Path) -> None:
    path = review_state.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "a" * 24 + ":" + "b" * 24: {
                        "item_id": "a" * 24,
                        "fingerprint": "b" * 24,
                        "action": "dismiss",
                        "until": None,
                        "why": "deliberate",
                        "updated_at": _stamp(10),
                    }
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    store = review_state.ReviewStateStore(vault)
    assert store.effective_state("a" * 24, "b" * 24)[0] == "dismissed"

    store.apply("c" * 24, "d" * 24, action="dismiss", why="handled: new one")

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["version"] == 2
    assert store.effective_state("a" * 24, "b" * 24)[0] == "dismissed"
    assert store.effective_state("c" * 24, "d" * 24)[0] == "dismissed"


def test_a_newer_schema_is_refused(vault: Path) -> None:
    path = review_state.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "records": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="REVIEW_STATE_INVALID"):
        review_state.ReviewStateStore(vault).load()


def test_the_read_limit_holds_the_stress_cardinality() -> None:
    """Derived from the gate's own numbers, never restated independently.

    The constant's docstring carries the measurement; this pins the property the
    measurement was taken for, so a future edit that lowers the ceiling below
    what the gate builds fails here rather than in a confusing OSError.
    """
    assert review_state._STATE_READ_LIMIT >= 42 * 1024 * 1024


# ==========================================================================
# retention and compaction
# ==========================================================================


def _seeded(vault: Path) -> review_state.ReviewStateStore:
    store = review_state.ReviewStateStore(vault)
    payload = {
        "version": 2,
        "records": {
            "lapsed" + "0" * 18 + ":" + "f" * 24: {
                "item_id": "lapsed" + "0" * 18,
                "fingerprint": "f" * 24,
                "action": "snooze",
                "until": (_now().date() - dt.timedelta(days=200)).isoformat(),
                "why": "deferred: later",
                "reason": "deferred",
                "origin": "manual",
                "updated_at": _stamp(200),
            },
            "fresh" + "0" * 19 + ":" + "f" * 24: {
                "item_id": "fresh" + "0" * 19,
                "fingerprint": "f" * 24,
                "action": "snooze",
                "until": (_now().date() - dt.timedelta(days=5)).isoformat(),
                "why": "deferred: later",
                "reason": "deferred",
                "origin": "manual",
                "updated_at": _stamp(5),
            },
            "standing" + "0" * 16 + ":" + "f" * 24: {
                "item_id": "standing" + "0" * 16,
                "fingerprint": "f" * 24,
                "action": "dismiss",
                "until": None,
                "why": "intentional: deliberate",
                "reason": "intentional",
                "origin": "manual",
                "updated_at": _stamp(900),
            },
            "stance" + "0" * 18 + ":" + "f" * 24: {
                "item_id": "stance" + "0" * 18,
                "fingerprint": "f" * 24,
                "action": "competing",
                "until": None,
                "why": "intentional: rivals",
                "reason": "intentional",
                "origin": "manual",
                "updated_at": _stamp(900),
            },
        },
        "dispositions": {
            "prediction_window": {
                "family": "prediction_window",
                "disposition": "quiet",
                "reason": "too_frequent",
                "why": "too_frequent: enough",
                "updated_at": _stamp(900),
                "origin": "manual",
            }
        },
        "surfaced": {
            "stale" + "0" * 19 + ":" + "f" * 24: {
                "first_surfaced_at": _stamp(500),
                "surface": "review",
                "origin": "automatic",
            },
            "recent" + "0" * 18 + ":" + "f" * 24: {
                "first_surfaced_at": _stamp(10),
                "surface": "review",
                "origin": "automatic",
            },
            "standing" + "0" * 16 + ":" + "f" * 24: {
                "first_surfaced_at": _stamp(900),
                "surface": "review",
                "origin": "automatic",
            },
        },
        "stats": {},
    }
    store._write(payload)
    return store


def test_compaction_drops_only_what_retention_allows(vault: Path) -> None:
    store = _seeded(vault)
    report = store.compact(force=True, now=_now())
    payload = store.load()

    assert "lapsed" + "0" * 18 + ":" + "f" * 24 not in payload["records"]
    assert "fresh" + "0" * 19 + ":" + "f" * 24 in payload["records"]
    assert "standing" + "0" * 16 + ":" + "f" * 24 in payload["records"]
    assert "stance" + "0" * 18 + ":" + "f" * 24 in payload["records"]
    assert payload["dispositions"]["prediction_window"]["disposition"] == "quiet"

    assert "stale" + "0" * 19 + ":" + "f" * 24 not in payload["surfaced"]
    assert "recent" + "0" * 18 + ":" + "f" * 24 in payload["surfaced"]
    # A stale ledger entry whose decision still stands is kept: the decision is
    # the reason the metric would want the entry.
    assert "standing" + "0" * 16 + ":" + "f" * 24 in payload["surfaced"]

    assert report["dropped"]["records"] == 1
    assert report["dropped"]["surfaced"] == 1
    assert report["origin"] == "automatic"


def test_compaction_runs_on_write_past_the_record_threshold(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_state, "_COMPACT_RECORD_THRESHOLD", 2)
    store = _seeded(vault)
    store.apply("e" * 24, "f" * 24, action="dismiss", why="handled: done")
    payload = store.load()
    assert "lapsed" + "0" * 18 + ":" + "f" * 24 not in payload["records"]
    assert payload["stats"]["compaction"]["dropped"]["records"] == 1


def test_reconcile_reports_what_compaction_dropped(vault: Path) -> None:
    from exomem import commands

    _seeded(vault)
    result = commands.op_maintain_memory(vault, mode="reconcile", dry_run=False)
    assert result["review_state_compaction"]["dropped"]["records"] == 1
    assert result["review_state_compaction"]["dropped"]["surfaced"] == 1


def test_compaction_never_drops_a_standing_decision_or_a_disposition(
    vault: Path,
) -> None:
    store = _seeded(vault)
    for _ in range(3):
        store.compact(force=True, now=_now())
    payload = store.load()
    assert "standing" + "0" * 16 + ":" + "f" * 24 in payload["records"]
    assert "stance" + "0" * 18 + ":" + "f" * 24 in payload["records"]
    assert "prediction_window" in payload["dispositions"]


# ==========================================================================
# the stress gate
# ==========================================================================


def _stress_payload() -> dict:
    records = {}
    for index in range(STRESS_RECORDS):
        item = f"{index:024x}"
        finger = f"{index:024x}"
        # Two thirds standing, one third lapsed snoozes past retention, which is
        # the shape compaction has to be able to walk.
        if index % 3 == 2:
            records[f"{item}:{finger}"] = {
                "item_id": item,
                "fingerprint": finger,
                "action": "snooze",
                "until": (_now().date() - dt.timedelta(days=200)).isoformat(),
                "why": "deferred: later",
                "reason": "deferred",
                "origin": "manual",
                "updated_at": _stamp(200),
            }
        else:
            records[f"{item}:{finger}"] = {
                "item_id": item,
                "fingerprint": finger,
                "action": "dismiss",
                "until": None,
                "why": "intentional: deliberate",
                "reason": "intentional",
                "origin": "manual",
                "updated_at": _stamp(30),
            }
    surfaced = {
        f"{index:024x}:{index:024x}s": {
            "first_surfaced_at": _stamp(500 if index % 2 else 10),
            "surface": "review",
            "origin": "automatic",
        }
        for index in range(STRESS_LEDGER_ENTRIES)
    }
    return {
        "version": 2,
        "records": records,
        "dispositions": {},
        "surfaced": surfaced,
        "stats": {},
    }


@pytest.mark.timeout(600)
def test_the_stress_gate_holds_at_multi_year_cardinality(vault: Path) -> None:
    """50,000 decisions and 150,000 ledger entries — a decade of heavy use.

    Failing this is the declared trigger for the append-plus-compaction or
    SQLite migration the roadmap keeps in reserve. See the module header for the
    measurements the budgets are pinned from.
    """
    store = review_state.ReviewStateStore(vault)
    store._write(_stress_payload())
    size = review_state.state_path(vault).stat().st_size
    assert size < review_state._STATE_READ_LIMIT, size

    start = time.perf_counter()
    payload = store.load()
    load_seconds = time.perf_counter() - start
    assert load_seconds < LOAD_BUDGET_SECONDS, f"load took {load_seconds:.3f}s"

    key = f"{7:024x}"
    start = time.perf_counter()
    assert store.decision(key, key, payload=payload) is not None
    lookup_seconds = time.perf_counter() - start
    assert lookup_seconds < LOOKUP_BUDGET_SECONDS, f"lookup took {lookup_seconds:.4f}s"

    # Compaction BEFORE the apply, deliberately: an apply past the record
    # threshold compacts as part of its own work, so measuring compaction after
    # one would measure a store that has nothing left to drop.
    start = time.perf_counter()
    report = store.compact(force=True, now=_now())
    compact_seconds = time.perf_counter() - start
    assert compact_seconds < COMPACT_BUDGET_SECONDS, f"compact took {compact_seconds:.3f}s"
    assert report["dropped"]["records"] > 0
    assert report["dropped"]["surfaced"] > 0

    start = time.perf_counter()
    store.apply("f" * 24, "e" * 24, action="dismiss", why="handled: done")
    apply_seconds = time.perf_counter() - start
    assert apply_seconds < APPLY_BUDGET_SECONDS, f"apply took {apply_seconds:.3f}s"

    compacted = review_state.state_path(vault).stat().st_size
    assert compacted < review_state._STATE_READ_LIMIT, compacted
