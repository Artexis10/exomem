from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import doctor as doctor_module
from exomem.__main__ import main
from exomem.runtime_readiness import HTTP_TRANSPORT, RUNTIME_CONTRACT


def _set_ha_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "https://coordinator.example.com")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "main")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "desktop")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOKEN", "secret")
    monkeypatch.setenv("EXOMEM_LEASE_COORDINATOR_TOKEN", "secret")
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_URL", "https://coordinator.example.com")
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_NAMESPACE", "main")
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_TOKEN", "secret")
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "1234")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "stable-signing-root")
    monkeypatch.setattr(
        doctor_module,
        "_probe_state",
        lambda _url, _namespace, token: (401, {}) if token is None else (200, {"result": None}),
    )


def _ready(replica_id: str, release: str, *, contract: int = RUNTIME_CONTRACT) -> dict:
    return {
        "status": "ready",
        "service": "exomem",
        "release": release,
        "runtime_contract": contract,
        "transport": HTTP_TRANSPORT,
        "replica_id": replica_id,
        "coordination": {
            "enabled": True,
            "role": "writer" if replica_id == "desktop" else "follower",
            "coordinator_healthy": True,
        },
        "takeover_eligible": True,
        "reasons": [],
    }


def test_ha_profile_is_offline_by_default(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)

    def fail_network(url: str):  # noqa: ANN202
        raise AssertionError(f"HA doctor made an unexpected network call: {url}")

    monkeypatch.setattr(doctor_module, "_probe_get", fail_network)
    report = doctor_module.doctor(vault=str(vault), profile="ha")

    assert report.profile == "ha"
    checks = {check.id: check for check in report.checks}
    assert checks["ha.env.EXOMEM_WRITER_LEASE_URL"].status == "pass"
    assert not any(check_id.startswith("ha.replica.") for check_id in checks)


