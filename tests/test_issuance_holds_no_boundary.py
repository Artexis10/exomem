"""Credential issuance needs no vault boundary, so a held boundary does not refuse it.

`transfer_artifact` upload and download mint a bearer capability, and `govern_memory`
session open and rotate write only the authorization-session store inside its own SQLite
transaction. None of them writes a vault-owned file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from held_boundary import held_boundary
from starlette.testclient import TestClient
from test_authorization_session_wire_reconnect import (
    _build_replica,
    _call,
    _configure_v4_authority,
    _reset_writer_state,  # noqa: F401  (autouse fixture)
)

_SESSION = {"operation": "session", "ttl_seconds": 600}


def _issued_bearer(terminal: dict) -> str:
    credential = terminal.get("issued_credential") or terminal["diagnostics"]["issued_credential"]
    return credential["bearer"]


@pytest.fixture
def custody(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_v4_authority(vault, tmp_path / "custody", monkeypatch)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "issuance-upload-secret")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://exomem.example")
    # A manager reads its acquire timeout when built, and the server is built before the hold.
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "0.1")
    # The holder and the server must share one lock directory to contend.
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "writer-lease-state"))


@pytest.mark.usefixtures("custody")
def test_transfer_artifact_issues_while_another_writer_holds_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(_build_replica(vault, monkeypatch)) as client:
        with held_boundary(vault, monkeypatch, release_when_refused=False, expect_refusal=False):
            upload = _call(client, 1, {"operation": "upload"}, tool_name="transfer_artifact")
            download = _call(client, 2, {"operation": "download"}, tool_name="transfer_artifact")

    assert upload["upload_url"] == "https://exomem.example/upload"
    assert download["download_url"] == "https://exomem.example/download"


@pytest.mark.usefixtures("custody")
def test_a_session_opens_and_rotates_while_another_writer_holds_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(_build_replica(vault, monkeypatch)) as client:
        with held_boundary(vault, monkeypatch, release_when_refused=False, expect_refusal=False):
            opened = _call(client, 1, {**_SESSION, "session_action": "open"})
            rotated = _call(
                client,
                2,
                {
                    **_SESSION,
                    "session_action": "rotate",
                    "authorization_session_credential": _issued_bearer(opened),
                },
            )

    assert _issued_bearer(rotated) != _issued_bearer(opened)
