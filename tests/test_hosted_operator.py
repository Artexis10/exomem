from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from pathlib import Path

import pytest

from exomem import __main__ as cli
from exomem import hosted_operator

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "openspec/changes/complete-hosted-runtime-deployment-contract/contracts/hosted-operator-v1.json"
)


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "operation_id": "operation-1",
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "vault_root": "/srv/exomem/vault",
        "state_root": "/srv/exomem/state",
        "log_root": "/srv/exomem/log",
        "expected_release": "0.1.0",
        "expected_protocol": "1",
        "runtime_uid": 10001,
        "runtime_gid": 10001,
        "active_credential_version": "credential-v1",
    }
    request.update(overrides)
    return request


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def test_checked_in_operator_contract_is_the_exact_cli_boundary() -> None:
    artifact = CONTRACT_PATH.read_bytes()
    assert hashlib.sha256(artifact).hexdigest() == (
        "407799e723e9d996e5ab15ca76c071c3ae497041a1096f106690712ce6fe4ca6"
    )
    contract = json.loads(artifact)

    assert contract["contract_version"] == 1
    assert contract["binding_version"] == 2
    assert contract["invocation"]["argv_by_command"] == {
        "init": [
            "exomem",
            "hosted",
            "init",
            "--contract-version",
            "1",
            "--request-file",
            "/run/exomem/operator-requests/init.json",
        ],
        "restore-candidate": [
            "exomem",
            "hosted",
            "restore-candidate",
            "--contract-version",
            "1",
            "--request-file",
            "/run/exomem/operator-requests/restore-candidate.json",
        ],
        "credential": [
            "exomem",
            "hosted",
            "credential",
            "--contract-version",
            "1",
            "--request-file",
            "-",
        ],
        "probe": [
            "exomem",
            "hosted",
            "probe",
            "--contract-version",
            "1",
            "--request-file",
            "-",
        ],
    }
    for command in contract["invocation"]["commands"]:
        schema = contract["commands"][command]
        assert set(schema["required"]) <= set(schema["properties"])
        assert schema["additional_properties"] is False
        assert set(schema["stable_errors"]) <= set(contract["stable_error_classes"])


def test_shared_decoder_rejects_duplicate_unknown_and_noncanonical_request_fields() -> None:
    duplicate = (
        b'{"request_id":"123e4567-e89b-42d3-a456-426614174000",'
        b'"request_id":"123e4567-e89b-42d3-a456-426614174000"}'
    )
    with pytest.raises(hosted_operator.OperatorFailure) as duplicate_error:
        hosted_operator.decode_request("init", duplicate)
    assert duplicate_error.value.code == "HOSTED_OPERATOR_CONTRACT_INVALID"

    with pytest.raises(hosted_operator.OperatorFailure) as unknown_error:
        hosted_operator.decode_request("init", _canonical(_request(extra="rejected")))
    assert unknown_error.value.code == "HOSTED_OPERATOR_CONTRACT_INVALID"

    with pytest.raises(hosted_operator.OperatorFailure) as runtime_error:
        hosted_operator.decode_request("init", _canonical(_request(runtime_uid=0)))
    assert runtime_error.value.code == "HOSTED_RUNTIME_ID_INVALID"


@pytest.mark.parametrize("suffix", [b"x", b"{}", b"\x00"])
def test_live_request_requires_bounded_eof_terminated_stdin(suffix: bytes) -> None:
    payload = _canonical(
        {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "operation_id": "probe-op",
            "cell_id": "cell-alpha",
            "vault_id": "vault-alpha",
            "state_root": "/srv/exomem/state",
            "selected_credential_version": "credential-v1",
            "expected_release": "0.1.0",
            "expected_protocol": "1",
            "expected_worker_policy_digest": "a" * 64,
            "expected_revision": 1,
            "port": 8080,
        }
    )
    with pytest.raises(hosted_operator.OperatorFailure) as error:
        hosted_operator.read_live_request("probe", io.BytesIO(payload + suffix))
    assert error.value.code == "HOSTED_OPERATOR_CONTRACT_INVALID"

    with pytest.raises(hosted_operator.OperatorFailure) as oversized:
        hosted_operator.read_live_request("probe", io.BytesIO(b"{" + b" " * 65536))
    assert oversized.value.code == "HOSTED_OPERATOR_CONTRACT_INVALID"


