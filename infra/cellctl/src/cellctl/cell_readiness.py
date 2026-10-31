"""How cellctl reaches a cell's readiness answer, and its verdict for the
cell-recall-lagging alert.

The listener, its port setting and the answer are exomem's protocol, which
cellctl cannot import. This module therefore imports only the standard
library: the product suite loads it by path (tests/cellctl_cell_readiness.py),
starts a cell's health-only listener by these names and judges the answer that
the cell builds, so the two sides cannot drift apart unseen.
"""

from __future__ import annotations

import json
import math
from datetime import timedelta

# The cell's health-only listener: `GET /health/ready` and nothing else, on the
# port that the cell reads from CELL_HEALTH_PORT_ENV. cellctl derives every cell
# bearer, so it reads readiness here and never on the cell port.
CELL_HEALTH_PORT = 8766
CELL_HEALTH_PORT_ENV = "EXOMEM_CLOUD_HEALTH_PORT"
CELL_READINESS_PATH = "/health/ready"
# `retrieval.state` values of that answer, a closed protocol set; any other value is unread.
RECALL_READY = "ready"
RECALL_LAGGING = "lagging"


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
