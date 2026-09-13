"""Operator tests use a private fake control socket and isolated release root."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="managed service operator is Linux-only")

ROOT = Path(__file__).resolve().parents[1]


def _operator(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    variables = os.environ.copy()
    variables["PYTHONPATH"] = str(ROOT / "src")
    variables["EXOMEM_STATE_ROOT"] = str(tmp_path / "state")
    if env:
        variables.update(env)
    return subprocess.run(
        [sys.executable, "-m", "exomem.service_upgrade", "--runtime-dir", str(tmp_path / "managed"), *args],
        cwd=ROOT,
        env=variables,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _control(tmp_path: Path, *, phase: str = "ready", count: int | None = None) -> tuple[list[dict], threading.Thread]:
    runtime = tmp_path / "managed"
    runtime.mkdir(mode=0o700)
    launcher = tmp_path / "launcher" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    requests: list[dict] = []
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(runtime / "control.sock"))
            listener.listen(4)
            listener.settimeout(3)
            ready.set()
            while len(requests) < (count if count is not None else (2 if phase == "ready" else 1)):
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    return
                with client:
                    payload = client.makefile("rb").readline()
                    request = json.loads(payload)
                    requests.append(request)
                    if request["command"] == "upgrade":
                        response = {"ok": True, "phase": "ready", "active": request["target"]}
                    else:
                        response = {
                            "ok": True,
                            "phase": phase,
                            "active": {"python": str(tmp_path / "launcher" / "bin" / "python"), "version": "0.1.0"},
                            "launcher_python": str(tmp_path / "launcher" / "bin" / "python"),
                            "unit": "exomem.service",
                            "port": 8765,
                        }
                    client.sendall((json.dumps(response) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return requests, thread


def test_status_uses_private_control_socket(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path, phase="unavailable")
    result = _operator(tmp_path, "--status")
    thread.join(3)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["phase"] == "unavailable"
    assert requests == [{"command": "status"}]


def test_upgrade_stages_immutable_release_before_sending_target(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path)
    fake_uv = tmp_path / "uv"
    trace = tmp_path / "uv.trace"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_UV_TRACE\"\n"
        "if [ \"$1\" = venv ]; then mkdir -p \"$4/bin\"; "
        "printf '#!/bin/sh\\necho 0.2.0\\n' > \"$4/bin/python\"; chmod +x \"$4/bin/python\"; fi\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    result = _operator(
        tmp_path,
        "--package-version", "0.2.0", "--profile", "lean",
        env={"FAKE_UV_TRACE": str(trace), "EXOMEM_UV": str(fake_uv)},
    )
    thread.join(3)
    assert result.returncode == 0, result.stderr
    assert [item["command"] for item in requests] == ["status", "upgrade"]
    target = requests[1]["target"]
    assert target["python"].startswith(str(tmp_path / "releases"))
    assert target["version"] == "0.2.0"
    assert "pip install" in trace.read_text(encoding="utf-8")
    assert str(tmp_path / "launcher") not in trace.read_text(encoding="utf-8").splitlines()[-1]


def test_unavailable_manager_fails_before_creating_release(tmp_path: Path) -> None:
    result = _operator(tmp_path, "--package-version", "0.2.0")
    assert result.returncode != 0
    assert not (tmp_path / "releases").exists()


def test_staging_failure_never_requests_handoff(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path, count=1)
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_uv.chmod(0o700)
    result = _operator(tmp_path, "--package-version", "0.2.0", env={"EXOMEM_UV": str(fake_uv)})
    thread.join(3)
    assert result.returncode != 0
    assert requests == [{"command": "status"}]


def test_operator_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    runtime = tmp_path / "managed"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    outside.chmod(0o644)
    (runtime / "operator.lock").symlink_to(outside)
    result = _operator(tmp_path, "--package-version", "0.2.0")
    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert outside.stat().st_mode & 0o777 == 0o644


def test_stage_failure_does_not_echo_package_manager_output(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path, count=1)
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\necho private-registry-credential >&2\nexit 2\n", encoding="utf-8")
    fake_uv.chmod(0o700)
    result = _operator(tmp_path, env={"EXOMEM_UV": str(fake_uv)})
    thread.join(3)
    assert result.returncode != 0
    assert requests == [{"command": "status"}]
    assert "private-registry-credential" not in result.stderr


def test_runtime_symlink_is_refused_before_operator_lock_creation(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    (tmp_path / "managed").symlink_to(actual, target_is_directory=True)
    result = _operator(tmp_path)
    assert result.returncode != 0
    assert not (actual / "operator.lock").exists()


def test_help_does_not_require_a_running_manager(tmp_path: Path) -> None:
    result = _operator(tmp_path, "--help")
    assert result.returncode == 0
    assert "--status" in result.stdout and "--resume" in result.stdout
