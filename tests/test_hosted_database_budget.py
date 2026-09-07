"""Regression coverage for the read-only Neon database budget audit."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "scripts" / "audit_hosted_database_budget.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_hosted_database_budget", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def responses() -> dict[str, object]:
    return {
        "/projects/project-1": {
            "project": {
                "id": "project-1",
                "org_id": "org-1",
                "compute_time_seconds": 123,
                "consumption_period_start": "2026-09-01T00:00:00Z",
                "consumption_period_end": "2026-10-01T00:00:00Z",
                "settings": {"quota": {"compute_time_seconds": 360000}},
                "untrusted": "SECRET-SENTINEL",
            }
        },
        "/projects/project-1/endpoints": {
            "endpoints": [
                {
                    "id": "ep-1",
                    "project_id": "project-1",
                    "branch_id": "br-1",
                    "autoscaling_limit_min_cu": 0.25,
                    "autoscaling_limit_max_cu": 1,
                    "suspend_timeout_seconds": 0,
                    "host": "SECRET-SENTINEL",
                }
            ]
        },
        "/projects/project-1/branches": {
            "branches": [
                {"id": "br-1", "project_id": "project-1", "default": True}
            ]
        },
        "/organizations/org-1/billing/spending_limit": {
            "spending_limit_cents": 500,
            "untrusted": "SECRET-SENTINEL",
        },
    }


def _runner(responses: dict[str, object], calls: list[list[str]]):
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 20,
            "check": False,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(responses[command[2]]), "")

    return run


def test_audit_reports_current_constrained_project_without_provider_details(
    responses: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    calls: list[list[str]] = []

    result = module.main(["--project-id", "project-1"], runner=_runner(responses, calls))

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    observed_at = report.pop("observedAtUTC")
    assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", observed_at)
    assert report == {
        "billingPeriod": {"end": "2026-10-01T00:00:00Z", "start": "2026-09-01T00:00:00Z"},
        "computeQuotaSeconds": 360000,
        "computeTimeSeconds": 123,
        "findings": [
            {"code": "endpoint_capacity", "maxCu": 1, "minCu": 0.25, "status": "pass"},
            {"code": "endpoint_count", "observed": 1, "status": "pass"},
            {"code": "branch_count", "observed": 1, "status": "pass"},
            {"code": "production_branch", "observed": 1, "status": "pass"},
            {"code": "spending_alert", "cents": 500, "status": "pass"},
        ],
        "monthlyComputeQuota": {
            "policyRequested": False,
            "providerEnforced": True,
            "requestedHours": None,
            "seconds": 360000,
        },
        "status": "pass",
    }
    assert calls == [
        ["neonctl", "api", "/projects/project-1", "--output", "json", "--no-analytics"],
        [
            "neonctl",
            "api",
            "/projects/project-1/endpoints",
            "--output",
            "json",
            "--no-analytics",
        ],
        [
            "neonctl",
            "api",
            "/projects/project-1/branches",
            "--output",
            "json",
            "--no-analytics",
        ],
        [
            "neonctl",
            "api",
            "/organizations/org-1/billing/spending_limit",
            "--output",
            "json",
            "--no-analytics",
        ],
    ]
    assert "SECRET-SENTINEL" not in capsys.readouterr().err


def test_audit_reports_capacity_and_inventory_drift(responses: dict[str, object]) -> None:
    module = _module()
    responses["/projects/project-1/endpoints"] = {
        "endpoints": [
            {
                "id": "ep-1",
                "project_id": "project-1",
                "branch_id": "br-1",
                "autoscaling_limit_min_cu": 0.25,
                "autoscaling_limit_max_cu": 2,
                "suspend_timeout_seconds": -1,
            },
            {
                "id": "ep-2",
                "project_id": "project-1",
                "branch_id": "br-2",
                "autoscaling_limit_min_cu": 0.25,
                "autoscaling_limit_max_cu": 1,
                "suspend_timeout_seconds": 0,
            },
        ]
    }
    responses["/projects/project-1/branches"] = {
        "branches": [
            {"id": "br-1", "project_id": "project-1", "default": True},
            {"id": "br-2", "project_id": "project-1", "default": False},
        ]
    }
    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"] if finding["status"] == "fail"} == {
        "endpoint_capacity",
        "endpoint_count",
        "branch_count",
    }


def test_audit_requires_configured_alert_and_requested_quota(
    responses: dict[str, object]
) -> None:
    module = _module()
    responses["/organizations/org-1/billing/spending_limit"] = {"spending_limit_cents": None}
    responses["/projects/project-1"]["project"]["settings"] = {
        "quota": {"compute_time_seconds": 0}
    }

    report = module.audit("project-1", monthly_compute_hours=100, runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"] if finding["status"] == "fail"} == {
        "spending_alert",
        "monthly_compute_quota",
    }
    assert report["monthlyComputeQuota"] == {
        "policyRequested": True,
        "providerEnforced": False,
        "requestedHours": 100,
        "seconds": 0,
    }


def test_missing_quota_is_reported_as_unlimited_until_a_cutoff_is_requested(
    responses: dict[str, object]
) -> None:
    module = _module()
    responses["/projects/project-1"]["project"]["settings"] = {}

    without_cutoff = module.audit("project-1", runner=_runner(responses, []))
    with_cutoff = module.audit("project-1", monthly_compute_hours=100, runner=_runner(responses, []))

    assert without_cutoff["status"] == "pass"
    assert without_cutoff["monthlyComputeQuota"] == {
        "policyRequested": False,
        "providerEnforced": False,
        "requestedHours": None,
        "seconds": None,
    }
    assert {finding["code"] for finding in with_cutoff["findings"] if finding["status"] == "fail"} == {
        "monthly_compute_quota"
    }


@pytest.mark.parametrize("settings", ["not-an-object", {"quota": []}])
def test_audit_fails_closed_on_malformed_quota_settings(
    responses: dict[str, object], settings: object
) -> None:
    module = _module()
    responses["/projects/project-1"]["project"]["settings"] = settings

    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert report["monthlyComputeQuota"] == {
        "policyRequested": False,
        "providerEnforced": None,
        "requestedHours": None,
        "seconds": None,
    }
    assert {finding["code"] for finding in report["findings"] if finding["status"] == "unknown"} >= {
        "compute_quota_unknown"
    }


def test_audit_rejects_invalid_negative_autosuspend(responses: dict[str, object]) -> None:
    module = _module()
    responses["/projects/project-1/endpoints"]["endpoints"][0]["suspend_timeout_seconds"] = -2

    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"]} >= {"endpoint_capacity_unknown"}


@pytest.mark.parametrize("pagination", [None, {"next": None}, {"next": ""}, {"next": "next-page"}])
def test_audit_rejects_incomplete_branch_pagination(
    responses: dict[str, object], pagination: object
) -> None:
    module = _module()
    responses["/projects/project-1/branches"]["pagination"] = pagination

    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"]} >= {"branch_inventory_unknown"}


def test_audit_accepts_absent_or_terminal_branch_pagination(responses: dict[str, object]) -> None:
    module = _module()

    absent = module.audit("project-1", runner=_runner(responses, []))
    responses["/projects/project-1/branches"]["pagination"] = {}
    terminal = module.audit("project-1", runner=_runner(responses, []))

    assert absent["status"] == terminal["status"] == "pass"
    assert all(finding["code"] != "branch_inventory_unknown" for finding in terminal["findings"])


def test_audit_rejects_endpoint_not_linked_to_the_only_branch(
    responses: dict[str, object]
) -> None:
    module = _module()
    responses["/projects/project-1/endpoints"]["endpoints"][0]["branch_id"] = "br-other"

    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"]} >= {"inventory_link_unknown"}


def test_audit_keeps_spending_alert_when_project_usage_is_malformed(
    responses: dict[str, object]
) -> None:
    module = _module()
    calls: list[list[str]] = []
    responses["/projects/project-1"]["project"]["compute_time_seconds"] = "not-a-number"

    report = module.audit("project-1", runner=_runner(responses, calls))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"]} >= {"project_usage_unknown"}
    assert any(finding["code"] == "spending_alert" and finding["status"] == "pass" for finding in report["findings"])
    assert calls[-1][2] == "/organizations/org-1/billing/spending_limit"


def test_audit_rejects_reversed_billing_period_but_keeps_spending_alert(
    responses: dict[str, object]
) -> None:
    module = _module()
    responses["/projects/project-1"]["project"]["consumption_period_start"] = "2026-10-01T00:00:00Z"
    responses["/projects/project-1"]["project"]["consumption_period_end"] = "2026-09-01T00:00:00Z"

    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert report["billingPeriod"] is None
    assert {finding["code"] for finding in report["findings"]} >= {"project_usage_unknown"}
    assert any(finding["code"] == "spending_alert" and finding["status"] == "pass" for finding in report["findings"])


def test_audit_reports_requested_quota_limit_separately_from_provider_quota(
    responses: dict[str, object]
) -> None:
    module = _module()

    hundred = module.audit("project-1", monthly_compute_hours=100, runner=_runner(responses, []))
    two_hundred = module.audit("project-1", monthly_compute_hours=200, runner=_runner(responses, []))

    assert hundred["monthlyComputeQuota"]["requestedHours"] == 100
    assert two_hundred["monthlyComputeQuota"]["requestedHours"] == 200
    assert hundred["computeQuotaSeconds"] == two_hundred["computeQuotaSeconds"] == 360000


def test_audit_handles_integer_too_large_for_float_conversion() -> None:
    module = _module()

    assert module._number(10**10000) is None


@pytest.mark.parametrize(
    "bad_value",
    [True, float("nan"), float("inf"), "one"],
)
def test_audit_rejects_invalid_numeric_provider_values(
    responses: dict[str, object], bad_value: object
) -> None:
    module = _module()
    responses["/projects/project-1/endpoints"]["endpoints"][0]["autoscaling_limit_max_cu"] = bad_value

    report = module.audit("project-1", runner=_runner(responses, []))

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"]} >= {"endpoint_capacity_unknown"}


def test_audit_preserves_independent_findings_after_timeout_without_leaking_errors(
    responses: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    calls: list[list[str]] = []

    def timeout_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[2].endswith("/branches"):
            raise subprocess.TimeoutExpired(command, 20, output="SECRET-SENTINEL")
        return _runner(responses, [])(command, **kwargs)

    result = module.main(["--project-id", "project-1"], runner=timeout_runner)

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert result == 1
    assert any(finding == {"code": "branch_inventory_unknown", "status": "unknown"} for finding in report["findings"])
    assert any(finding["code"] == "endpoint_capacity" and finding["status"] == "pass" for finding in report["findings"])
    assert "SECRET-SENTINEL" not in output.out + output.err
    assert len(calls) == 4


def test_audit_rejects_invalid_project_id_without_running_command() -> None:
    module = _module()
    calls: list[list[str]] = []

    report = module.audit("project-1;DELETE", runner=_runner({}, calls))

    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        report.pop("observedAtUTC"),
    )
    assert report == {
        "billingPeriod": None,
        "computeQuotaSeconds": None,
        "computeTimeSeconds": None,
        "findings": [{"code": "project_unknown", "status": "unknown"}],
        "monthlyComputeQuota": {
            "policyRequested": False,
            "providerEnforced": None,
            "requestedHours": None,
            "seconds": None,
        },
        "status": "fail",
    }
    assert calls == []
