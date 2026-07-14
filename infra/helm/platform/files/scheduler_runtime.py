#!/usr/bin/env python3
"""Persist scheduler outcomes, expose metrics, and evaluate content-free alerts."""

from __future__ import annotations

import copy
import json
import os
import signal
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

NAMESPACE = "exomem-platform"
API_ORIGIN = "https://kubernetes.default.svc"
TOKEN_PATH = "/var/run/secrets/exomem-api/token"
CA_PATH = "/var/run/secrets/exomem-api/ca.crt"
STATE_PREFIX = "exomem-hosted-scheduler-state-"
ALERT_STATE = "exomem-hosted-scheduler-alert-state"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def initial_state(job: str, cadence_seconds: int) -> dict[str, Any]:
    if not job or cadence_seconds <= 0:
        raise ValueError("scheduler state identity is invalid")
    return {
        "schema_version": 1,
        "job": job,
        "cadence_seconds": cadence_seconds,
        "attempts_total": 0,
        "failures_total": 0,
        "consecutive_failures": 0,
        "last_attempt_unixtime": 0,
        "last_success_unixtime": 0,
        "duration_seconds": {
            "buckets": {"1": 0, "5": 0, "20": 0, "+Inf": 0},
            "count": 0,
            "sum": 0.0,
        },
    }


def _validate_state(state: dict[str, Any]) -> None:
    expected = initial_state(str(state.get("job", "")), int(state.get("cadence_seconds", 0)))
    if set(state) != set(expected) or state.get("schema_version") != 1:
        raise ValueError("scheduler state shape is invalid")
    histogram = state.get("duration_seconds")
    if not isinstance(histogram, dict) or set(histogram) != {"buckets", "count", "sum"}:
        raise ValueError("scheduler histogram is invalid")
    buckets = histogram.get("buckets")
    if not isinstance(buckets, dict) or set(buckets) != {"1", "5", "20", "+Inf"}:
        raise ValueError("scheduler histogram is invalid")
    integer_fields = (
        "attempts_total",
        "failures_total",
        "consecutive_failures",
        "last_attempt_unixtime",
        "last_success_unixtime",
    )
    values = [state.get(name) for name in integer_fields]
    values.extend(buckets.values())
    values.append(histogram.get("count"))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise ValueError("scheduler state counters are invalid")
    total = histogram.get("sum")
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total < 0:
        raise ValueError("scheduler histogram is invalid")


def record_attempt(
    state: dict[str, Any], *, success: bool, duration_seconds: float, observed_at: int
) -> dict[str, Any]:
    _validate_state(state)
    if duration_seconds < 0 or observed_at <= 0:
        raise ValueError("scheduler observation is invalid")
    updated = copy.deepcopy(state)
    updated["attempts_total"] += 1
    updated["last_attempt_unixtime"] = observed_at
    if success:
        updated["last_success_unixtime"] = observed_at
        updated["consecutive_failures"] = 0
    else:
        updated["failures_total"] += 1
        updated["consecutive_failures"] += 1
    histogram = updated["duration_seconds"]
    histogram["count"] += 1
    histogram["sum"] = round(float(histogram["sum"]) + duration_seconds, 6)
    for label, bound in (("1", 1.0), ("5", 5.0), ("20", 20.0)):
        if duration_seconds <= bound:
            histogram["buckets"][label] += 1
    histogram["buckets"]["+Inf"] += 1
    return updated


def initial_alert_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "baselines": {},
        "active": {},
        "transitions_total": 0,
        "last_evaluated_unixtime": 0,
    }


