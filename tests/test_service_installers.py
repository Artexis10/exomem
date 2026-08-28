"""Contract tests for native one-command service installation."""

from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from benchmark_capabilities import require_posix_executable_scripts

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install-service.sh"
COMMON_SH = ROOT / "scripts" / "_service-common.sh"
TRANSITION_TOOL = ROOT / "scripts" / "service-transition-receipt.py"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_python(path: Path) -> None:
    _write_executable(
        path,
        r'''
        #!/usr/bin/python3
        import html
        import os
        import plistlib
        import re
        import runpy
        import shlex
        import stat
        import sys
        from pathlib import Path

        trace = Path(os.environ["TRACE_FILE"])

        def log(message):
            with trace.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")

        if len(sys.argv) > 1 and Path(sys.argv[1]).name == "service-transition-receipt.py":
            script = sys.argv[1]
            sys.argv = sys.argv[1:]
            runpy.run_path(script, run_name="__main__")

        if len(sys.argv) == 6 and sys.argv[1] == "-":
            _, _, unit_raw, platform, preferred, mode = sys.argv
            unit = Path(unit_raw)
            existing = ""
            if platform == "Darwin":
                binding_path = unit
                payload = plistlib.loads(unit.read_bytes())
                existing = str(
                    payload.get("EnvironmentVariables", {}).get(
                        "EXOMEM_STATE_ROOT", ""
                    )
                ).strip()
            else:
                unit_text = unit.read_text(encoding="utf-8")
                match = re.search(r"(?m)^EnvironmentFile=([^\n]+)$", unit_text)
                assert match is not None
                encoded = match.group(1).strip().lstrip("-")
                env_path = Path(
                    re.sub(
                        r"\\x([0-9A-Fa-f]{2})",
                        lambda item: chr(int(item.group(1), 16)),
                        encoded,
                    )
                )
                binding_path = env_path
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("EXOMEM_STATE_ROOT="):
                        existing = line.split("=", 1)[1].strip().strip('"')
            if mode == "binding-path":
                print(binding_path.resolve())
                raise SystemExit(0)
            selected = str(Path(existing or preferred.strip()).resolve())
            assert Path(selected).is_absolute()
            if mode == "bind":
                if existing and str(Path(existing).resolve()) != str(Path(preferred).resolve()):
                    raise SystemExit("state root mismatch")
                log("bind state root")
            print(selected)
            raise SystemExit(0)

        if len(sys.argv) > 1 and sys.argv[1] == "-" and len(sys.argv) == 8:
            _, _, env_path, systemd_path, process_path, xml_path, log_dir, legacy = sys.argv
            values = {}
            for raw in Path(env_path).read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
            values.setdefault("EXOMEM_LOG_DIR", log_dir)
            values["EXOMEM_STATE_ROOT"] = os.environ["EXOMEM_MANAGED_STATE_ROOT_DEFAULT"]
            values.setdefault("PATH", os.environ["PATH"])
            if legacy == "1":
                values["EXOMEM_MCP_LEGACY_COMPAT"] = "1"
            Path(systemd_path).write_text(
                "".join(f'{key}="{value}"\n' for key, value in values.items()),
                encoding="utf-8",
            )
            Path(process_path).write_text(
                "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()),
                encoding="utf-8",
            )
            xml = ["    <key>EnvironmentVariables</key>", "    <dict>"]
            for key, value in values.items():
                xml += [f"        <key>{html.escape(key)}</key>", f"        <string>{html.escape(value)}</string>"]
            xml.append("    </dict>")
            Path(xml_path).write_text("\n".join(xml) + "\n", encoding="utf-8")
            for output in (systemd_path, process_path, xml_path):
                Path(output).chmod(stat.S_IRUSR | stat.S_IWUSR)
            log("render environment")
            raise SystemExit(0)

        if len(sys.argv) > 1 and sys.argv[1] == "-" and len(sys.argv) == 10:
            _, _, src, dest, env_xml, python, working_dir, host, port, log_dir = sys.argv
            text = Path(src).read_text(encoding="utf-8")
            replacements = {
                "__VENV_PYTHON__": python,
                "__WORKING_DIRECTORY__": working_dir,
                "__BIND_HOST__": host,
                "__PORT__": port,
                "__LOG_DIR__": log_dir,
            }
            for marker, value in replacements.items():
                text = text.replace(marker, html.escape(value))
            text = text.replace("    __ENVIRONMENT_VARIABLES__\n", Path(env_xml).read_text(encoding="utf-8"))
            Path(dest).write_text(text, encoding="utf-8")
            log("render launchd")
            raise SystemExit(0)

        if len(sys.argv) > 1 and sys.argv[1] == "-" and len(sys.argv) == 9:
            _, _, src, dest, python, working_dir, env_file, host, port = sys.argv
            text = Path(src).read_text(encoding="utf-8")
            def scalar_path(value):
                escaped = {" ": "\\x20", "\t": "\\x09", "\n": "\\x0a", "\r": "\\x0d", "\\": "\\x5c"}
                return "".join(escaped.get(char, char) for char in value)
            replacements = {
                "__VENV_PYTHON__": python.replace("\\", "\\\\").replace('"', '\\"'),
                "__WORKING_DIRECTORY__": scalar_path(working_dir),
                "__SERVICE_ENV_FILE__": scalar_path(env_file),
                "__BIND_HOST__": host,
                "__PORT__": port,
            }
            for marker, value in replacements.items():
                text = text.replace(marker, value)
            Path(dest).write_text(text, encoding="utf-8")
            log("render systemd")
            raise SystemExit(0)

        if sys.argv[1:3] == ["-m", "exomem"] and "maintain" in sys.argv:
            assert "--migrate-state" in sys.argv
            assert "--offline" in sys.argv
            log("migrate state offline")
            raise SystemExit(0)

        if sys.argv[1:3] == ["-m", "exomem"] and "doctor" in sys.argv:
            profile = sys.argv[sys.argv.index("--profile") + 1]
            log(f"doctor {profile}")
            if os.environ.get("FAKE_DOCTOR_FAIL_PROFILE") == profile:
                raise SystemExit(1)
            raise SystemExit(0)

        if len(sys.argv) > 2 and sys.argv[1] == "-c" and "importlib.metadata" in sys.argv[2]:
            print(os.environ.get("FAKE_INSTALLED_VERSION", "9.9.9"))
            raise SystemExit(0)

        raise SystemExit(0)
        ''',
    )