def test_ha_profile_reports_missing_coordination_configuration(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "EXOMEM_BASE_URL",
        "EXOMEM_JWT_SIGNING_KEY",
        "EXOMEM_WRITER_LEASE_URL",
        "EXOMEM_WRITER_LEASE_VAULT_ID",
        "EXOMEM_WRITER_LEASE_REPLICA_ID",
        "EXOMEM_WRITER_LEASE_TOKEN",
        "EXOMEM_OAUTH_STORAGE_URL",
        "EXOMEM_OAUTH_STORAGE_NAMESPACE",
        "EXOMEM_OAUTH_STORAGE_TOKEN",
        "EXOMEM_LEASE_COORDINATOR_TOKEN",
        "EXOMEM_GITHUB_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    report = doctor_module.doctor(vault=str(vault), profile="ha")
    checks = {check.id: check for check in report.checks}

    assert checks["ha.env.EXOMEM_WRITER_LEASE_URL"].status == "fail"
    assert checks["ha.env.EXOMEM_WRITER_LEASE_VAULT_ID"].status == "fail"
    assert checks["ha.env.EXOMEM_WRITER_LEASE_REPLICA_ID"].status == "fail"
    assert checks["ha.env.EXOMEM_OAUTH_STORAGE_URL"].status == "fail"
    assert checks["ha.env.EXOMEM_OAUTH_STORAGE_TOKEN"].status == "fail"
    assert checks["ha.env.EXOMEM_GITHUB_USER_ID"].status == "fail"
    assert checks["ha.env.EXOMEM_BASE_URL"].status == "fail"
    assert checks["ha.env.EXOMEM_JWT_SIGNING_KEY"].status == "fail"


def test_ha_profile_rejects_mismatched_storage_credentials(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_TOKEN", "different-secret")

    report = doctor_module.doctor(vault=str(vault), profile="ha")
    checks = {check.id: check for check in report.checks}

    assert checks["ha.auth.credentials_match"].status == "fail"
    rendered = doctor_module.render_human(report)
    assert "different-secret" not in rendered
    assert "secret" not in rendered


def test_ha_profile_requires_coordinator_token_even_when_replica_tokens_match(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    monkeypatch.delenv("EXOMEM_LEASE_COORDINATOR_TOKEN")

    checks = {
        check.id: check
        for check in doctor_module.doctor(vault=str(vault), profile="ha").checks
    }

    assert checks["ha.env.EXOMEM_LEASE_COORDINATOR_TOKEN"].status == "fail"
    assert checks["ha.auth.credentials_match"].status == "fail"


def test_ha_probe_proves_coordinator_requires_and_accepts_auth(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    calls: list[tuple[str, str, str | None]] = []

    def probe_state(url: str, namespace: str, token: str | None):
        calls.append((url, namespace, token))
        return (401, {"error": "unauthorized"}) if token is None else (200, {"result": None})

    monkeypatch.setattr(doctor_module, "_probe_state", probe_state, raising=False)
    monkeypatch.setattr(doctor_module, "_check_ha_probes", lambda _urls: [])

    report = doctor_module.doctor(
        vault=str(vault),
        profile="ha",
        probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com"],
    )
    checks = {check.id: check for check in report.checks}

    assert checks["ha.auth.anonymous_rejected"].status == "pass"
    assert checks["ha.auth.storage_credential"].status == "pass"
    assert calls == [
        ("https://coordinator.example.com", "main", None),
        ("https://coordinator.example.com", "main", "secret"),
    ]


def test_ha_probe_rejects_anonymous_state_access(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_state",
        lambda _url, _namespace, token: (200, {"result": None}),
        raising=False,
    )
    monkeypatch.setattr(doctor_module, "_check_ha_probes", lambda _urls: [])

    report = doctor_module.doctor(
        vault=str(vault), profile="ha", probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com"],
    )

    assert next(
        c for c in report.checks if c.id == "ha.auth.anonymous_rejected"
    ).status == "fail"


def test_ha_probe_requires_absent_sentinel_result(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_state",
        lambda _url, _namespace, token: (
            (401, {}) if token is None else (200, {"result": {"unexpected": True}})
        ),
    )
    monkeypatch.setattr(doctor_module, "_check_ha_probes", lambda _urls: [])

    report = doctor_module.doctor(
        vault=str(vault), profile="ha", probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com"],
    )

    assert next(
        c for c in report.checks if c.id == "ha.auth.storage_credential"
    ).status == "fail"


@pytest.mark.parametrize("status", [401, 403])
def test_ha_probe_distinguishes_rejected_storage_credential(
    vault: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _set_ha_env(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "_probe_state",
        lambda _url, _namespace, token: (
            (401, {}) if token is None else (status, {})
        ),
        raising=False,
    )
    monkeypatch.setattr(doctor_module, "_check_ha_probes", lambda _urls: [])

    report = doctor_module.doctor(
        vault=str(vault), profile="ha", probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com"],
    )
    check = next(c for c in report.checks if c.id == "ha.auth.storage_credential")

    assert check.status == "fail"
    assert "rejected" in check.message.lower()
    assert "secret" not in doctor_module.render_human(report)


def test_ha_probe_distinguishes_storage_outage(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)

    def probe_state(_url: str, _namespace: str, token: str | None):
        if token is None:
            return 401, {}
        raise ConnectionError("offline")

    monkeypatch.setattr(doctor_module, "_probe_state", probe_state, raising=False)
    monkeypatch.setattr(doctor_module, "_check_ha_probes", lambda _urls: [])
    report = doctor_module.doctor(
        vault=str(vault), profile="ha", probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com"],
    )
    check = next(c for c in report.checks if c.id == "ha.auth.storage_credential")

    assert check.status == "fail"
    assert "reach" in check.message.lower()


def test_ha_probe_accepts_compatible_release_drift(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    responses = {
        "https://desktop.example.com/health/ready": (200, _ready("desktop", "0.20.1")),
        "https://laptop.example.com/health/ready": (200, _ready("laptop", "0.20.2")),
    }
    monkeypatch.setattr(doctor_module, "_probe_get", lambda url: responses[url])

    report = doctor_module.doctor(
        vault=str(vault),
        profile="ha",
        probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com/"],
    )
    checks = {check.id: check for check in report.checks}

    assert checks["ha.replica.1"].status == "pass"
    assert checks["ha.replica.2"].status == "pass"
    assert checks["ha.compatibility"].status == "pass"
    assert checks["ha.release_drift"].status == "warn"
    assert report.success is True


def test_ha_probe_rejects_incompatible_or_duplicate_replicas(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    responses = {
        "https://desktop.example.com/health/ready": (200, _ready("desktop", "0.20.1")),
        "https://laptop.example.com/health/ready": (
            200,
            _ready("desktop", "0.21.0", contract=RUNTIME_CONTRACT + 1),
        ),
    }
    monkeypatch.setattr(doctor_module, "_probe_get", lambda url: responses[url])

    report = doctor_module.doctor(
        vault=str(vault),
        profile="ha",
        probe=True,
        replica_urls=["https://desktop.example.com", "https://laptop.example.com"],
    )
    checks = {check.id: check for check in report.checks}

    assert checks["ha.replica.2"].status == "fail"
    assert checks["ha.compatibility"].status == "fail"
    assert report.success is False


def test_ha_probe_requires_explicit_replica_urls(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_ha_env(monkeypatch)
    monkeypatch.delenv("EXOMEM_HA_REPLICA_URLS", raising=False)
    report = doctor_module.doctor(vault=str(vault), profile="ha", probe=True)
    checks = {check.id: check for check in report.checks}

    assert checks["ha.replica_urls"].status == "fail"


def test_ha_cli_passes_repeatable_replica_urls(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    seen = {}

    def fake_doctor(**kwargs):  # noqa: ANN202
        seen.update(kwargs)
        return doctor_module.DoctorReport(profile="ha", checks=[])

    monkeypatch.setattr(doctor_module, "doctor", fake_doctor)
    code = main(
        [
            "doctor",
            "--profile",
            "ha",
            "--vault",
            str(vault),
            "--replica-url",
            "https://desktop.example.com",
            "--replica-url",
            "https://laptop.example.com",
            "--json",
        ]
    )

    assert code == 0
    assert seen["replica_urls"] == [
        "https://desktop.example.com",
        "https://laptop.example.com",
    ]
    assert json.loads(capsys.readouterr().out)["profile"] == "ha"
