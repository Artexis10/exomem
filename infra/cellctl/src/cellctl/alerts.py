"""The platform alerts cellctl raises (move-cloud-cells-to-local-storage).

One alert fires while a running cell's last successful backup is older than
its schedule allows (task 2.8), and one while a local cell past 80% use has no
room on its node to grow (D10). Each goes through the existing alert receiver
as the same content-free transition the hosted scheduler evaluator sends
(`infra/helm/platform/files/scheduler_runtime.py`). Substrate's receiver
(`alert-receiver.ts`) is the contract both senders follow: it accepts exactly
these five keys, and it emails only when an alert's state differs from the last
one it delivered. So a restarted cellctl can resend the current state without a
duplicate email.

They are alerts, never gates: nothing here changes what a pass does. The cells
themselves go to the log, by id, because the transition carries no content.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .state import GROWTH_NO_ROOM, CellRow, ClusterObservation
from .storage_config import StorageConfig

JOB = "exomem-cloud-cells"
BACKUP_ALERT = "backup-stale"
# move-cloud-cells-to-local-storage D10.
GROWTH_ALERT = "storage-growth-blocked"
# An hourly backup missed twice, or a nightly one missed once with two hours'
# grace for the window.
HOURLY_ALERT_AFTER = timedelta(hours=2)
NIGHTLY_ALERT_AFTER = timedelta(hours=26)
SERVING_STATES = ("running", "read_only")


def stale_backups(
    rows: Iterable[CellRow],
    observations: Mapping[str, ClusterObservation],
    now: datetime,
    *,
    storage: StorageConfig,
) -> list[str]:
    """Cells that should be serving and have no backup within their schedule.

    A cell on local storage is backed up hourly (D3), any other nightly. A cell
    that could not be observed this pass is held to the nightly bound, so an
    observation outage alone never fires the alert. A cell never backed up is
    aged from its creation."""

    stale = []
    for row in rows:
        if row.desired_state not in SERVING_STATES:
            continue
        observation = observations.get(row.cell_id)
        hourly = observation is not None and storage.is_local(observation.pv_storage_class)
        since = row.last_backup_at or row.created_at
        if since is not None and now - since > (HOURLY_ALERT_AFTER if hourly else NIGHTLY_ALERT_AFTER):
            stale.append(row.cell_id)
    return sorted(stale)


def growth_blocked(
    rows: Iterable[CellRow],
    observations: Mapping[str, ClusterObservation],
    verdicts: Mapping[str, str],
    *,
    storage: StorageConfig,
) -> bool | None:
    """D10: whether a serving cell past 80% use cannot grow because its node
    has no room. `verdicts` holds what each local cell's latest measured
    backup found, and lives only in cellctl's memory. After a restart the
    answer is unknown (None), not "resolved", until every serving local cell
    has been measured again; a cell that could not be observed this pass may
    be local, so it counts as unmeasured unless it has a verdict."""

    serving = [row for row in rows if row.desired_state in SERVING_STATES]
    if any(verdicts.get(row.cell_id) == GROWTH_NO_ROOM for row in serving):
        return True
    unmeasured = [
        row for row in serving
        if row.cell_id not in verdicts
        and ((observation := observations.get(row.cell_id)) is None or storage.is_local(observation.pv_storage_class))
    ]
    return None if unmeasured else False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def deliver(webhook_url: str, *, alert: str, active: bool, observed_at: datetime) -> None:
    """POST one transition to the receiver. The id binds the evaluation time,
    so a later firing is always new; the receiver folds a repeated state."""

    parsed = urlsplit(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("alert delivery target is not an exact HTTPS URL")
    transition = {"job": JOB, "alert": alert, "active": active}
    transition_id = hashlib.sha256(
        json.dumps({**transition, "observed_at": observed_at.isoformat()}, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"schema_version": 1, "transition_id": transition_id, **transition},
                        separators=(",", ":"), sort_keys=True).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Exomem-Alert-Transition": transition_id},
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if not 200 <= response.status < 300 or response.geturl() != webhook_url:
                raise RuntimeError("alert delivery failed")
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise RuntimeError("alert delivery failed") from error
