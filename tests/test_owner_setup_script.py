from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "owner-setup.sh"


def test_runner_uses_existing_stop_receipt_and_restart_helpers_in_order() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    transition = text.split("# Ordered maintenance transition", 1)[1]
    calls = [
        "exomem_create_transition_receipt",
        "exomem_stop_service",
        "exomem_assert_service_stopped",
        '"$VENV_PYTHON" -m exomem.native_owner_maintenance',
        "exomem_start_service",
        "exomem_assert_service_restarted",
        "exomem_assert_listener_owned_by_worker",
        "exomem_clear_transition_receipt",
    ]
    positions = [transition.index(call) for call in calls]
    assert positions == sorted(positions)
    assert text.index("PREFLIGHT_ARGS=(preflight") < text.index("# Ordered maintenance transition")
    assert '. "$SCRIPT_DIR/_service-common.sh"' in text
    assert "uv pip" not in text
    assert "drain" not in text.lower()
    assert 'source "$BINDING_PATH"' not in text


def test_runner_keeps_failed_work_stopped_and_resume_requires_exact_receipt() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--resume" in text
    assert "exomem_assert_stopped_resume_authority" in text
    assert "exomem_publish_failed_transition_receipt" in text
    assert 'exomem_stop_service "$SERVICE_ID"' in text
    assert "service remains stopped" in text
    assert "receipt: $TRANSITION_RECEIPT" in text
    assert "review:  $REQUEST_ID" in text


def test_runner_rejects_windows_and_never_offers_an_offline_flag() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "Windows owner setup is not supported by this command" in text
    assert "--offline" not in text


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(sys.platform != "linux", reason="systemd user-service runner")
@pytest.mark.parametrize("python_name", ["python", "python with spaces"])
def test_runner_executes_stop_proof_apply_start_check_with_stubbed_commands(
    tmp_path: Path,
    python_name: str,
) -> None:
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls.log"
    state = tmp_path / "service-state"
    state.write_text("running", encoding="ascii")
    pid = tmp_path / "service-pid"
    pid.write_text("111", encoding="ascii")
    env_file = tmp_path / "service.env"
    env_file.write_text("fixture=true\n", encoding="ascii")
    fake_python = commands / python_name
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    unit = tmp_path / "exomem.service"
    unit.write_text(
        f'[Service]\nEnvironmentFile={env_file}\nExecStart="{fake_python}" -m exomem --port 8765\n',
        encoding="utf-8",
    )
    metadata = {
        "service_id": "exomem",
        "vault_root": str(vault),
        "state_root": str(state_root),
        "binding_path": str(env_file),
        "port": 8765,
        "environment_digest": "a" * 64,
    }
    _executable(
        fake_python,
        "#!/usr/bin/env bash\nset -eu\n"
        'if [[ "${1:-}" == "-c" ]]; then\n'
        '  [[ "$2" == *"importlib.metadata"* ]] && { echo 0.75.0; exit 0; }\n'
        f'  exec {sys.executable} "$@"\nfi\n'
        'if [[ "${1:-}" == "-m" && "${2:-}" == "exomem.native_owner_maintenance" ]]; then\n'
        '  echo "$3" >> "$FAKE_CALL_LOG"\n'
        f"  [[ \"$3\" == metadata ]] && echo '{json.dumps(metadata)}'\n"
        '  if [[ "$3" == apply && ! -e "$FAKE_APPLY_MARKER" ]]; then touch "$FAKE_APPLY_MARKER"; exit 1; fi\n'
        "  exit 0\nfi\n"
        f'exec {sys.executable} "$@"\n',
    )
    _executable(commands / "uname", "#!/usr/bin/env bash\necho Linux\n")
    _executable(
        commands / "systemctl",
        "#!/usr/bin/env bash\nset -eu\n"
        'if [[ "$*" == *MainPID* ]]; then [[ "$(cat "$FAKE_STATE")" == running ]] && cat "$FAKE_PID" || echo 0\n'
        'elif [[ "$*" == *ActiveState* ]]; then cat "$FAKE_STATE"\n'
        'elif [[ "$*" == *" stop "* ]]; then echo stop >> "$FAKE_CALL_LOG"; echo inactive > "$FAKE_STATE"\n'
        'elif [[ "$*" == *" start "* ]]; then echo start >> "$FAKE_CALL_LOG"; echo 222 > "$FAKE_PID"; echo running > "$FAKE_STATE"\n'
        "fi\n",
    )
    _executable(
        commands / "lsof",
        "#!/usr/bin/env bash\n"
        'if [[ "$(cat "$FAKE_STATE")" == running ]]; then cat "$FAKE_PID"; exit 0; fi\n'
        "exit 1\n",
    )
    _executable(
        commands / "ps",
        "#!/usr/bin/env bash\n"
        'if [[ "$(cat "$FAKE_STATE")" == running && "${2:-}" == "$(cat "$FAKE_PID")" ]]; then echo "$2"; fi\n',
    )
    _executable(
        commands / "curl",
        '#!/usr/bin/env bash\necho health >> "$FAKE_CALL_LOG"\necho \'{"version":"0.75.0"}\'\n',
    )
    _executable(commands / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "FAKE_CALL_LOG": str(log),
        "FAKE_STATE": str(state),
        "FAKE_PID": str(pid),
        "FAKE_APPLY_MARKER": str(tmp_path / "apply-failed-once"),
        "EXOMEM_TRANSITION_RECEIPT_ROOT": str(tmp_path / "receipts"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
    }

    first = subprocess.run(
        ["bash", str(SCRIPT), "--unit-file", str(unit), "--request-id", "owner-review-" + "a" * 32],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert first.returncode != 0
    assert state.read_text(encoding="ascii").strip() == "inactive"
    assert "service remains stopped" in first.stderr
    assert "receipt:" in first.stderr
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--unit-file",
            str(unit),
            "--request-id",
            "owner-review-" + "a" * 32,
            "--resume",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="ascii").splitlines()
    assert calls.index("preflight") < calls.index("stop") < calls.index("apply")
    assert calls.index("apply") < calls.index("start") < calls.index("health")
