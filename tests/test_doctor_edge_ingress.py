"""`exomem doctor`'s `edge-ingress` section (design.md Decision 3): verifies the
public apex is fronted by the HA edge worker rather than tunnel-direct.
Network probes are stubbed at the same module-level seams as the `ha` profile
tests (`_probe_get`, `_probe_get_authorized`, `urllib.request.urlopen`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import doctor as doctor_module

_COORDINATOR_URL = "https://coordinator.example.com"
_VAULT_ID = "main"
_REPLICA_ID = "desktop"
_TOKEN = "secret"
_BASE_URL = "https://kb.example.com"
_LEASE_URL = f"{_BASE_URL}/v1/vaults/{_VAULT_ID}/lease"
_READY_URL = f"{_BASE_URL}/health/ready"


def _set_edge_ingress_env(monkeypatch: pytest.MonkeyPatch, *, ttl: str | None = None) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", _COORDINATOR_URL)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", _VAULT_ID)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", _REPLICA_ID)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOKEN", _TOKEN)
    monkeypatch.setenv("EXOMEM_BASE_URL", _BASE_URL)
    if ttl is not None:
        monkeypatch.setenv("EXOMEM_WRITER_LEASE_TTL", ttl)
    else:
        monkeypatch.delenv("EXOMEM_WRITER_LEASE_TTL", raising=False)


def _valid_version_payload(**overrides: object) -> dict:
    payload: dict = {
        "service": "exomem-ha-edge",
        "git_sha": "abc1234",
        "deployed_vars": {
            "MCP_TOOL_TIMEOUT_MS": 60000,
            "ORIGIN_TIMEOUT_MS": 2500,
            "REQUIRE_COORDINATION": True,
            "SUPPORTED_RUNTIME_CONTRACTS": "1",
            "DESKTOP_REPLICA_ID": "desktop",
            "LAPTOP_REPLICA_ID": "laptop",
            "DESKTOP_ORIGIN": "https://desktop.example.com",
            "LAPTOP_ORIGIN": "https://laptop.example.com",
        },
    }
    payload.update(overrides)
    return payload


def _stub_lease_status(monkeypatch: pytest.MonkeyPatch, *, holder: str) -> None:
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"holder": holder, "expires_at": 4102444800.0, "fencing_token": 7}
            ).encode("utf-8")

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: _Response(), raising=True
    )


def _apply_probe_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lease_response: tuple[int, object] = (401, {"error": "unauthorized"}),
    version_response: tuple[int, object] | None = None,
    ready_response: tuple[int, object] = (200, {"replica_id": _REPLICA_ID}),
    lease_holder: str = _REPLICA_ID,
) -> None:
    """Stub the three network seams the probe checks use: unauthenticated GET,
    authenticated GET, and the coordinator status client."""
    if version_response is None:
        version_response = (200, _valid_version_payload())

    def fake_probe_get(url: str) -> tuple[int, object]:
        if url == _LEASE_URL:
            return lease_response
        if url == _READY_URL:
            return ready_response
        raise AssertionError(f"unexpected _probe_get url: {url}")

    monkeypatch.setattr(doctor_module, "_probe_get", fake_probe_get)
    monkeypatch.setattr(
        doctor_module,
        "_probe_get_authorized",
        lambda url, token: version_response,  # noqa: ARG005
    )
    _stub_lease_status(monkeypatch, holder=lease_holder)


def _checks_by_id(report: doctor_module.DoctorReport) -> dict[str, doctor_module.DoctorCheck]:
    return {c.id: c for c in report.checks}


# --------------------------------------------------------------------------- #
# Gating: skipped when disabled, offline unless --probe
# --------------------------------------------------------------------------- #
def test_edge_ingress_section_skipped_when_lease_disabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_URL", raising=False)

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)

    assert not any(c.id.startswith("edge_ingress.") for c in report.checks)


def test_edge_ingress_is_offline_by_default(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)

    def fail_network(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("edge-ingress doctor made an unexpected network call")

    monkeypatch.setattr(doctor_module, "_probe_get", fail_network)
    monkeypatch.setattr(doctor_module, "_probe_get_authorized", fail_network)

    report = doctor_module.doctor(vault=str(vault), profile="lean")
    checks = _checks_by_id(report)

    assert checks["edge_ingress.lease_ttl"].status == "pass"
    assert "edge_ingress.worker_fronting" not in checks
    assert "edge_ingress.provenance" not in checks
    assert "edge_ingress.read_routing" not in checks


# --------------------------------------------------------------------------- #
# Config lint: lease TTL floor
# --------------------------------------------------------------------------- #
def test_edge_ingress_lease_ttl_warns_below_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch, ttl="10")

    report = doctor_module.doctor(vault=str(vault), profile="lean")
    check = _checks_by_id(report)["edge_ingress.lease_ttl"]

    assert check.status == "warn"
    assert "30" in check.message


def test_edge_ingress_lease_ttl_passes_at_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch, ttl="30")

    report = doctor_module.doctor(vault=str(vault), profile="lean")
    check = _checks_by_id(report)["edge_ingress.lease_ttl"]

    assert check.status == "pass"


def test_edge_ingress_config_error_reports_a_single_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", _COORDINATOR_URL)
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_VAULT_ID", raising=False)
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_REPLICA_ID", raising=False)

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    edge_checks = [c for c in report.checks if c.id.startswith("edge_ingress.")]

    assert [c.id for c in edge_checks] == ["edge_ingress.config"]
    assert edge_checks[0].status == "fail"


def test_edge_ingress_base_url_missing_fails_before_probing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    monkeypatch.delenv("EXOMEM_BASE_URL", raising=False)

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    checks = _checks_by_id(report)

    assert checks["edge_ingress.base_url"].status == "fail"
    assert "edge_ingress.worker_fronting" not in checks


# --------------------------------------------------------------------------- #
# Check 1: worker-shaped 401 on the unauthenticated coordinator path
# --------------------------------------------------------------------------- #
def test_edge_ingress_probes_all_pass_on_a_healthy_worker_fronted_apex(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    _apply_probe_stubs(monkeypatch)

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    checks = _checks_by_id(report)

    assert checks["edge_ingress.worker_fronting"].status == "pass"
    assert checks["edge_ingress.provenance"].status == "pass"
    assert checks["edge_ingress.read_routing"].status == "pass"
    assert report.success is True


def test_edge_ingress_worker_fronting_fails_when_apex_is_tunnel_direct(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    # A tunnel-direct origin 404s the worker-only coordinator path instead of
    # answering the worker's `{"error": "unauthorized"}` 401 shape.
    _apply_probe_stubs(monkeypatch, lease_response=(404, {"error": "not found"}))

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.worker_fronting"]

    assert check.status == "fail"
    assert check.remediation is not None
    assert "tunnel" in check.remediation.lower()


def test_edge_ingress_worker_fronting_fails_on_unexpected_401_body(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    _apply_probe_stubs(monkeypatch, lease_response=(401, {"detail": "unauthorized"}))

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)

    assert _checks_by_id(report)["edge_ingress.worker_fronting"].status == "fail"


def test_edge_ingress_worker_fronting_fails_on_network_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)

    def raise_probe(_url: str):
        raise ConnectionError("offline")

    monkeypatch.setattr(doctor_module, "_probe_get", raise_probe)
    monkeypatch.setattr(
        doctor_module, "_probe_get_authorized", lambda url, token: (200, _valid_version_payload())  # noqa: ARG005
    )
    _stub_lease_status(monkeypatch, holder=_REPLICA_ID)

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.worker_fronting"]

    assert check.status == "fail"
    assert "reach" in check.message.lower()


# --------------------------------------------------------------------------- #
# Check 2: authenticated /__version + deploy provenance drift
# --------------------------------------------------------------------------- #
def test_edge_ingress_provenance_fails_on_missing_endpoint(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    _apply_probe_stubs(monkeypatch, version_response=(404, {"error": "not found"}))

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)

    assert _checks_by_id(report)["edge_ingress.provenance"].status == "fail"


def test_edge_ingress_provenance_warns_on_unlabeled_git_sha(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    _apply_probe_stubs(
        monkeypatch, version_response=(200, _valid_version_payload(git_sha="unlabeled"))
    )

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.provenance"]

    assert check.status == "warn"
    assert "git_sha" in check.message.lower()
    assert check.details is not None
    assert check.details["git_sha"] == "unlabeled"
    assert report.success is True  # warnings never fail the report


def test_edge_ingress_provenance_warns_on_timeout_below_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    payload = _valid_version_payload()
    payload["deployed_vars"]["MCP_TOOL_TIMEOUT_MS"] = 15000
    _apply_probe_stubs(monkeypatch, version_response=(200, payload))

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.provenance"]

    assert check.status == "warn"
    assert "MCP_TOOL_TIMEOUT_MS" in check.message


def test_edge_ingress_provenance_warns_when_coordination_not_required(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    payload = _valid_version_payload()
    payload["deployed_vars"]["REQUIRE_COORDINATION"] = False
    _apply_probe_stubs(monkeypatch, version_response=(200, payload))

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.provenance"]

    assert check.status == "warn"
    assert "REQUIRE_COORDINATION" in check.message


def test_edge_ingress_provenance_warns_on_missing_replica_origin_mapping(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    payload = _valid_version_payload()
    payload["deployed_vars"]["DESKTOP_ORIGIN"] = ""
    _apply_probe_stubs(monkeypatch, version_response=(200, payload))

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.provenance"]

    assert check.status == "warn"
    assert "desktop" in check.message.lower()


# --------------------------------------------------------------------------- #
# Check 3: public /health/ready agrees with the coordinator's lease holder
# --------------------------------------------------------------------------- #
def test_edge_ingress_read_routing_fails_on_replica_mismatch(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_edge_ingress_env(monkeypatch)
    _apply_probe_stubs(
        monkeypatch,
        ready_response=(200, {"replica_id": "laptop"}),
        lease_holder="desktop",
    )

    report = doctor_module.doctor(vault=str(vault), profile="lean", probe=True)
    check = _checks_by_id(report)["edge_ingress.read_routing"]

    assert check.status == "fail"
    assert "laptop" in check.message
    assert "desktop" in check.message
    assert report.success is False
