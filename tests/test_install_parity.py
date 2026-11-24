"""Fast CLI provenance and managed service/CLI release-parity contracts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from exomem import install_info

ROOT = Path(__file__).resolve().parents[1]


def _run_version(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = textwrap.dedent(
        f"""
        import builtins
        import sys

        blocked = {{"torch", "sentence_transformers", "PIL", "faster_whisper"}}
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.split(".", 1)[0] in blocked:
                raise AssertionError(f"optional model import: {{name}}")
            return original(name, *args, **kwargs)
        builtins.__import__ = guarded

        from exomem.__main__ import main
        raise SystemExit(main({list(args)!r}))
        """
    )
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_version_json_is_cheap_and_reports_managed_release_mismatch() -> None:
    manifest = ROOT / "tests" / "install_fixtures" / "managed-install.json"
    result = _run_version(
        "--version",
        "--json",
        env={"EXOMEM_MANAGED_INSTALL_MANIFEST": str(manifest)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert set(report) == {
        "version",
        "python_executable",
        "install_source",
        "local_profile",
        "managed_service_version",
        "managed_service_profile",
        "managed_service_target",
        "effective_route",
        "version_match",
        "manifest_status",
    }
    assert report["managed_service_version"] == "99.1.0"
    assert report["managed_service_profile"] == "media"
    assert report["local_profile"] == "lean"
    assert report["effective_route"] == "direct"
    assert report["version_match"] is False
    assert "TOP SECRET" not in result.stdout
    assert "also secret" not in result.stdout


def test_version_json_rejects_service_target_url_credentials(tmp_path: Path) -> None:
    manifest = tmp_path / "managed-install.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_target": (
                    "https://manifest-user:userinfo-secret@example.test:8443"
                    "?token=query-secret#fragment-secret"
                ),
            }
        ),
        encoding="utf-8",
    )

    result = _run_version(
        "--version",
        "--json",
        env={"EXOMEM_MANAGED_INSTALL_MANIFEST": str(manifest)},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["managed_service_target"] is None
    assert "manifest-user" not in result.stdout
    assert "userinfo-secret" not in result.stdout
    assert "query-secret" not in result.stdout
    assert "fragment-secret" not in result.stdout


def test_plain_version_is_a_single_stable_line() -> None:
    result = _run_version(
        "--version",
        env={
            "EXOMEM_MANAGED_INSTALL_MANIFEST": str(
                ROOT / "tests" / "install_fixtures" / "absent-managed-install.json"
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("exomem ")
    assert result.stdout.count("\n") == 1
    assert result.stderr == ""


def _run_unix_sync(mode: str, *, tool: str | None) -> subprocess.CompletedProcess[str]:
    listed = f"printf '%s\\n' '{tool} v0.4.1'" if tool else ":"
    common_script = "./scripts/_service-common.sh"
    command = (
        "uv() { "
        'if [ "$1 $2" = "tool list" ]; then '
        f"{listed}; "
        "else printf 'UV_CALL %s\\n' \"$*\"; fi; }; "
        f'. "{common_script}"; '
        f'exomem_sync_uv_cli "{mode}" "1.2.3"'
    )
    bash = shutil.which("bash") or "bash"
    return subprocess.run(
        [bash, "-c", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _require_bash(result: subprocess.CompletedProcess[str]) -> None:
    # Codex's Windows sandbox can resolve Git Bash but blocks its signal-pipe
    # bootstrap.  This is an execution-boundary failure, not a script result;
    # normal Windows and Unix CI still exercise the behavior below.
    startup = result.stdout.replace("\x00", "")
    if result.returncode and "Bash/Service/CreateInstance/E_ACCESSDENIED" in startup:
        pytest.skip("sandbox blocked Git Bash startup")
    if result.returncode and "couldn't create signal pipe, Win32 error 5" in startup:
        pytest.skip("sandbox blocked Git Bash startup")


@pytest.mark.parametrize("tool", ["evermatter", "exomem"])
def test_unix_auto_syncs_existing_uv_tool_to_exact_service_release_under_its_name(
    tool: str,
) -> None:
    # Installing the other name beside the existing tool would leave two
    # distributions owning the same commands.
    result = _run_unix_sync("auto", tool=tool)

    _require_bash(result)
    assert result.returncode == 0, result.stderr
    assert f"UV_CALL tool install --force {tool}==1.2.3" in result.stdout
    assert result.stdout.count("UV_CALL tool install") == 1


def test_unix_auto_never_installs_an_absent_uv_tool() -> None:
    result = _run_unix_sync("auto", tool=None)

    _require_bash(result)
    assert result.returncode == 0, result.stderr
    assert "UV_CALL tool install" not in result.stdout


def test_unix_always_may_install_and_never_skips() -> None:
    always = _run_unix_sync("always", tool=None)
    never = _run_unix_sync("never", tool="exomem")

    _require_bash(always)
    _require_bash(never)
    assert always.returncode == 0, always.stderr
    assert "UV_CALL tool install --force evermatter==1.2.3" in always.stdout
    assert never.returncode == 0, never.stderr
    assert "UV_CALL tool install" not in never.stdout


def test_upgrade_scripts_defer_cli_sync_until_live_version_is_verified() -> None:
    windows = (ROOT / "scripts" / "upgrade.ps1").read_text(encoding="utf-8")
    windows_common = (ROOT / "scripts" / "_service-common.ps1").read_text(encoding="utf-8")
    unix = (ROOT / "scripts" / "upgrade.sh").read_text(encoding="utf-8")

    assert 'ValidateSet("auto", "always", "never")' in windows
    assert '$CliSync = "auto"' in windows
    assert "Sync-ExomemUvCli" in windows
    assert "Assert-ExomemInstallApplied" in windows_common
    assert windows.index("Install-ExomemPackage") < windows.index("Offline state migration...")
    assert windows.index("Offline state migration...") < windows.index("Preflight: exomem doctor")
    assert windows.index("Preflight: exomem doctor") < windows.index("Restarting $ServiceName")
    assert windows.index("Restarting $ServiceName") < windows.index("Serving version:")
    assert windows.index("Serving version:") < windows.index("Sync-ExomemUvCli")

    assert "--cli-sync" in unix
    assert 'exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"' in unix
    assert unix.index("exomem_assert_install_applied") < unix.index("Offline state migration...")
    assert unix.index("Offline state migration...") < unix.index("Preflight: exomem doctor")
    assert unix.index("Preflight: exomem doctor") < unix.index("Starting $SERVICE_ID...")
    assert unix.index("Starting $SERVICE_ID...") < unix.index("Serving version:")
    assert unix.index("Serving version:") < unix.index('exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"')


_BOOTSTRAPS = [
    pytest.param(
        [shutil.which("sh") or "sh", "scripts/install.sh"],
        id="install.sh",
        marks=pytest.mark.skipif(
            sys.platform == "win32" or shutil.which("sh") is None,
            reason="install.sh is the macOS and Linux bootstrap",
        ),
    ),
    pytest.param(
        [shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", "scripts/install.ps1"],
        id="install.ps1",
        marks=pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available"),
    ),
]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake uv is a POSIX script")
@pytest.mark.parametrize("bootstrap", _BOOTSTRAPS)
@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        (None, "tool install evermatter"),
        ("evermatter", "tool upgrade evermatter"),
        ("exomem", "tool upgrade exomem"),
    ],
)
def test_bootstrap_upgrades_an_existing_uv_tool_under_its_name(
    tmp_path: Path, bootstrap: list[str], listed: str | None, expected: str
) -> None:
    # A rerun must upgrade the CLI the user already has, never install the other
    # distribution name beside it.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "uv-calls"
    fake_uv = bin_dir / "uv"
    listing = f"printf '%s\\n' '{listed} v0.4.1' '- exomem' '- kb'" if listed else ":"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "tool list" ]; then ' + listing + "; exit 0; fi\n"
        'if [ "$1" = "--version" ]; then echo "uv 0.0.0"; exit 0; fi\n'
        f'echo "$*" >> "{calls}"\n'
    )
    fake_uv.chmod(0o755)
    environment = {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }

    result = subprocess.run(
        bootstrap,
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = calls.read_text().splitlines()
    assert [call.rsplit(" --compile-bytecode", 1)[0] for call in recorded] == [expected]


def test_editable_project_root_comes_from_a_valid_direct_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'exomem'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    class Distribution:
        @staticmethod
        def read_text(name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"url": root.as_uri(), "dir_info": {"editable": True}})

    monkeypatch.setattr(install_info, "distribution", lambda _name: Distribution())

    assert install_info.editable_project_root() == root.resolve()


def test_editable_project_root_rejects_non_file_or_unvalidated_direct_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        @staticmethod
        def read_text(_name: str) -> str:
            return json.dumps({"url": "https://example.test/exomem", "dir_info": {"editable": True}})

    monkeypatch.setattr(install_info, "distribution", lambda _name: Distribution())

    assert install_info.editable_project_root() is None


def test_editable_project_root_reports_malformed_dir_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        @staticmethod
        def read_text(_name: str) -> str:
            return json.dumps({"url": "file:///tmp/exomem", "dir_info": []})

    monkeypatch.setattr(install_info, "distribution", lambda _name: Distribution())

    root, warning = install_info.editable_project_root_status()

    assert root is None
    assert warning == "direct_url.json has malformed dir_info"


def test_editable_project_root_requires_the_project_and_lock_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'exomem'\n", encoding="utf-8")

    class Distribution:
        @staticmethod
        def read_text(_name: str) -> str:
            return json.dumps({"url": root.as_uri(), "dir_info": {"editable": True}})

    monkeypatch.setattr(install_info, "distribution", lambda _name: Distribution())

    assert install_info.editable_project_root() is None


def test_persisted_local_profile_merges_valid_managed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "managed-install.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "service_profile": "media", "future": "keep"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_MANAGED_INSTALL_MANIFEST", str(manifest))

    install_info.persist_local_profile("hybrid")

    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "service_profile": "media",
        "cli_profile": "hybrid",
        "future": "keep",
    }
    assert install_info.configured_local_profile() == "hybrid"


def test_persisted_local_profile_preserves_manifest_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "managed-install.json"
    manifest.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    os.chmod(manifest, 0o600)
    original_mode = stat.S_IMODE(manifest.stat().st_mode)
    monkeypatch.setenv("EXOMEM_MANAGED_INSTALL_MANIFEST", str(manifest))

    install_info.persist_local_profile("hybrid")

    assert stat.S_IMODE(manifest.stat().st_mode) == original_mode


@pytest.mark.parametrize("content", ["not json", '{"schema_version": 2}'])
def test_persisted_local_profile_refuses_to_overwrite_unknown_manifest_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    manifest = tmp_path / "managed-install.json"
    manifest.write_text(content, encoding="utf-8")
    monkeypatch.setenv("EXOMEM_MANAGED_INSTALL_MANIFEST", str(manifest))

    with pytest.raises(ValueError):
        install_info.persist_local_profile("hybrid")

    assert manifest.read_text(encoding="utf-8") == content
