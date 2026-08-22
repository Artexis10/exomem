"""CLI session authority is admitted only through a protected descriptor."""

from __future__ import annotations

import json
import os

import pytest

from exomem import cli_ops, product_invoke, writer_lease
from exomem.__main__ import _core_op_main

BEARER = (
    "as1.AQEBAQEBAQEBAQEBAQEBAQ."
    "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
)


@pytest.fixture(autouse=True)
def _isolated_writer_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease-state")
    )
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def test_invalid_fd_credential_wins_before_cli_value_coercion(
    vault,
    capsys: pytest.CaptureFixture[str],
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"not-a-session-bearer")
        os.close(write_fd)
        write_fd = -1

        code = _core_op_main(
            [
                "ask_memory",
                "--json",
                "--authorization-session-fd",
                str(read_fd),
                "--limit",
                "malformed",
            ]
        )
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }
    assert "malformed" not in json.dumps(payload)
    assert "not-a-session-bearer" not in json.dumps(payload)


def test_invalid_fd_credential_wins_before_cli_required_argument_validation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"not-a-session-bearer")
        os.close(write_fd)
        write_fd = -1

        code = _core_op_main(
            [
                "govern_memory",
                "--json",
                "--authorization-session-fd",
                str(read_fd),
            ]
        )
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }


def test_unknown_canonical_fd_credential_wins_before_cli_validation(
    vault,
    capsys: pytest.CaptureFixture[str],
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, BEARER.encode())
        os.close(write_fd)
        write_fd = -1

        code = _core_op_main(
            [
                "ask_memory",
                "--json",
                "--authorization-session-fd",
                str(read_fd),
                "--limit",
                "malformed",
            ]
        )
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }
    assert "malformed" not in captured.out
    assert BEARER not in captured.out


def test_unknown_canonical_fd_credential_wins_before_required_subcommand_arguments(
    vault,
    capsys: pytest.CaptureFixture[str],
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, BEARER.encode())
        os.close(write_fd)
        write_fd = -1

        code = _core_op_main(
            [
                "govern_memory",
                "--json",
                "--authorization-session-fd",
                str(read_fd),
            ]
        )
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }
    assert BEARER not in captured.out


def test_cli_environment_bearer_is_forbidden_without_echo(
    vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("EXOMEM_AUTHORIZATION_SESSION_CREDENTIAL", BEARER)

    code = _core_op_main(["browse_memory", "--json", "--mode", "list"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == (
        "AUTHORIZATION_SESSION_UNAVAILABLE"
    )
    assert BEARER not in captured.out


def test_cli_help_exposes_fd_carrier_but_no_literal_bearer_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        _core_op_main(["ask_memory", "--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--authorization-session-fd" in help_text
    assert "--authorization-session-credential" not in help_text


def test_literal_cli_bearer_option_refuses_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "as1." + "A" * 22 + "." + "B" * 43

    code = _core_op_main(
        [
            "ask_memory",
            "--json",
            "--authorization-session-credential",
            secret,
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert secret not in captured.out
    assert secret not in captured.err
    assert json.loads(captured.out)["error"]["code"] == (
        "AUTHORIZATION_SESSION_UNAVAILABLE"
    )


def test_unbound_prepared_library_invocation_fails_closed(vault) -> None:
    command = product_invoke.product_command("browse_memory")
    kwargs = cli_ops.coerce(command.params, {"mode": "list"}, tool=command.name)

    with pytest.raises(RuntimeError, match="authorization session is unavailable"):
        product_invoke.invoke_prepared(command, kwargs, vault_root=vault)