def evaluate_alerts(
    states: list[dict[str, Any]],
    alert_state: dict[str, Any],
    *,
    observed_at: int,
    missed_run_seconds: int,
    failure_threshold: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if missed_run_seconds <= 0 or failure_threshold <= 0:
        raise ValueError("scheduler alert thresholds are invalid")
    if alert_state.get("schema_version") != 1 or set(alert_state) != set(initial_alert_state()):
        raise ValueError("scheduler alert state is invalid")
    updated = copy.deepcopy(alert_state)
    baselines = updated["baselines"]
    active = updated["active"]
    if not isinstance(baselines, dict) or not isinstance(active, dict):
        raise ValueError("scheduler alert state is invalid")
    transitions: list[dict[str, Any]] = []
    for state in states:
        _validate_state(state)
        job = state["job"]
        baseline = baselines.setdefault(job, observed_at)
        if not isinstance(baseline, int) or baseline <= 0:
            raise ValueError("scheduler alert baseline is invalid")
        anchor = max(baseline, state["last_attempt_unixtime"])
        desired = {
            "missed-run": observed_at >= anchor + state["cadence_seconds"] + missed_run_seconds,
            "consecutive-failures": state["consecutive_failures"] >= failure_threshold,
        }
        for alert_name, firing in desired.items():
            key = f"{job}:{alert_name}"
            previous = active.get(key, False)
            if not isinstance(previous, bool):
                raise ValueError("scheduler alert state is invalid")
            active[key] = firing
            if previous != firing:
                transitions.append({"job": job, "alert": alert_name, "active": firing})
    updated["transitions_total"] += len(transitions)
    updated["last_evaluated_unixtime"] = observed_at
    return updated, transitions


def _labels(**labels: str) -> str:
    rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    return "{" + rendered + "}"


def render_metrics(states: list[dict[str, Any]], alert_state: dict[str, Any]) -> str:
    lines = [
        "# TYPE exomem_hosted_scheduler_attempts_total counter",
        "# TYPE exomem_hosted_scheduler_failures_total counter",
        "# TYPE exomem_hosted_scheduler_duration_seconds histogram",
        "# TYPE exomem_hosted_scheduler_last_success_unixtime gauge",
        "# TYPE exomem_hosted_scheduler_alert_active gauge",
    ]
    for state in sorted(states, key=lambda item: str(item.get("job", ""))):
        _validate_state(state)
        labels = _labels(job=state["job"], contract_version="1")
        lines.append(f"exomem_hosted_scheduler_attempts_total{labels} {state['attempts_total']}")
        lines.append(f"exomem_hosted_scheduler_failures_total{labels} {state['failures_total']}")
        for bound in ("1", "5", "20", "+Inf"):
            bucket_labels = _labels(
                job=state["job"], contract_version="1", le=bound
            )
            count = state["duration_seconds"]["buckets"][bound]
            lines.append(f"exomem_hosted_scheduler_duration_seconds_bucket{bucket_labels} {count}")
        lines.append(
            f"exomem_hosted_scheduler_duration_seconds_sum{labels} "
            f"{state['duration_seconds']['sum']}"
        )
        lines.append(
            f"exomem_hosted_scheduler_duration_seconds_count{labels} "
            f"{state['duration_seconds']['count']}"
        )
        lines.append(
            f"exomem_hosted_scheduler_last_success_unixtime{labels} "
            f"{state['last_success_unixtime']}"
        )
    active = alert_state.get("active")
    if not isinstance(active, dict):
        raise ValueError("scheduler alert state is invalid")
    for key, firing in sorted(active.items()):
        job, alert = key.rsplit(":", 1)
        lines.append(
            f"exomem_hosted_scheduler_alert_active{_labels(job=job, alert=alert)} "
            f"{1 if firing else 0}"
        )
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


def _api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = open(TOKEN_PATH, encoding="utf-8").read().strip()
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        API_ORIGIN + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=CA_PATH)
    with urllib.request.urlopen(request, timeout=10, context=context) as response:  # noqa: S310
        result = json.load(response)
    if not isinstance(result, dict):
        raise RuntimeError("Kubernetes API returned an invalid object")
    return result


