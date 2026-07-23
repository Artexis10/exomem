"""Fast CLI provenance and managed service/CLI release-parity contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


def _run_unix_sync(mode: str, *, tool_present: bool) -> subprocess.CompletedProcess[str]:
    listed = "printf '%s\\n' 'exomem v0.4.1'" if tool_present else ":"
    command = (
        "uv() { "
        'if [ "$1 $2" = "tool list" ]; then '
        f"{listed}; "
        "else printf 'UV_CALL %s\\n' \"$*\"; fi; }; "
        f'. "{ROOT / "scripts" / "_service-common.sh"}"; '
        f'exomem_sync_uv_cli "{mode}" "1.2.3"'
    )
    return subprocess.run(
        ["bash", "-c", command],
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


def test_unix_auto_syncs_existing_uv_tool_to_exact_service_release() -> None:
    result = _run_unix_sync("auto", tool_present=True)

    _require_bash(result)
    assert result.returncode == 0, result.stderr
    assert "UV_CALL tool install --force exomem==1.2.3" in result.stdout


def test_unix_auto_never_installs_an_absent_uv_tool() -> None:
    result = _run_unix_sync("auto", tool_present=False)

    _require_bash(result)
    assert result.returncode == 0, result.stderr
    assert "UV_CALL tool install" not in result.stdout


def test_unix_always_may_install_and_never_skips() -> None:
    always = _run_unix_sync("always", tool_present=False)
    never = _run_unix_sync("never", tool_present=True)

    _require_bash(always)
    _require_bash(never)
    assert always.returncode == 0, always.stderr
    assert "UV_CALL tool install --force exomem==1.2.3" in always.stdout
    assert never.returncode == 0, never.stderr
    assert "UV_CALL tool install" not in never.stdout


def test_upgrade_scripts_defer_cli_sync_until_live_version_is_verified() -> None:
    windows = (ROOT / "scripts" / "upgrade.ps1").read_text(encoding="utf-8")
    unix = (ROOT / "scripts" / "upgrade.sh").read_text(encoding="utf-8")

    assert 'ValidateSet("auto", "always", "never")' in windows
    assert '$CliSync = "auto"' in windows
    assert "Sync-ExomemUvCli" in windows
    assert windows.index("if ($SkipRestart)") < windows.index("Sync-ExomemUvCli")
    assert windows.index("Serving version:") < windows.index("Sync-ExomemUvCli")

    assert "--cli-sync" in unix
    assert 'exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"' in unix
    assert unix.index('if [[ "$SKIP_RESTART" -eq 1 ]]') < unix.index(
        'exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"'
    )
    assert unix.index("Serving version:") < unix.index('exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"')


def test_bootstrap_upgrades_an_existing_uv_tool_instead_of_leaving_it_stale() -> None:
    install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "uv tool list" in install
    assert "uv tool upgrade exomem" in install
