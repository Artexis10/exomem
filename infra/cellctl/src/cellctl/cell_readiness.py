"""How cellctl reaches a cell's readiness answer, and its verdicts for the
cell-recall-lagging and cell-originals-damaged alerts.

The listener, its port setting and the answer are exomem's protocol, which
cellctl cannot import. This module therefore imports only the standard
library: the product suite loads it by path (tests/cellctl_cell_readiness.py),
starts a cell's health-only listener by these names and judges the answer that
the cell builds, so the two sides cannot drift apart unseen.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

# The cell's health-only listener: `GET /health/ready` and `GET
# /health/traffic-history` and nothing else, on the port that the cell reads
# from CELL_HEALTH_PORT_ENV. cellctl derives every cell bearer, so it reads
# readiness and the hourly counts here and never on the cell port.
CELL_HEALTH_PORT = 8766
CELL_HEALTH_PORT_ENV = "EXOMEM_CLOUD_HEALTH_PORT"
CELL_READINESS_PATH = "/health/ready"
# automate-cloud-releases D4: the cell's hourly request counts and observed seconds.
CELL_TRAFFIC_HISTORY_PATH = "/health/traffic-history"
# `retrieval.state` values of that answer, a closed protocol set; any other value is unread.
RECALL_READY = "ready"
RECALL_LAGGING = "lagging"
# `originals.state` value of that answer when a fixity pass has counted; any other value is unknown.
ORIGINALS_MEASURED = "measured"


def recall_lag_verdict(body: bytes, *, after: timedelta) -> bool | None:
    """Whether one cell's readiness answer shows its recall serving a
    catalogue unchanged for at least `after`. Only `retrieval.state` and
    `retrieval.catalogue_unchanged_seconds` are read, and the body is never logged.

    False only when recall reads strictly ready, which the cell answers only
    once its catalogue has caught up. A lag younger than `after`, or with no
    age, is unknown (None): the cell measures the age from when its own
    process first saw that catalogue, so a cell restarted during a stall
    reports a young age for an old stall, and a RESOLVED then would end an
    alert while the stall goes on. An answer that cannot be read is unknown too."""

    try:
        payload = json.loads(body)
    # RecursionError: a cell's answer is not trusted, and deep nesting must not take the pass down.
    except (ValueError, RecursionError):
        return None
    retrieval = payload.get("retrieval") if isinstance(payload, dict) else None
    if not isinstance(retrieval, dict):
        return None
    state = retrieval.get("state")
    if state == RECALL_READY:
        return False
    if state != RECALL_LAGGING:
        return None
    age = retrieval.get("catalogue_unchanged_seconds")
    if isinstance(age, bool) or not isinstance(age, int | float):
        return None
    # Only a float can be NaN or infinite. An int compares with a float exactly
    # however large it is, but math.isfinite raises OverflowError on one no float can hold.
    if isinstance(age, float) and not math.isfinite(age):
        return None
    return True if age >= after.total_seconds() else None


def originals_verdict(body: bytes) -> bool | None:
    """Whether one cell's readiness answer counts a missing or damaged stored
    original. Only `originals.state` and `originals.unresolved` are read, and the
    body is never logged.

    False only when the cell's last fixity pass counted none. Before the first
    pass, with fixity off, or for an answer that cannot be read, it is unknown (None)."""

    try:
        payload = json.loads(body)
    # RecursionError: a cell's answer is not trusted, and deep nesting must not take the pass down.
    except (ValueError, RecursionError):
        return None
    originals = payload.get("originals") if isinstance(payload, dict) else None
    if not isinstance(originals, dict) or originals.get("state") != ORIGINALS_MEASURED:
        return None
    unresolved = originals.get("unresolved")
    if isinstance(unresolved, bool) or not isinstance(unresolved, int) or unresolved < 0:
        return None
    return unresolved > 0


def _payload(body: bytes) -> dict | None:
    try:
        payload = json.loads(body)
    # RecursionError: a cell's answer is not trusted, and deep nesting must not take the pass down.
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def traffic_idle(body: bytes) -> tuple[int, float | None] | None:
    """The readiness `traffic` block's `inflight` and `last_request_age_seconds`, or
    None when either cannot be read. The age is None when the cell has handled no MCP
    request since it started. The body is never logged."""

    payload = _payload(body)
    traffic = payload.get("traffic") if payload is not None else None
    if not isinstance(traffic, dict) or not _count(traffic.get("inflight")) or "last_request_age_seconds" not in traffic:
        return None
    age = traffic["last_request_age_seconds"]
    if age is None:
        return traffic["inflight"], None
    if isinstance(age, bool) or not isinstance(age, int | float) or (isinstance(age, float) and not math.isfinite(age)):
        return None
    return traffic["inflight"], float(age)


def traffic_history(body: bytes) -> tuple[int, datetime, tuple[int, ...], tuple[int, ...]] | None:
    """The `/health/traffic-history` answer as (retention_hours, first bucket's UTC hour,
    request counts, observed seconds), or None when it cannot be read."""

    payload = _payload(body)
    if payload is None:
        return None
    retention = payload.get("retention_hours")
    requests = payload.get("requests")
    seconds = payload.get("observed_seconds")
    first_raw = payload.get("first_hour")
    if not (_count(retention) and retention > 0 and isinstance(requests, list) and isinstance(seconds, list)
            and len(requests) == len(seconds) and isinstance(first_raw, str)):
        return None
    if not all(_count(value) for value in requests) or not all(_count(value) for value in seconds):
        return None
    try:
        first = datetime.fromisoformat(first_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if first.tzinfo is None:
        return None
    return retention, first.astimezone(UTC), tuple(requests), tuple(seconds)