def test_live_request_reads_until_real_eof_even_when_stream_returns_short_chunks() -> None:
    payload = _canonical(
        {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "operation_id": "credential-op",
            "cell_id": "cell-alpha",
            "vault_id": "vault-alpha",
            "state_root": "/srv/exomem/state",
            "action": "abort",
            "expected_revision": 2,
        }
    )

    class ShortStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(5, size) if size >= 0 else 5)

    decoded = hosted_operator.read_live_request("credential", ShortStream(payload))
    assert decoded["operation_id"] == "credential-op"


def test_offline_request_reader_hardens_one_descriptor_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = tmp_path / "init.json"
    request.write_bytes(_canonical(_request()))
    request.chmod(0o400)
    before = request.stat()
    real_fstat = os.fstat

    def root_owned(fd: int) -> os.stat_result:
        value = real_fstat(fd)
        fields = list(value)
        fields[stat.ST_UID] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(hosted_operator.os, "fstat", root_owned)
    decoded = hosted_operator.read_offline_request(
        "init", request, expected_path=request
    )

    assert decoded["operation_id"] == "operation-1"
    assert request.stat().st_ino == before.st_ino

    request.chmod(0o600)
    with pytest.raises(hosted_operator.OperatorFailure) as writable:
        hosted_operator.read_offline_request("init", request, expected_path=request)
    assert writable.value.code == "HOSTED_OPERATOR_CONTRACT_INVALID"


def test_offline_request_rejects_wrong_owner_hardlink_symlink_and_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = tmp_path / "init.json"
    request.write_bytes(_canonical(_request()))
    request.chmod(0o400)

    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request("init", request, expected_path=request)

    real_fstat = os.fstat

    def root_owned(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd))
        fields[stat.ST_UID] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(hosted_operator.os, "fstat", root_owned)
    hardlink = tmp_path / "init-hardlink.json"
    os.link(request, hardlink)
    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request("init", request, expected_path=request)
    hardlink.unlink()

    target = tmp_path / "target.json"
    request.rename(target)
    try:
        request.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request("init", request, expected_path=request)
    request.unlink()

    request.write_bytes(b"{" + b" " * 65_536)
    request.chmod(0o400)
    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request("init", request, expected_path=request)


def test_offline_request_reads_open_generation_when_leaf_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = tmp_path / "init.json"
    original = _canonical(_request(operation_id="original-operation"))
    replacement = _canonical(_request(operation_id="replacement-operation"))
    request.write_bytes(original)
    request.chmod(0o400)
    real_open = os.open
    real_fstat = os.fstat
    opened = False

    def root_owned(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd))
        fields[stat.ST_UID] = 0
        return os.stat_result(fields)

    def replace_after_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        nonlocal opened
        descriptor = real_open(path, flags, mode)
        if not opened:
            opened = True
            old = tmp_path / "opened-generation.json"
            request.rename(old)
            request.write_bytes(replacement)
            request.chmod(0o400)
        return descriptor

    monkeypatch.setattr(hosted_operator.os, "fstat", root_owned)
    monkeypatch.setattr(hosted_operator.os, "open", replace_after_open)

    decoded = hosted_operator.read_offline_request("init", request, expected_path=request)

    assert decoded["operation_id"] == "original-operation"


def test_offline_request_rejects_relative_path_parent_symlink_and_non_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = tmp_path / "init.json"
    request.write_bytes(_canonical(_request()))
    request.chmod(0o400)
    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request(
            "init", Path("init.json"), expected_path=Path("init.json")
        )

    real = tmp_path / "real"
    real.mkdir()
    real_request = real / "init.json"
    real_request.write_bytes(_canonical(_request()))
    real_request.chmod(0o400)
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    linked_request = linked_parent / "init.json"
    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request(
            "init", linked_request, expected_path=linked_request
        )

    request.unlink()
    request.mkdir()
    real_fstat = os.fstat

    def root_owned(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd))
        fields[stat.ST_UID] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(hosted_operator.os, "fstat", root_owned)
    with pytest.raises(hosted_operator.OperatorFailure):
        hosted_operator.read_offline_request("init", request, expected_path=request)