def _fixture(tmp_path: Path, *, os_name: str = "Linux", arch: str = "x86_64") -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    service_root = tmp_path / "service root"
    trace = tmp_path / "trace.log"
    env_file = tmp_path / ".env"
    service_state = tmp_path / "service.state"
    worker_pid = tmp_path / "worker.pid"
    next_pid = tmp_path / "next.pid"
    listener = tmp_path / "listener.pid"
    home.mkdir()
    bin_dir.mkdir()
    (service_root / ".venv" / "bin").mkdir(parents=True)
    trace.touch()
    next_pid.write_text("4201\n", encoding="ascii")
    env_file.write_text(
        "\n".join(
            [
                f"EXOMEM_VAULT_PATH={tmp_path / 'vault'}",
                "EXOMEM_BASE_URL=https://memory.example.test",
                "EXOMEM_GITHUB_USERNAME=test-user",
                "GITHUB_CLIENT_ID=test-client",
                "GITHUB_CLIENT_SECRET=secret&value",
                "EXOMEM_JWT_SECRET=test-jwt-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _fake_python(service_root / ".venv" / "bin" / "python")

    _write_executable(
        bin_dir / "uname",
        f'''
        #!/bin/sh
        if [ "$1" = "-m" ]; then
            printf '%s\n' "{arch}"
        else
            printf '%s\n' "{os_name}"
        fi
        ''',
    )
    _write_executable(
        bin_dir / "uv",
        '''
        #!/bin/sh
        printf 'uv %s\n' "$*" >> "$TRACE_FILE"
        exit 0
        ''',
    )
    _write_executable(
        bin_dir / "systemctl",
        '''
        #!/bin/sh
        printf 'systemctl %s\n' "$*" >> "$TRACE_FILE"
        if [ "$1" = "--user" ] && [ "$2" = "show" ]; then
            case "${5:-}" in
                MainPID)
                    if [ "$(cat "$FAKE_SERVICE_STATE_FILE" 2>/dev/null)" = "running" ] \
                        && [ -f "$FAKE_WORKER_PID_FILE" ]; then
                        cat "$FAKE_WORKER_PID_FILE"
                    else
                        printf '0\n'
                    fi
                    ;;
                ActiveState)
                    cat "$FAKE_SERVICE_STATE_FILE" 2>/dev/null || printf 'inactive\n'
                    ;;
            esac
            exit 0
        fi
        if [ "$1" = "--user" ] && [ "$2" = "stop" ]; then
            printf 'inactive\n' > "$FAKE_SERVICE_STATE_FILE"
            rm -f "$FAKE_WORKER_PID_FILE" "$FAKE_LISTENER_FILE"
            exit 0
        fi
        if [ "$1" = "--user" ] && [ "$2" = "start" ]; then
            pid=$(( $(cat "$FAKE_NEXT_PID_FILE") + 1 ))
            printf '%s\n' "$pid" > "$FAKE_NEXT_PID_FILE"
            printf '%s\n' "$pid" > "$FAKE_WORKER_PID_FILE"
            printf '%s\n' "$pid" > "$FAKE_LISTENER_FILE"
            printf 'running\n' > "$FAKE_SERVICE_STATE_FILE"
            exit 0
        fi
        exit 0
        ''',
    )
    _write_executable(
        bin_dir / "launchctl",
        '''
        #!/bin/sh
        printf 'launchctl %s\n' "$*" >> "$TRACE_FILE"
        if [ "$1" = "print" ]; then
            if [ "$(cat "$FAKE_SERVICE_STATE_FILE" 2>/dev/null)" != "running" ] \
                || [ ! -f "$FAKE_WORKER_PID_FILE" ]; then
                exit 1
            fi
            printf '{\n    pid = %s\n}\n' "$(cat "$FAKE_WORKER_PID_FILE")"
            exit 0
        fi
        if [ "$1" = "bootout" ]; then
            printf 'inactive\n' > "$FAKE_SERVICE_STATE_FILE"
            rm -f "$FAKE_WORKER_PID_FILE" "$FAKE_LISTENER_FILE"
            exit 0
        fi
        if [ "$1" = "kickstart" ]; then
            pid=$(( $(cat "$FAKE_NEXT_PID_FILE") + 1 ))
            printf '%s\n' "$pid" > "$FAKE_NEXT_PID_FILE"
            printf '%s\n' "$pid" > "$FAKE_WORKER_PID_FILE"
            printf '%s\n' "$pid" > "$FAKE_LISTENER_FILE"
            printf 'running\n' > "$FAKE_SERVICE_STATE_FILE"
            exit 0
        fi
        exit 0
        ''',
    )
    _write_executable(
        bin_dir / "ps",
        '''
        #!/bin/sh
        if [ "$1" = "-p" ] \
            && [ "$(cat "$FAKE_SERVICE_STATE_FILE" 2>/dev/null)" = "running" ] \
            && [ "${2:-}" = "$(cat "$FAKE_WORKER_PID_FILE" 2>/dev/null)" ]; then
            printf '%s\n' "$2"
        fi
        exit 0
        ''',
    )
    _write_executable(
        bin_dir / "lsof",
        '''
        #!/bin/sh
        if [ -f "$FAKE_LISTENER_FILE" ]; then
            cat "$FAKE_LISTENER_FILE"
            exit 0
        fi
        exit 1
        ''',
    )
    _write_executable(
        bin_dir / "plutil",
        '''
        #!/bin/sh
        printf 'com.exomem\n'
        ''',
    )
    _write_executable(
        bin_dir / "loginctl",
        '''
        #!/bin/sh
        printf 'loginctl %s\n' "$*" >> "$TRACE_FILE"
        if [ "$1" = "show-user" ]; then
            printf 'yes\n'
        fi
        exit 0
        ''',
    )
    _write_executable(
        bin_dir / "curl",
        '''
        #!/bin/sh
        printf 'curl %s\n' "$*" >> "$TRACE_FILE"
        case "${*}" in
            *"/health") printf '{"version":"%s"}' "${FAKE_LIVE_VERSION:-9.9.9}" ;;
            *) printf '%s' "${FAKE_HTTP_STATUS:-401}" ;;
        esac
        exit 0
        ''',
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USER": "test-user",
            "PATH": f"{bin_dir}:{env['PATH']}",
            "TRACE_FILE": str(trace),
            "FAKE_SERVICE_STATE_FILE": str(service_state),
            "FAKE_WORKER_PID_FILE": str(worker_pid),
            "FAKE_NEXT_PID_FILE": str(next_pid),
            "FAKE_LISTENER_FILE": str(listener),
            "FAKE_INSTALLED_VERSION": "9.9.9",
            "FAKE_LIVE_VERSION": "9.9.9",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )
    return env, service_root, env_file


def _invoke(
    env: dict[str, str],
    service_root: Path,
    env_file: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--release",
            "--service-root",
            str(service_root),
            "--env-file",
            str(env_file),
            *args,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _run(
    tmp_path: Path,
    *args: str,
    os_name: str = "Linux",
    arch: str = "x86_64",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, str]]:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path, os_name=os_name, arch=arch)
    result = _invoke(env, service_root, env_file, *args)
    return result, service_root, Path(env["TRACE_FILE"]), env


def test_linux_release_install_renders_env_gates_then_verifies(tmp_path: Path) -> None:
    result, service_root, trace_path, env = _run(tmp_path, "--profile", "hybrid")

    assert result.returncode == 0, result.stderr
    trace = trace_path.read_text(encoding="utf-8")
    assert "uv pip install --upgrade --python" in trace
    assert "exomem[embeddings]" in trace
    assert trace.index("uv pip install") < trace.index("migrate state offline")
    assert trace.index("migrate state offline") < trace.index("doctor hybrid")
    assert trace.index("doctor hybrid") < trace.index("doctor remote")
    assert trace.index("doctor remote") < trace.index("systemctl --user daemon-reload")
    assert trace.index("systemctl --user enable exomem") < trace.index(
        "systemctl --user start exomem"
    )
    assert trace.index("systemctl --user start exomem") < trace.index("curl ")

    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "exomem.service"
    unit_text = unit.read_text(encoding="utf-8")
    assert str(service_root / ".venv" / "bin" / "python") in unit_text
    assert "EnvironmentFile=" in unit_text
    assert "__" not in unit_text

    if shutil.which("systemd-analyze"):
        verified = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert verified.returncode == 0, verified.stderr

    service_env = Path(env["XDG_CONFIG_HOME"]) / "exomem" / "service.env"
    service_env_text = service_env.read_text(encoding="utf-8")
    assert "GITHUB_CLIENT_SECRET=" in service_env_text
    expected_state_root = Path(env["HOME"]) / ".local" / "state" / "exomem" / "state"
    assert f'EXOMEM_STATE_ROOT="{expected_state_root}"' in service_env_text
    assert stat.S_IMODE(service_env.stat().st_mode) == 0o600
    assert "-> 401 (healthy, OAuth enforced)" in result.stdout
    assert "version:    9.9.9" in result.stdout


def test_default_release_profile_is_standard_multimodal(tmp_path: Path) -> None:
    result, _, trace_path, _ = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    trace = trace_path.read_text(encoding="utf-8")
    assert "exomem[embeddings,media]" in trace
    assert "doctor standard" in trace


def test_macos_arm64_standard_adds_mlx(tmp_path: Path) -> None:
    result, _, trace_path, _ = _run(tmp_path, os_name="Darwin", arch="arm64")

    assert result.returncode == 0, result.stderr
    assert "exomem[embeddings,media,media-mlx]" in trace_path.read_text(encoding="utf-8")


def test_macos_arm64_media_adds_mlx_and_launchd_environment(tmp_path: Path) -> None:
    result, _, trace_path, env = _run(
        tmp_path,
        "--profile",
        "media",
        os_name="Darwin",
        arch="arm64",
    )

    assert result.returncode == 0, result.stderr
    trace = trace_path.read_text(encoding="utf-8")
    assert "exomem[embeddings,media,vision,diarization,media-mlx]" in trace
    assert trace.index("doctor remote") < trace.index("launchctl bootstrap")
    assert trace.index("launchctl kickstart") < trace.index("curl ")

    plist = Path(env["HOME"]) / "Library" / "LaunchAgents" / "com.exomem.plist"
    plist_text = plist.read_text(encoding="utf-8")
    assert "<key>EnvironmentVariables</key>" in plist_text
    assert "<key>PATH</key>" in plist_text
    assert "secret&amp;value" in plist_text
    assert "__" not in plist_text
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600


def test_doctor_failure_leaves_the_service_stopped(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    env["FAKE_DOCTOR_FAIL_PROFILE"] = "hybrid"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--release",
            "--profile",
            "hybrid",
            "--service-root",
            str(service_root),
            "--env-file",
            str(env_file),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    trace = Path(env["TRACE_FILE"]).read_text(encoding="utf-8")
    assert trace.index("migrate state offline") < trace.index("doctor hybrid")
    assert "doctor hybrid" in trace
    assert "systemctl --user start exomem" not in trace
    assert "systemctl --user stop exomem" in trace
    assert "launchctl" not in trace
    assert Path(env["FAKE_SERVICE_STATE_FILE"]).read_text(encoding="ascii").strip() == "inactive"


def test_http_200_stops_service_and_fails_closed(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    env["FAKE_HTTP_STATUS"] = "200"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--release",
            "--profile",
            "lean",
            "--service-root",
            str(service_root),
            "--env-file",
            str(env_file),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "OAuth is not enforced" in result.stderr
    trace = Path(env["TRACE_FILE"]).read_text(encoding="utf-8")
    assert "curl " in trace
    assert "systemctl --user stop exomem" in trace


def test_live_version_mismatch_stops_service_and_fails_closed(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    env["FAKE_LIVE_VERSION"] = "9.9.8"

    result = _invoke(env, service_root, env_file, "--profile", "lean")

    assert result.returncode != 0
    assert "live service version '9.9.8' differs" in result.stderr
    trace = Path(env["TRACE_FILE"]).read_text(encoding="utf-8")
    assert "curl -fsS --max-time 5 http://127.0.0.1:8765/health" in trace
    assert "systemctl --user stop exomem" in trace
    assert Path(env["FAKE_SERVICE_STATE_FILE"]).read_text(
        encoding="ascii"
    ).strip() == "inactive"


def test_ss_socket_without_visible_pid_is_not_reported_unbound(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "ss",
        '''
        #!/bin/sh
        printf 'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:*\n'
        ''',
    )
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [
            "bash",
            "-c",
            '''
            command() {
                if [[ "$1" == "-v" && "$2" == "lsof" ]]; then
                    return 1
                fi
                builtin command "$@"
            }
            . "$1"
            exomem_assert_listener_unbound 8765
            ''',
            "listener-proof",
            str(COMMON_SH),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "cannot prove listener port 8765 is unbound" in result.stderr


def test_ss_mixed_visible_and_hidden_listeners_are_not_a_complete_pid_proof(
    tmp_path: Path,
) -> None:
    require_posix_executable_scripts()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "ss",
        '''
        #!/bin/sh
        printf '%s\n' \
          'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:* users:(("python",pid=4312,fd=3))' \
          'LISTEN 0 128 0.0.0.0:8765 0.0.0.0:*'
        ''',
    )
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [
            "bash",
            "-c",
            '''
            command() {
                if [[ "$1" == "-v" && "$2" == "lsof" ]]; then
                    return 1
                fi
                builtin command "$@"
            }
            . "$1"
            exomem_listener_pids 8765
            ''',
            "ambiguous-listener-proof",
            str(COMMON_SH),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0


def test_posix_resume_refuses_a_detached_non_listening_writer_until_it_exits(
    tmp_path: Path,
) -> None:
    require_posix_executable_scripts()
    database = tmp_path / "legacy.sqlite"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                "db.execute('create table writes(value text)'); "
                "db.execute(\"insert into writes values ('before')\"); db.commit(); "
                "print('READY', flush=True); assert sys.stdin.readline().strip()=='commit'; "
                "db.execute(\"insert into writes values ('after')\"); db.commit(); "
                "print('COMMITTED', flush=True); db.close()"
            ),
            str(database),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None and child.stdout is not None
    assert child.stdout.readline().strip() == "READY"
    receipt = tmp_path / "receipts" / "exomem.json"
    binding = tmp_path / "service.env"
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    identity = [
        "--path",
        str(receipt),
        "--service-id",
        "exomem",
        "--binding-path",
        str(binding),
        "--state-root",
        str(state_root),
        "--vault",
        str(vault),
        "--target-port",
        "8765",
    ]
    created = subprocess.run(
        [
            sys.executable,
            str(TRANSITION_TOOL),
            "create",
            *identity,
            "--port",
            "8765",
            "--worker-pid",
            str(child.pid),
            "--listener-pid",
            str(child.pid),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    driver = """
        . "$1"
        exomem_service_is_stopped() { return 0; }
        exomem_listener_pids() { return 0; }
        exomem_assert_stopped_resume_authority \
            "$2" "$3" exomem "$4" "$5" "$6" 8765
    """

    def resume() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                driver,
                "posix-receipt-driver",
                str(COMMON_SH),
                sys.executable,
                str(receipt),
                str(binding),
                str(state_root),
                str(vault),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    try:
        refused = resume()
        assert refused.returncode != 0
        assert f"captured transition pid {child.pid} is still alive" in refused.stderr
        child.stdin.write("commit\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "COMMITTED"
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        accepted = resume()
        assert accepted.returncode == 0, accepted.stderr
        with sqlite3.connect(database) as reader:
            assert reader.execute("select value from writes order by rowid").fetchall() == [
                ("before",),
                ("after",),
            ]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize("receipt_phase", ("starting", "started"))
def test_posix_failed_start_publishes_new_listener_before_resume_can_be_authorized(
    tmp_path: Path, receipt_phase: str,
) -> None:
    require_posix_executable_scripts()
    database = tmp_path / "failed-start.sqlite"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                "db.execute('create table writes(value text)'); "
                "db.execute(\"insert into writes values ('before')\"); db.commit(); "
                "print('LISTENING', flush=True); "
                "assert sys.stdin.readline().strip()=='close'; print('CLOSED', flush=True); "
                "assert sys.stdin.readline().strip()=='commit'; "
                "db.execute(\"insert into writes values ('after')\"); db.commit(); "
                "print('COMMITTED', flush=True); db.close()"
            ),
            str(database),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None and child.stdout is not None
    assert child.stdout.readline().strip() == "LISTENING"
    receipt = tmp_path / "receipts" / "exomem.json"
    binding = tmp_path / "service.env"
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    target_worker_pid = 2147482999
    identity = [
        "--path",
        str(receipt),
        "--service-id",
        "exomem",
        "--binding-path",
        str(binding),
        "--state-root",
        str(state_root),
        "--vault",
        str(vault),
        "--target-port",
        "8765",
    ]
    created = subprocess.run(
        [
            sys.executable,
            str(TRANSITION_TOOL),
            "create",
            *identity,
            "--port",
            "8764",
            "--worker-pid",
            "2147483000",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    transitioned = subprocess.run(
        [sys.executable, str(TRANSITION_TOOL), "phase", *identity, "--phase", receipt_phase],
        text=True,
        capture_output=True,
        check=False,
    )
    assert transitioned.returncode == 0, transitioned.stderr
    publish_driver = r'''
        . "$1"
        exomem_listener_pids() {
            [[ "$1" == 8765 ]] && printf '%s\n' "$LISTENER_PID"
            return 0
        }
        exomem_publish_failed_transition_receipt \
            "$2" "$3" exomem "$4" "$5" "$6" 8765 "$TARGET_WORKER_PID"
    '''
    env = os.environ.copy()
    env["LISTENER_PID"] = str(child.pid)
    env["TARGET_WORKER_PID"] = str(target_worker_pid)
    published = subprocess.run(
        [
            "bash",
            "-c",
            publish_driver,
            "posix-failed-start-publisher",
            str(COMMON_SH),
            sys.executable,
            str(receipt),
            str(binding),
            str(state_root),
            str(vault),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert published.returncode == 0, published.stderr
    verified = subprocess.run(
        [sys.executable, str(TRANSITION_TOOL), "verify", *identity, "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    proof_pids = json.loads(verified.stdout)["proof_pids"]
    assert child.pid in proof_pids
    assert target_worker_pid in proof_pids

    resume_driver = r'''
        . "$1"
        exomem_service_is_stopped() { return 0; }
        exomem_listener_pids() { return 0; }
        exomem_assert_stopped_resume_authority \
            "$2" "$3" exomem "$4" "$5" "$6" 8765
    '''

    def resume() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                resume_driver,
                "posix-failed-start-resume",
                str(COMMON_SH),
                sys.executable,
                str(receipt),
                str(binding),
                str(state_root),
                str(vault),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    try:
        child.stdin.write("close\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "CLOSED"
        refused = resume()
        assert refused.returncode != 0
        assert f"captured transition pid {child.pid} is still alive" in refused.stderr
        child.stdin.write("commit\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "COMMITTED"
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        accepted = resume()
        assert accepted.returncode == 0, accepted.stderr
        with sqlite3.connect(database) as reader:
            assert reader.execute("select value from writes order by rowid").fetchall() == [
                ("before",),
                ("after",),
            ]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize("receipt_phase", ("starting", "started"))
def test_posix_failed_start_proof_failure_retains_preaccepted_phase(
    tmp_path: Path, receipt_phase: str,
) -> None:
    require_posix_executable_scripts()
    for mode in ("unavailable", "unattributable", "write-failure"):
        case = tmp_path / receipt_phase / mode
        receipt = case / "receipts" / "exomem.json"
        binding = case / "service.env"
        state_root = case / "state"
        vault = case / "vault"
        identity = [
            "--path",
            str(receipt),
            "--service-id",
            "exomem",
            "--binding-path",
            str(binding),
            "--state-root",
            str(state_root),
            "--vault",
            str(vault),
            "--target-port",
            "8765",
        ]
        created = subprocess.run(
            [
                sys.executable,
                str(TRANSITION_TOOL),
                "create",
                *identity,
                "--port",
                "8764",
                "--worker-pid",
                "2147483000",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        transitioned = subprocess.run(
            [sys.executable, str(TRANSITION_TOOL), "phase", *identity, "--phase", receipt_phase],
            text=True,
            capture_output=True,
            check=False,
        )
        assert transitioned.returncode == 0, transitioned.stderr
        driver = r'''
            . "$1"
            if [[ "$FAILURE_MODE" == unavailable ]]; then
                exomem_listener_pids() { return 2; }
            elif [[ "$FAILURE_MODE" == unattributable ]]; then
                exomem_listener_pids() { printf 'hidden\n'; return 0; }
            else
                exomem_listener_pids() { return 0; }
                exomem_update_transition_receipt() { return 1; }
            fi
            exomem_publish_failed_transition_receipt \
                "$2" "$3" exomem "$4" "$5" "$6" 8765 2147482999
        '''
        env = os.environ.copy()
        env["FAILURE_MODE"] = mode
        result = subprocess.run(
            [
                "bash",
                "-c",
                driver,
                "posix-failed-start-failure",
                str(COMMON_SH),
                sys.executable,
                str(receipt),
                str(binding),
                str(state_root),
                str(vault),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        assert json.loads(receipt.read_text(encoding="utf-8"))["phase"] == receipt_phase
        resume = subprocess.run(
            [
                "bash",
                "-c",
                r'''
                . "$1"
                exomem_service_is_stopped() { return 0; }
                exomem_listener_pids() { return 0; }
                exomem_assert_stopped_resume_authority \
                    "$2" "$3" exomem "$4" "$5" "$6" 8765
                ''',
                "posix-failed-start-resume-refusal",
                str(COMMON_SH),
                sys.executable,
                str(receipt),
                str(binding),
                str(state_root),
                str(vault),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert resume.returncode != 0
        assert "incomplete start" in resume.stderr


def test_existing_install_stops_binds_then_rolls_forward(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    first = _invoke(env, service_root, env_file, "--profile", "lean")
    assert first.returncode == 0, first.stderr
    first_pid = Path(env["FAKE_WORKER_PID_FILE"]).read_text(encoding="ascii").strip()

    # An ambient change cannot silently relocate an existing service. The
    # rendered binding remains the authority until a separate offline move.
    env["EXOMEM_STATE_ROOT"] = str(tmp_path / "different-state-root")
    trace_path = Path(env["TRACE_FILE"])
    trace_path.write_text("", encoding="utf-8")
    second = _invoke(env, service_root, env_file, "--profile", "lean")

    assert second.returncode == 0, second.stderr
    second_pid = Path(env["FAKE_WORKER_PID_FILE"]).read_text(encoding="ascii").strip()
    assert second_pid != first_pid
    trace = trace_path.read_text(encoding="utf-8")
    stop = trace.index("systemctl --user stop exomem")
    prove = trace.index("systemctl --user show exomem --property ActiveState --value")
    bind = trace.index("bind state root")
    install = trace.index("uv pip install")
    migrate = trace.index("migrate state offline")
    doctor = trace.index("doctor lean")
    start = trace.index("systemctl --user start exomem")
    assert stop < prove < bind < install < migrate < doctor < start

    service_env = Path(env["XDG_CONFIG_HOME"]) / "exomem" / "service.env"
    original_state_root = Path(env["HOME"]) / ".local" / "state" / "exomem" / "state"
    assert f'EXOMEM_STATE_ROOT="{original_state_root}"' in service_env.read_text(
        encoding="utf-8"
    )


def test_stopped_existing_install_requires_the_exact_failed_transition_receipt(
    tmp_path: Path,
) -> None:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    first = _invoke(env, service_root, env_file, "--profile", "lean")
    assert first.returncode == 0, first.stderr

    trace_path = Path(env["TRACE_FILE"])
    trace_path.write_text("", encoding="utf-8")

    env["FAKE_DOCTOR_FAIL_PROFILE"] = "lean"
    failed = _invoke(env, service_root, env_file, "--profile", "lean")
    assert failed.returncode != 0
    receipt = (
        Path(env["HOME"])
        / ".local"
        / "state"
        / "exomem"
        / "transitions"
        / "exomem.json"
    )
    assert receipt.is_file()

    refused = _invoke(env, service_root, env_file, "--profile", "lean")
    assert refused.returncode != 0
    assert "existing service must be running with a capturable worker" in refused.stderr

    trace_path.write_text("", encoding="utf-8")
    env.pop("FAKE_DOCTOR_FAIL_PROFILE")
    resumed = _invoke(
        env,
        service_root,
        env_file,
        "--profile",
        "lean",
        "--resume-stopped-transition",
    )
    assert resumed.returncode == 0, resumed.stderr
    assert not receipt.exists()
    trace = trace_path.read_text(encoding="utf-8")
    assert trace.index("systemctl --user show exomem --property ActiveState --value") < trace.index(
        "bind state root"
    )
    assert trace.index("bind state root") < trace.index("uv pip install")
    assert trace.index("migrate state offline") < trace.index(
        "systemctl --user start exomem"
    )


def test_fresh_install_refuses_an_occupied_listener_before_install(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    Path(env["FAKE_LISTENER_FILE"]).write_text("4999\n", encoding="ascii")

    result = _invoke(env, service_root, env_file, "--profile", "lean")

    assert result.returncode != 0
    assert "fresh install cannot prove its listener is unbound" in result.stderr
    assert "uv pip install" not in Path(env["TRACE_FILE"]).read_text(encoding="utf-8")


def test_help_and_invalid_profile_are_non_mutating() -> None:
    require_posix_executable_scripts()
    help_result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    invalid_result = subprocess.run(
        ["bash", str(INSTALL_SH), "--profile", "invalid"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert "--release" in help_result.stdout
    assert "--repo-dev" in help_result.stdout
    assert 'MODE="repo-dev"' in INSTALL_SH.read_text(encoding="utf-8")
    assert invalid_result.returncode != 0
    assert "lean, onnx, hybrid, standard, or media" in invalid_result.stderr


def test_onnx_profile_installs_the_cpu_lane_and_preflights_as_hybrid(tmp_path: Path) -> None:
    """#481: a GPU-less host had no way to get vectors without a CUDA torch wheel.

    `torch` is pinned to the CUDA index for Linux, so `--profile hybrid` pulled
    multi-GB of wheel that can never be used, and `lean` gave no vectors at all.
    `onnx` is an install lane rather than a doctor profile — it expects exactly
    the vector lane `hybrid` expects, so the preflight maps onto that.
    """
    require_posix_executable_scripts()
    env, service_root, env_file = _fixture(tmp_path)
    subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--release",
            "--profile",
            "onnx",
            "--service-root",
            str(service_root),
            "--env-file",
            str(env_file),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    trace = Path(env["TRACE_FILE"]).read_text(encoding="utf-8")
    assert "exomem[embeddings-onnx]" in trace
    assert "exomem[embeddings]\n" not in trace, "must not pull the CUDA torch lane"
    assert "doctor hybrid" in trace


def test_windows_installer_gates_remote_and_verifies_before_success() -> None:
    text = (ROOT / "scripts" / "install-service.ps1").read_text(encoding="utf-8")
    # Package install and profile->extras mapping moved into the shared helper so
    # upgrade.ps1 performs them identically instead of duplicating them.
    common = (ROOT / "scripts" / "_service-common.ps1").read_text(encoding="utf-8")

    assert "Install-ExomemPackage" in text
    assert '[string]$Profile = "standard"' in text
    assert '"uv", "pip", "install", "--upgrade", "--refresh-package", "exomem", "--python", $Python, $pkg' in common
    assert '"[embeddings,media]"' in common
    # #578: installing is not deploying. The installer shares this helper, so a
    # fresh venv that ends up with nothing in it has to fail here too.
    assert "Assert-ExomemInstallApplied" in common
    assert "Preflight: exomem doctor --profile remote" in text
    assert "function Test-McpEndpoint" in text
    assert "-SkipHttpErrorCheck" in text
    assert "OAuth is not enforced" in text
    assert "CHATGPT_PLUGIN_REFRESH_REQUIRED" in text
    assert "connector rollout is incomplete" in text
    assert text.index("Preflight: exomem doctor --profile remote") < text.index("& $NssmPath install")
    assert text.index("Test-McpEndpoint -HostName") < text.index("Granted no-UAC")
    assert text.index("Test-McpEndpoint -HostName") < text.index("CHATGPT_PLUGIN_REFRESH_REQUIRED")


def test_the_embedded_toolchain_stand_ins_are_valid_python() -> None:
    """The shims are Python source in a string, so nothing type-checks them.

    An edit that inserted a module-level import into this file matched inside
    one of these bodies too, putting an unindented line inside an indented
    block. Nothing here failed -- the shim was written out fine and every
    installer run then died with `IndentationError` from the generated file,
    which reads as the installer being broken.
    """
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    bodies = [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.lstrip().startswith("#!/usr/bin/python3")
    ]
    assert bodies, "no embedded Python stand-in found to check"
    for body in bodies:
        ast.parse(textwrap.dedent(body).lstrip())
