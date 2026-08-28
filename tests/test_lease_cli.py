"""`exomem lease status|schema-admission|release` — ops-only CLI (R6). Not an MCP/REST product
command. Exercises the CLI's own argument handling, --yes gate, --json
output, and exit codes against a FakeClient (fast, deterministic); the
coordinator's own release-on-behalf-of contract is proven separately in
tests/test_lease_coordinator.py against the real ASGI app.
"""

from __future__ import annotations

import json

import pytest

from exomem import writer_lease
from exomem.__main__ import main
from exomem.cli_ops import OpError
from exomem.writer_lease import LeaseRecord, SchemaAdmission


class FakeCoordinatorClient:
    def __init__(self, config) -> None:  # noqa: ANN001
        self.config = config

    def status(self) -> LeaseRecord:
        return _STATE["record"]

    def release_holder(self, holder_replica_id: str, fencing_token: int) -> LeaseRecord:
        _STATE["release_calls"].append((holder_replica_id, fencing_token))
        record = _STATE["record"]
        if record.holder == holder_replica_id and record.fencing_token == fencing_token:
            _STATE["record"] = LeaseRecord(None, None, fencing_token, True)
            return _STATE["record"]
        return LeaseRecord(record.holder, record.expires_at, record.fencing_token, False)

    def schema_admission(self, schema_version: int) -> SchemaAdmission:
        required = _STATE["required_schema"]
        return SchemaAdmission(
            admitted=schema_version == required,
            governance_enrolled=True,
            required_schema_version=required,
            schema_fence_generation=7,
        )


_STATE: dict = {}


@pytest.fixture(autouse=True)
def _lease_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "https://lease.example")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "main")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "desktop")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setattr(writer_lease, "LeaseCoordinatorClient", FakeCoordinatorClient)
    _STATE["record"] = LeaseRecord("laptop", 999999999.0, 3, True)
    _STATE["release_calls"] = []
    _STATE["required_schema"] = 4
    yield


def test_lease_status_prints_role_and_holder(capsys: pytest.CaptureFixture) -> None:
    assert main(["lease", "status"]) == 0
    out = capsys.readouterr().out
    assert "role: follower" in out
    assert "holder: laptop" in out
    assert "fencing_token: 3" in out


def test_lease_status_json_is_the_full_manager_status_shape(capsys: pytest.CaptureFixture) -> None:
    assert main(["lease", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["holder"] == "laptop"
    assert payload["fencing_token"] == 3
    assert "idempotency" in payload
    assert set(payload["idempotency"]) == {"pending", "abandoned", "oldest_pending_age_seconds"}


def test_lease_release_without_yes_is_refused_and_shows_holder(
    capsys: pytest.CaptureFixture,
) -> None:
    code = main(["lease", "release"])
    assert code == 2
    assert _STATE["release_calls"] == []
    err = capsys.readouterr().err
    assert "laptop" in err
    assert "--yes" in err


def test_lease_release_with_yes_releases_the_foreign_holder(
    capsys: pytest.CaptureFixture,
) -> None:
    code = main(["lease", "release", "--yes"])
    assert code == 0
    assert _STATE["release_calls"] == [("laptop", 3)]
    out = capsys.readouterr().out
    assert "released" in out
    assert "laptop" in out


def test_lease_release_with_yes_json_output(capsys: pytest.CaptureFixture) -> None:
    code = main(["lease", "release", "--yes", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"released": True, "previous_holder": "laptop"}


def test_lease_release_is_a_no_op_when_unheld(capsys: pytest.CaptureFixture) -> None:
    _STATE["record"] = LeaseRecord(None, None, 0, False)
    code = main(["lease", "release", "--yes"])
    assert code == 0
    assert _STATE["release_calls"] == []
    out = capsys.readouterr().out
    assert "nothing to release" in out


def test_lease_release_json_no_op_when_unheld(capsys: pytest.CaptureFixture) -> None:
    _STATE["record"] = LeaseRecord(None, None, 0, False)
    code = main(["lease", "release", "--yes", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"released": False, "reason": "unheld"}


def test_lease_status_unauthorized_coordinator_reports_clean_error(
    capsys: pytest.CaptureFixture,
) -> None:
    class UnauthorizedClient(FakeCoordinatorClient):
        def status(self) -> LeaseRecord:
            raise OpError("WRITER_COORDINATOR_UNAVAILABLE", "coordinator refused the request")

    import exomem.writer_lease as writer_lease_module

    writer_lease_module.LeaseCoordinatorClient = UnauthorizedClient
    code = main(["lease", "release"])
    assert code == 1
    err = capsys.readouterr().err
    assert "WRITER_COORDINATOR_UNAVAILABLE" in err


def test_lease_disabled_when_no_coordinator_url_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_URL", raising=False)
    code = main(["lease", "status"])
    assert code == 1
    assert "not configured" in capsys.readouterr().err


def test_lease_steal_and_force_acquire_are_not_offered(capsys: pytest.CaptureFixture) -> None:
    """R6 deliberately excludes steal/force-acquire — release plus preferred
    reclaim already hands over within roughly one lease TTL."""
    with pytest.raises(SystemExit) as exc:
        main(["lease", "steal"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_lease_schema_admission_is_an_exit_code_gate_for_deployment(
    capsys: pytest.CaptureFixture,
) -> None:
    assert main(["lease", "schema-admission", "--schema-version", "3", "--json"]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused == {
        "admitted": False,
        "governance_enrolled": True,
        "required_schema_version": 4,
        "schema_fence_generation": 7,
    }

    assert main(["lease", "schema-admission", "--schema-version", "4", "--json"]) == 0
    admitted = json.loads(capsys.readouterr().out)
    assert admitted["admitted"] is True