def _configmap(name: str) -> dict[str, Any]:
    return _api_request("GET", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{name}")


def _read_state(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    resource = _configmap(name)
    data = resource.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("state.json"), str):
        raise RuntimeError("scheduler state ConfigMap is invalid")
    state = json.loads(data["state.json"])
    if not isinstance(state, dict):
        raise RuntimeError("scheduler state ConfigMap is invalid")
    return resource, state


def _write_state(resource: dict[str, Any], state: dict[str, Any]) -> None:
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise RuntimeError("scheduler state ConfigMap is invalid")
    resource["data"] = {"state.json": json.dumps(state, separators=(",", ":"), sort_keys=True)}
    _api_request(
        "PUT",
        f"/api/v1/namespaces/{NAMESPACE}/configmaps/{metadata['name']}",
        resource,
    )


def _exact_https_request(target: str, bearer: str, connect_timeout: int) -> bool:
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not bearer
    ):
        raise ValueError("scheduler target is not exact HTTPS")
    request = urllib.request.Request(
        target,
        method="GET",
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=connect_timeout) as response:
            return response.status == 200 and response.geturl() == target
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return False


def request_once() -> int:
    job = os.environ["JOB_NAME"]
    target = os.environ["TARGET_URL"]
    bearer = os.environ["EXOMEM_HOSTED_SCHEDULER_SECRET"]
    cadence = int(os.environ["CADENCE_SECONDS"])
    connect_timeout = int(os.environ.get("CONNECT_TIMEOUT_SECONDS", "5"))
    total_timeout = int(os.environ.get("TOTAL_TIMEOUT_SECONDS", "20"))
    resource, state = _read_state(STATE_PREFIX + job)
    _validate_state(state)
    if state["job"] != job or state["cadence_seconds"] != cadence:
        raise RuntimeError("scheduler state identity does not match the job")
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline(_signum: int, _frame: Any) -> None:
        raise TimeoutError("scheduler total request deadline exceeded")

    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, total_timeout)
    try:
        success = _exact_https_request(target, bearer, connect_timeout)
    except TimeoutError:
        success = False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    duration = max(0.0, time.monotonic() - started)
    updated = record_attempt(state, success=success, duration_seconds=duration, observed_at=int(time.time()))
    _write_state(resource, updated)
    return 0 if success else 2


def _all_states() -> list[dict[str, Any]]:
    names = [item for item in os.environ["SCHEDULER_JOBS"].split(",") if item]
    return [_read_state(STATE_PREFIX + name)[1] for name in names]


def evaluate_once() -> None:
    alert_resource, alert_state = _read_state(ALERT_STATE)
    updated, transitions = evaluate_alerts(
        _all_states(),
        alert_state,
        observed_at=int(time.time()),
        missed_run_seconds=int(os.environ["MISSED_RUN_SECONDS"]),
        failure_threshold=int(os.environ["FAILURE_THRESHOLD"]),
    )
    _write_state(alert_resource, updated)
    for transition in transitions:
        firing = transition["active"]
        event = {
            "apiVersion": "v1",
            "kind": "Event",
            "metadata": {
                "generateName": "exomem-scheduler-alert-",
                "namespace": NAMESPACE,
            },
            "involvedObject": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "name": ALERT_STATE,
                "namespace": NAMESPACE,
            },
            "reason": "SchedulerAlertFiring" if firing else "SchedulerAlertResolved",
            "message": f"{transition['job']} {transition['alert']} {'firing' if firing else 'resolved'}",
            "type": "Warning" if firing else "Normal",
            "source": {"component": "exomem-hosted-scheduler-alerts"},
            "firstTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "lastTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": 1,
        }
        _api_request("POST", f"/api/v1/namespaces/{NAMESPACE}/events", event)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/metrics":
            self.send_error(404)
            return
        try:
            _, alerts = _read_state(ALERT_STATE)
            body = render_metrics(_all_states(), alerts).encode()
        except Exception:  # noqa: BLE001
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/openmetrics-text; version=1.0.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "request":
        return request_once()
    if mode == "evaluate":
        while True:
            evaluate_once()
            time.sleep(60)
    if mode == "collect":
        ThreadingHTTPServer(("0.0.0.0", 9090), MetricsHandler).serve_forever()
        return 0
    print("usage: scheduler_runtime.py request|evaluate|collect", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
