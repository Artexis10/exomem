#!/usr/bin/env python3
"""Read Neon metadata and report whether hosted database budget controls hold."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

_IDENTIFIER = re.compile(r"^[a-z0-9-]{1,60}$")
_TIMEOUT_SECONDS = 20
_Runner = Callable[..., subprocess.CompletedProcess[str]]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return value if parsed.tzinfo == UTC else None


def _get(path: str, *, neonctl: str, runner: _Runner) -> dict[str, Any] | None:
    try:
        result = runner(
            [neonctl, "api", path, "--output", "json", "--no-analytics"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        return None
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _finding(code: str, status: str, **values: int | float) -> dict[str, object]:
    return {"code": code, **values, "status": status}


def _report(
    findings: list[dict[str, object]],
    *,
    compute_time_seconds: int | None = None,
    compute_quota_seconds: int | None = None,
    billing_period: dict[str, str] | None = None,
    monthly_compute_hours: int | None = None,
    provider_enforced: bool | None = None,
) -> dict[str, object]:
    return {
        "billingPeriod": billing_period,
        "computeQuotaSeconds": compute_quota_seconds,
        "computeTimeSeconds": compute_time_seconds,
        "findings": findings,
        "monthlyComputeQuota": {
            "policyRequested": monthly_compute_hours is not None,
            "providerEnforced": provider_enforced,
            "requestedHours": monthly_compute_hours,
            "seconds": compute_quota_seconds,
        },
        "observedAtUTC": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "pass" if all(item["status"] == "pass" for item in findings) else "fail",
    }


def _project_identity(response: dict[str, Any] | None, project_id: str) -> dict[str, Any] | None:
    project = response.get("project") if response is not None else None
    if not isinstance(project, dict) or project.get("id") != project_id:
        return None
    org_id = project.get("org_id")
    if not isinstance(org_id, str) or _IDENTIFIER.fullmatch(org_id) is None:
        return None
    return project


def _project_details(
    project: dict[str, Any],
) -> tuple[int | None, int | None, dict[str, str] | None, bool, bool]:
    compute_time = _integer(project.get("compute_time_seconds"))
    quota_value: int | None = None
    quota_known = True
    if "settings" in project:
        settings = project["settings"]
        if not isinstance(settings, dict):
            quota_known = False
        elif "quota" in settings:
            quota = settings["quota"]
            if not isinstance(quota, dict):
                quota_known = False
            elif "compute_time_seconds" in quota:
                quota_value = _integer(quota["compute_time_seconds"])
                if quota_value is None or quota_value < 0:
                    quota_known = False
    start = _timestamp(project.get("consumption_period_start"))
    end = _timestamp(project.get("consumption_period_end"))
    period = {"end": end, "start": start} if start is not None and end is not None else None
    usage_known = False
    if compute_time is not None and compute_time >= 0 and start is not None and end is not None:
        usage_known = datetime.fromisoformat(start[:-1] + "+00:00") < datetime.fromisoformat(
            end[:-1] + "+00:00"
        )
    return (
        compute_time if usage_known else None,
        quota_value if quota_known else None,
        period if usage_known else None,
        usage_known,
        quota_known,
    )


def _endpoint_findings(response: dict[str, Any] | None, project_id: str) -> list[dict[str, object]]:
    endpoints = response.get("endpoints") if response is not None else None
    if not isinstance(endpoints, list):
        return [
            _finding("endpoint_capacity_unknown", "unknown"),
            _finding("endpoint_count_unknown", "unknown"),
        ]
    if not all(
        isinstance(endpoint, dict)
        and endpoint.get("project_id") == project_id
        and isinstance(endpoint.get("id"), str)
        and _IDENTIFIER.fullmatch(endpoint["id"]) is not None
        and isinstance(endpoint.get("branch_id"), str)
        and _IDENTIFIER.fullmatch(endpoint["branch_id"]) is not None
        for endpoint in endpoints
    ):
        return [
            _finding("endpoint_capacity_unknown", "unknown"),
            _finding("endpoint_count_unknown", "unknown"),
        ]
    count_status = "pass" if len(endpoints) == 1 else "fail"
    capacities: list[tuple[float, float, int]] = []
    for endpoint in endpoints:
        minimum = _number(endpoint.get("autoscaling_limit_min_cu"))
        maximum = _number(endpoint.get("autoscaling_limit_max_cu"))
        autosuspend = _integer(endpoint.get("suspend_timeout_seconds"))
        if minimum is None or maximum is None or autosuspend is None or autosuspend < -1:
            return [
                _finding("endpoint_capacity_unknown", "unknown"),
                _finding("endpoint_count", count_status, observed=len(endpoints)),
            ]
        capacities.append((minimum, maximum, autosuspend))
    if not capacities:
        return [
            _finding("endpoint_capacity", "fail"),
            _finding("endpoint_count", count_status, observed=0),
        ]
    minimum = min(item[0] for item in capacities)
    maximum = max(item[1] for item in capacities)
    capacity_status = (
        "pass"
        if all(item[0] == 0.25 and 0 < item[0] <= item[1] <= 1 and item[2] >= 0 for item in capacities)
        else "fail"
    )
    return [
        _finding("endpoint_capacity", capacity_status, maxCu=maximum, minCu=minimum),
        _finding("endpoint_count", count_status, observed=len(endpoints)),
    ]


def _branch_findings(response: dict[str, Any] | None, project_id: str) -> list[dict[str, object]]:
    branches = response.get("branches") if response is not None else None
    if not isinstance(branches, list) or not all(
        isinstance(branch, dict)
        and branch.get("project_id") == project_id
        and isinstance(branch.get("id"), str)
        and _IDENTIFIER.fullmatch(branch["id"]) is not None
        and isinstance(branch.get("default"), bool)
        for branch in branches
    ):
        return [_finding("branch_inventory_unknown", "unknown")]
    if not _branch_inventory_complete(response) and len(branches) <= 1:
        return [_finding("branch_inventory_unknown", "unknown")]
    default_count = sum(branch.get("default") is True for branch in branches)
    return [
        _finding("branch_count", "pass" if len(branches) == 1 else "fail", observed=len(branches)),
        _finding("production_branch", "pass" if default_count == 1 else "fail", observed=default_count),
    ]


def _branch_inventory_complete(response: dict[str, Any] | None) -> bool:
    if response is None:
        return False
    if "pagination" not in response:
        return True
    pagination = response["pagination"]
    return isinstance(pagination, dict) and "next" not in pagination


def _inventory_link_finding(
    endpoints_response: dict[str, Any] | None,
    branches_response: dict[str, Any] | None,
    project_id: str,
) -> dict[str, object] | None:
    endpoints = endpoints_response.get("endpoints") if endpoints_response is not None else None
    branches = branches_response.get("branches") if branches_response is not None else None
    if (
        not isinstance(endpoints, list)
        or not isinstance(branches, list)
        or len(endpoints) != 1
        or len(branches) != 1
        or not _branch_inventory_complete(branches_response)
        or not isinstance(endpoints[0], dict)
        or not isinstance(branches[0], dict)
        or endpoints[0].get("project_id") != project_id
        or branches[0].get("project_id") != project_id
        or not isinstance(endpoints[0].get("id"), str)
        or _IDENTIFIER.fullmatch(endpoints[0]["id"]) is None
        or not isinstance(endpoints[0].get("branch_id"), str)
        or _IDENTIFIER.fullmatch(endpoints[0]["branch_id"]) is None
        or not isinstance(branches[0].get("id"), str)
        or _IDENTIFIER.fullmatch(branches[0]["id"]) is None
        or not isinstance(branches[0].get("default"), bool)
    ):
        return None
    if endpoints[0].get("branch_id") != branches[0].get("id"):
        return _finding("inventory_link_unknown", "unknown")
    return None


def audit(
    project_id: str,
    *,
    neonctl: str = "neonctl",
    monthly_compute_hours: int | None = None,
    runner: _Runner = subprocess.run,
) -> dict[str, object]:
    """Return a sanitized audit report after only Neon API GET metadata calls."""

    if _IDENTIFIER.fullmatch(project_id) is None:
        return _report([_finding("project_unknown", "unknown")])

    project_response = _get(f"/projects/{project_id}", neonctl=neonctl, runner=runner)
    endpoints_response = _get(f"/projects/{project_id}/endpoints", neonctl=neonctl, runner=runner)
    branches_response = _get(f"/projects/{project_id}/branches", neonctl=neonctl, runner=runner)
    project = _project_identity(project_response, project_id)
    findings = _endpoint_findings(endpoints_response, project_id)
    findings.extend(_branch_findings(branches_response, project_id))
    if project is None:
        findings.append(_finding("project_unknown", "unknown"))
        return _report(findings, monthly_compute_hours=monthly_compute_hours)

    compute_time, quota_seconds, period, usage_known, quota_known = _project_details(project)
    link_finding = _inventory_link_finding(endpoints_response, branches_response, project_id)
    if link_finding is not None:
        findings.append(link_finding)

    org_id = project["org_id"]
    assert isinstance(org_id, str)
    spending = _get(
        f"/organizations/{org_id}/billing/spending_limit", neonctl=neonctl, runner=runner
    )
    if spending is None or "spending_limit_cents" not in spending:
        findings.append(_finding("spending_alert_unknown", "unknown"))
    elif spending["spending_limit_cents"] is None:
        findings.append(_finding("spending_alert", "fail"))
    else:
        cents = _integer(spending["spending_limit_cents"])
        if cents is None:
            findings.append(_finding("spending_alert_unknown", "unknown"))
        else:
            findings.append(
                _finding("spending_alert", "pass" if 0 < cents <= 500 else "fail", cents=cents)
            )
    if not usage_known:
        findings.append(_finding("project_usage_unknown", "unknown"))
    if not quota_known:
        findings.append(_finding("compute_quota_unknown", "unknown"))
    elif monthly_compute_hours is not None:
        if quota_seconds is None:
            findings.append(_finding("monthly_compute_quota", "fail"))
        else:
            maximum = monthly_compute_hours * 3600
            findings.append(
                _finding(
                    "monthly_compute_quota",
                    "pass" if 0 < quota_seconds <= maximum else "fail",
                )
            )
    if not quota_known:
        provider_enforced = None
    elif quota_seconds is None:
        provider_enforced = False
    else:
        provider_enforced = quota_seconds > 0
    return _report(
        findings,
        compute_time_seconds=compute_time,
        compute_quota_seconds=quota_seconds,
        billing_period=period,
        monthly_compute_hours=monthly_compute_hours,
        provider_enforced=provider_enforced,
    )


def _positive_hours(value: str) -> int:
    try:
        hours = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive whole number") from exc
    if hours <= 0:
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return hours


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--neonctl", default="neonctl")
    parser.add_argument("--monthly-compute-hours", type=_positive_hours)
    return parser


def main(arguments: list[str] | None = None, *, runner: _Runner = subprocess.run) -> int:
    args = _parser().parse_args(arguments)
    report = audit(
        args.project_id,
        neonctl=args.neonctl,
        monthly_compute_hours=args.monthly_compute_hours,
        runner=runner,
    )
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
