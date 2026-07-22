from __future__ import annotations

import importlib.util
import signal
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/scripts/verify_hcp_backend.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_hcp_backend", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace_document(**overrides: object) -> dict[str, object]:
    attributes: dict[str, object] = {
        "name": "exomem-hosted-foundation",
        "execution-mode": "local",
        "setting-overwrites": {"execution-mode": True},
        "vcs-repo": None,
        "auto-apply": False,
        "assessments-enabled": False,
        "global-remote-state": False,
        "project-remote-state": False,
        "terraform-version": "1.15.8",
        "locked": False,
        "permissions": {
            "can-read-state-versions": True,
            "can-create-state-versions": True,
            "can-lock": True,
            "can-unlock": True,
        },
    }
    attributes.update(overrides)
    return {"data": {"id": "ws-example", "type": "workspaces", "attributes": attributes}}


def test_validate_workspace_accepts_explicit_local_state_only_contract() -> None:
    module = _load_module()

    workspace = module.validate_workspace(
        _workspace_document(),
        expected_name="exomem-hosted-foundation",
        expected_terraform_version="1.15.8",
        require_unlocked=True,
    )

    assert workspace.workspace_id == "ws-example"
    assert workspace.name == "exomem-hosted-foundation"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"execution-mode": "remote"}, "execution mode"),
        ({"setting-overwrites": {"execution-mode": False}}, "explicit"),
        ({"vcs-repo": {"identifier": "example/repo"}}, "VCS"),
        ({"auto-apply": True}, "auto-apply"),
        ({"assessments-enabled": True}, "assessments"),
        ({"global-remote-state": True}, "remote-state sharing"),
        ({"project-remote-state": True}, "remote-state sharing"),
        ({"terraform-version": "1.15.7"}, "Terraform version"),
        ({"locked": True}, "locked"),
    ],
)
def test_validate_workspace_rejects_unsafe_hcp_settings(
    override: dict[str, object], message: str
) -> None:
    module = _load_module()

    with pytest.raises(module.BackendProofError, match=message):
        module.validate_workspace(
            _workspace_document(**override),
            expected_name="exomem-hosted-foundation",
            expected_terraform_version="1.15.8",
            require_unlocked=True,
        )


def test_validate_workspace_rejects_token_without_state_and_lock_permissions() -> None:
    module = _load_module()

    with pytest.raises(module.BackendProofError, match="permissions"):
        module.validate_workspace(
            _workspace_document(permissions={"can-read-state-versions": True}),
            expected_name="exomem-hosted-foundation",
            expected_terraform_version="1.15.8",
            require_unlocked=True,
        )


def test_stop_process_group_escalates_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()

    class FakeProcess:
        pid = 1234

        def __init__(self) -> None:
            self.communicate_calls = 0

        def poll(self):
            return None

        def communicate(self, timeout: int):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("terraform", timeout)
            return ("", "")

    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    process = FakeProcess()

    module._stop_process_group(process)

    assert signals == [(1234, signal.SIGTERM), (1234, signal.SIGKILL)]
    assert process.communicate_calls == 2


def test_unlock_retries_transient_hcp_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def unlock(self, workspace_id: str) -> None:
            assert workspace_id == "ws-example"
            self.calls += 1
            if self.calls < 3:
                raise module.HcpApiError("temporarily unavailable", status_code=503)

    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    client = FakeClient()

    module._unlock_with_retry(client, "ws-example")

    assert client.calls == 3
