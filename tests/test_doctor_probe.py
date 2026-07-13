"""`exomem doctor --profile remote --probe` contract.

`--probe` is an opt-in flag on `doctor(profile="remote")` (see
openspec/changes/remote-connector-quickstart) that adds three read-only HTTP
checks behind a module-level `_probe_get(url) -> tuple[int, dict | str]` seam
that this suite monkeypatches — no real network call is ever made here,
matching the httpx-mock precedent in tests/test_auth_cache.py.

Written as TDD-red tests ahead of the `--probe` implementation: if `doctor()`
does not yet accept a `probe` keyword, every `probe=True` call below raises a
clean `TypeError` (a real test failure, not a collection/import error) rather
than assuming the feature exists. They turn green once `_probe_get` and the
`probe` keyword are wired through `doctor()`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from exomem import doctor as doctor_module

BASE_URL = "https://kb.example.com"
LOCAL_MCP_URL = "http://127.0.0.1:8765/mcp"
OAUTH_DISCOVERY_URL = f"{BASE_URL}/.well-known/oauth-authorization-server"
PROTECTED_RESOURCE_URL = f"{BASE_URL}/.well-known/oauth-protected-resource"


def _set_remote_env(monkeypatch: pytest.MonkeyPatch, *, base_url: str | None = BASE_URL) -> None:
    if base_url is None:
        monkeypatch.delenv("EXOMEM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_BASE_URL", base_url)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("EXOMEM_GITHUB_USERNAME", "octocat")
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "1234")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "test-signing-key")


def test_remote_doctor_requires_positive_immutable_github_id(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_remote_env(monkeypatch)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "not-numeric")

    checks = {
        check.id: check
        for check in doctor_module.doctor(vault=str(vault), profile="remote").checks
    }

    assert checks["env.EXOMEM_GITHUB_USER_ID"].status == "fail"
    assert "positive" in (checks["env.EXOMEM_GITHUB_USER_ID"].remediation or "")


def _make_probe_get(
    responses: dict[str, tuple[int, dict | str] | BaseException],
) -> Callable[[str], tuple[int, dict | str]]:
    """Build a fake `_probe_get(url)` driven by a url -> (status, body)|exception map.

    Any url not present in `responses` raises a `ConnectionError` rather than an
    assertion failure, so a probe check hitting an unanticipated url still
    exercises the "network error -> failing DoctorCheck, never a raise" contract
    instead of crashing the test harness itself.
    """

    def _probe_get(url: str) -> tuple[int, dict | str]:
        result = responses.get(url, ConnectionError(f"no fake response configured for {url!r}"))
        if isinstance(result, BaseException):
            raise result
        return result

    return _probe_get


def test_doctor_probe_default_off_makes_no_network_calls(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`probe` defaults to False; doctor() must never touch the `_probe_get` seam."""
    _set_remote_env(monkeypatch)

    def _boom(url: str) -> tuple[int, dict | str]:
        raise AssertionError(f"_probe_get must not be called when probe=False (url={url})")

    monkeypatch.setattr(doctor_module, "_probe_get", _boom, raising=False)

    report = doctor_module.doctor(vault=str(vault), profile="remote")

    assert report.profile == "remote"
    assert not any(c.id.startswith("probe.") for c in report.checks)


