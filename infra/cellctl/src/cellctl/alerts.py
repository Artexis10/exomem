"""The backup-age alert (move-cloud-cells-to-local-storage, task 2.8).

A running cell whose last successful backup is older than its schedule allows
raises one platform alert through the existing alert receiver, as the same
content-free transition the hosted scheduler evaluator sends
(`infra/helm/platform/files/scheduler_runtime.py`). Substrate's receiver
(`alert-receiver.ts`) is the contract both senders follow: it accepts exactly
these five keys, and it emails only when an alert's state differs from the last
one it delivered. So a restarted cellctl can resend the current state without a
duplicate email, and keeps no alert state of its own.

It is an alert, never a gate: nothing here changes what a pass does. The stale
cells themselves go to the log, by id, because the transition carries no
content.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .state import CellRow, ClusterObservation
from .storage_config import StorageConfig

JOB = "exomem-cloud-cells"
ALERT = "backup-stale"
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def deliver(webhook_url: str, *, active: bool, observed_at: datetime) -> None:
    """POST one transition to the receiver. The id binds the evaluation time,
    so a later firing is always new; the receiver folds a repeated state."""

    parsed = urlsplit(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("alert delivery target is not an exact HTTPS URL")
    transition = {"job": JOB, "alert": ALERT, "active": active}
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
