"""Contract tests for native one-command service installation."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install-service.sh"


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
        import shlex
        import stat
        import sys
        from pathlib import Path

        trace = Path(os.environ["TRACE_FILE"])

        def log(message):
            with trace.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")

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

        if sys.argv[1:3] == ["-m", "exomem"] and "doctor" in sys.argv:
            profile = sys.argv[sys.argv.index("--profile") + 1]
            log(f"doctor {profile}")
            if os.environ.get("FAKE_DOCTOR_FAIL_PROFILE") == profile:
                raise SystemExit(1)
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
    home.mkdir()
    bin_dir.mkdir()
    (service_root / ".venv" / "bin").mkdir(parents=True)
    trace.touch()
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
    for command in ("uv", "systemctl", "launchctl"):
        _write_executable(
            bin_dir / command,
            f'''
            #!/bin/sh
            printf '%s %s\n' "{command}" "$*" >> "$TRACE_FILE"
            exit 0
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
        printf '%s' "${FAKE_HTTP_STATUS:-401}"
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
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )
    return env, service_root, env_file


def _run(tmp_path: Path, *args: str, os_name: str = "Linux", arch: str = "x86_64") -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, str]]:
    env, service_root, env_file = _fixture(tmp_path, os_name=os_name, arch=arch)
    result = subprocess.run(
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
    return result, service_root, Path(env["TRACE_FILE"]), env


def test_linux_release_install_renders_env_gates_then_verifies(tmp_path: Path) -> None:
    result, service_root, trace_path, env = _run(tmp_path, "--profile", "hybrid")

    assert result.returncode == 0, result.stderr
    trace = trace_path.read_text(encoding="utf-8")
    assert "uv pip install --upgrade --python" in trace
    assert "exomem[embeddings]" in trace
    assert trace.index("doctor hybrid") < trace.index("doctor remote")
    assert trace.index("doctor remote") < trace.index("systemctl --user daemon-reload")
    assert trace.index("systemctl --user enable --now exomem") < trace.index("curl ")

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
    assert "GITHUB_CLIENT_SECRET=" in service_env.read_text(encoding="utf-8")
    assert stat.S_IMODE(service_env.stat().st_mode) == 0o600
    assert "-> 401 (healthy, OAuth enforced)" in result.stdout


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


def test_doctor_failure_does_not_touch_service_manager(tmp_path: Path) -> None:
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
    assert "doctor hybrid" in trace
    assert "systemctl" not in trace
    assert "launchctl" not in trace


def test_http_200_stops_service_and_fails_closed(tmp_path: Path) -> None:
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


def test_help_and_invalid_profile_are_non_mutating() -> None:
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
    assert "lean, hybrid, standard, or media" in invalid_result.stderr


def test_windows_installer_gates_remote_and_verifies_before_success() -> None:
    text = (ROOT / "scripts" / "install-service.ps1").read_text(encoding="utf-8")

    assert '"pip", "install", "--upgrade", "--python", $venvPython, $pkg' in text
    assert '[string]$Profile = "standard"' in text
    assert '"exomem[embeddings,media]"' in text
    assert "Preflight: exomem doctor --profile remote" in text
    assert "function Test-McpEndpoint" in text
    assert "-SkipHttpErrorCheck" in text
    assert "OAuth is not enforced" in text
    assert text.index("Preflight: exomem doctor --profile remote") < text.index("& $NssmPath install")
    assert text.index("Test-McpEndpoint -HostName") < text.index("Granted no-UAC")