def test_operator_main_emits_one_canonical_envelope_and_empty_stderr() -> None:
    request = {
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "operation_id": "credential-op",
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "state_root": "/srv/exomem/state",
        "action": "stage",
        "expected_revision": 1,
        "pending_version": "credential-v2",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = hosted_operator.main(
        [
            "credential",
            "--contract-version",
            "1",
            "--request-file",
            "-",
        ],
        stdin=io.BytesIO(_canonical(request)),
        stdout=stdout,
        stderr=stderr,
        handlers={
            "credential": lambda _request: (
                "HOSTED_CREDENTIAL_STAGED",
                {
                    "phase": "staged",
                    "revision": 2,
                    "active_version": "credential-v1",
                    "pending_version": "credential-v2",
                    "preferred_version": "credential-v1",
                    "rotation_id": "123e4567-e89b-42d3-a456-426614174001",
                    "proof_valid_until": None,
                },
            )
        },
    )

    assert status == 0
    assert stderr.getvalue() == ""
    raw = stdout.getvalue()
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")) + "\n"
    assert json.loads(raw) == {
        "code": "HOSTED_CREDENTIAL_STAGED",
        "command": "credential",
        "contract_version": 1,
        "data": {
            "active_version": "credential-v1",
            "pending_version": "credential-v2",
            "phase": "staged",
            "preferred_version": "credential-v1",
            "proof_valid_until": None,
            "revision": 2,
            "rotation_id": "123e4567-e89b-42d3-a456-426614174001",
        },
        "ok": True,
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
    }


def test_operator_main_redacts_modeled_and_unexpected_failures() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    status = hosted_operator.main(
        ["credential", "--contract-version", "2", "--request-file", "-"],
        stdin=io.BytesIO(b"secret-sentinel"),
        stdout=stdout,
        stderr=stderr,
    )

    assert status == 2
    assert stderr.getvalue() == ""
    envelope = json.loads(stdout.getvalue())
    assert envelope["command"] is None
    assert envelope["request_id"] is None
    assert envelope["error"] == {
        "code": "HOSTED_OPERATOR_CONTRACT_INVALID",
        "message": "hosted operator request is invalid",
        "operator_action": "fix-request",
        "retryable": False,
    }
    assert "secret-sentinel" not in stdout.getvalue()


def test_operator_main_rejects_handler_output_outside_frozen_success_schema() -> None:
    request = {
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "operation_id": "credential-op",
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "state_root": "/srv/exomem/state",
        "action": "abort",
        "expected_revision": 2,
    }
    stdout = io.StringIO()

    status = hosted_operator.main(
        ["credential", "--contract-version", "1", "--request-file", "-"],
        stdin=io.BytesIO(_canonical(request)),
        stdout=stdout,
        handlers={
            "credential": lambda _request: (
                "HOSTED_CREDENTIAL_ABORTED",
                {
                    "phase": "stable",
                    "revision": 3,
                    "active_version": "credential-v1",
                    "pending_version": None,
                    "preferred_version": "credential-v1",
                    "rotation_id": None,
                    "proof_valid_until": None,
                    "credential_plaintext": "must-never-pass-through",
                },
            )
        },
    )

    assert status == 6
    rendered = stdout.getvalue()
    assert json.loads(rendered)["error"]["code"] == "HOSTED_OPERATOR_INTERNAL"
    assert "must-never-pass-through" not in rendered


def test_top_level_cli_lazy_dispatches_only_exact_hosted_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str]] = []
    monkeypatch.setattr(hosted_operator, "main", lambda argv: received.append(argv) or 17)

    result = cli.main(
        ["hosted", "probe", "--contract-version", "1", "--request-file", "-"]
    )

    assert result == 17
    assert received == [["probe", "--contract-version", "1", "--request-file", "-"]]