def test_doctor_probe_true_all_endpoints_healthy(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario: remote probe confirms the live endpoint triple."""
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: (401, "unauthorized"),
                OAUTH_DISCOVERY_URL: (200, {"issuer": f"{BASE_URL}/"}),
                PROTECTED_RESOURCE_URL: (200, {"resource": f"{BASE_URL}/mcp"}),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    assert checks["probe.local_mcp"].status == "pass"
    assert checks["probe.oauth_discovery"].status == "pass"
    assert checks["probe.protected_resource"].status == "pass"


def test_doctor_probe_local_mcp_200_is_a_misconfiguration(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 (instead of 401) on the local /mcp endpoint means auth is not enforced."""
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: (200, "ok"),
                OAUTH_DISCOVERY_URL: (200, {"issuer": f"{BASE_URL}/"}),
                PROTECTED_RESOURCE_URL: (200, {"resource": f"{BASE_URL}/mcp"}),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    assert checks["probe.local_mcp"].status == "fail"


def test_doctor_probe_local_mcp_connection_error_is_actionable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: ConnectionError("connection refused"),
                OAUTH_DISCOVERY_URL: (200, {"issuer": f"{BASE_URL}/"}),
                PROTECTED_RESOURCE_URL: (200, {"resource": f"{BASE_URL}/mcp"}),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    probe_check = checks["probe.local_mcp"]
    assert probe_check.status == "fail"
    remediation = (probe_check.remediation or "").lower()
    assert "server" in remediation or "service" in remediation


def test_doctor_probe_oauth_discovery_non_200_fails_with_tunnel_remediation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: (401, "unauthorized"),
                OAUTH_DISCOVERY_URL: (500, "internal error"),
                PROTECTED_RESOURCE_URL: (200, {"resource": f"{BASE_URL}/mcp"}),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    probe_check = checks["probe.oauth_discovery"]
    assert probe_check.status == "fail"
    assert "tunnel" in (probe_check.remediation or "").lower()


def test_doctor_probe_oauth_discovery_connection_error_fails_with_tunnel_remediation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: (401, "unauthorized"),
                OAUTH_DISCOVERY_URL: ConnectionError("dns failure"),
                PROTECTED_RESOURCE_URL: (200, {"resource": f"{BASE_URL}/mcp"}),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    probe_check = checks["probe.oauth_discovery"]
    assert probe_check.status == "fail"
    assert "tunnel" in (probe_check.remediation or "").lower()


def test_doctor_probe_protected_resource_404_names_mcp_registration_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario: remote probe catches the bare well-known 404 that breaks connector
    registration."""
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: (401, "unauthorized"),
                OAUTH_DISCOVERY_URL: (200, {"issuer": f"{BASE_URL}/"}),
                PROTECTED_RESOURCE_URL: (404, "not found"),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    probe_check = checks["probe.protected_resource"]
    assert probe_check.status == "fail"
    # `mcp_registration_failed` is named in whichever field explains the failure
    # mode (message or remediation) — the check's job is to name it somewhere.
    assert "mcp_registration_failed" in probe_check.message or "mcp_registration_failed" in (
        probe_check.remediation or ""
    )


def test_doctor_probe_protected_resource_mismatch_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_remote_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get(
            {
                LOCAL_MCP_URL: (401, "unauthorized"),
                OAUTH_DISCOVERY_URL: (200, {"issuer": f"{BASE_URL}/"}),
                PROTECTED_RESOURCE_URL: (200, {"resource": "https://wrong-host.example.com/mcp"}),
            }
        ),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    checks = {c.id: c for c in report.checks}
    assert checks["probe.protected_resource"].status == "fail"


def test_doctor_probe_missing_base_url_does_not_crash(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`EXOMEM_BASE_URL` unset must never crash `--probe`; the two hostname-dependent
    probes fail (or are skipped) alongside the existing `env.EXOMEM_BASE_URL`
    failure."""
    _set_remote_env(monkeypatch, base_url=None)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get",
        _make_probe_get({LOCAL_MCP_URL: (401, "unauthorized")}),
        raising=False,
    )

    report = doctor_module.doctor(vault=str(vault), profile="remote", probe=True)

    assert isinstance(report, doctor_module.DoctorReport)
    checks = {c.id: c for c in report.checks}
    assert checks["env.EXOMEM_BASE_URL"].status == "fail"
    for probe_id in ("probe.oauth_discovery", "probe.protected_resource"):
        if probe_id in checks:
            assert checks[probe_id].status == "fail"
