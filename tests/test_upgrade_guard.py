"""#578: the deploy script must not report success while installing nothing.

`scripts/upgrade.ps1` printed `Installing exomem[embeddings,media] into ...`,
continued to the doctor preflight, and left the venv on the previous release. The
service then restarted cleanly on the old build and every later observation was
attributed to a release that was never deployed.

Two separable defects, exercised separately here:

1. `Installed version: $before -> $after` was a REPORT, not a check. `uv` exits 0
   having applied nothing (a stale index cache resolves the unpinned `--upgrade`
   back to the installed release), and the run carried on.
2. Windows PowerShell 5.1 raises a terminating NativeCommandError on the first
   merged stderr record under `$ErrorActionPreference = "Stop"`, even when the
   command exits 0 -- and `uv` writes its entire plan to stderr by design.

Defect 2 is shell-specific: it cannot reproduce under pwsh 7.x, where merged
native stderr is not an error. The `windows-powershell-5.1` parametrisation below
is the only lane that covers it, and it skips wherever `powershell.exe` is absent
(i.e. all of Linux CI). Both defects are covered on Windows; defect 1 is covered
everywhere pwsh exists.

`scripts/upgrade.sh` never had defect 2, but carried defect 1 verbatim; its lane is
at the bottom of this file.

The fakes stand in for `uv` and for the service venv's interpreter, so nothing
here touches a real environment, index, or service.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_service-common.ps1"
PWSH = shutil.which("pwsh")
WINDOWS_POWERSHELL = shutil.which("powershell") if os.name == "nt" else None

SHELLS = [
    pytest.param(
        PWSH,
        id="pwsh",
        marks=pytest.mark.skipif(PWSH is None, reason="pwsh is not available"),
    ),
    pytest.param(
        WINDOWS_POWERSHELL,
        id="windows-powershell-5.1",
        marks=pytest.mark.skipif(
            WINDOWS_POWERSHELL is None,
            reason="Windows PowerShell 5.1 is not available",
        ),
    ),
]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# `uv` writes its whole plan to stderr and nothing to stdout -- the banner, the
# resolve line, and the "+ exomem==X" plan lines. That is not incidental: it is
# what made a merged 2>&1 necessary in the first place, and what made the merge
# fatal under 5.1. The fakes reproduce it exactly.
_UV_SH = """
    #!/bin/sh
    echo "uv $*" >> "$FAKE_UV_TRACE"
    echo "Using Python 3.13.11 environment at: fake-venv" >&2
    case " $* " in
      *" --dry-run "*)
        echo "Resolved 135 packages in 1.20s" >&2
        if [ -n "$FAKE_UV_TARGET" ]; then
            echo " + exomem==$FAKE_UV_TARGET" >&2
        else
            echo "Would make no changes" >&2
        fi
        exit ${FAKE_UV_DRYRUN_EXIT:-0}
        ;;
    esac
    echo "Resolved 135 packages in 839ms" >&2
    if [ -n "$FAKE_UV_INSTALLS" ]; then
        printf '%s' "$FAKE_UV_INSTALLS" > "$FAKE_VENV_VERSION"
        echo "Installed 1 package in 429ms" >&2
        echo " + exomem==$FAKE_UV_INSTALLS" >&2
    else
        echo "Audited 135 packages in 12ms" >&2
    fi
    exit ${FAKE_UV_EXIT:-0}
"""

_UV_CMD = """
    @echo off
    echo uv %* >> "%FAKE_UV_TRACE%"
    echo Using Python 3.13.11 environment at: fake-venv 1>&2
    echo %* | findstr /C:"--dry-run" >nul
    if not errorlevel 1 (
        echo Resolved 135 packages in 1.20s 1>&2
        if defined FAKE_UV_TARGET (
            echo  + exomem==%FAKE_UV_TARGET% 1>&2
        ) else (
            echo Would make no changes 1>&2
        )
        exit /b %FAKE_UV_DRYRUN_EXIT%
    )
    echo Resolved 135 packages in 839ms 1>&2
    if defined FAKE_UV_INSTALLS (
        echo|set /p="%FAKE_UV_INSTALLS%" > "%FAKE_VENV_VERSION%"
        echo Installed 1 package in 429ms 1>&2
        echo  + exomem==%FAKE_UV_INSTALLS% 1>&2
    ) else (
        echo Audited 135 packages in 12ms 1>&2
    )
    exit /b %FAKE_UV_EXIT%
"""

# Stands in for the service venv's interpreter: `Get-ExomemInstalledVersion` runs
# `<python> -c "import importlib.metadata ..."` and reads one line of stdout.
_PYTHON_SH = """
    #!/bin/sh
    if [ -s "$FAKE_VENV_VERSION" ]; then cat "$FAKE_VENV_VERSION"; exit 0; fi
    exit 1
"""

_PYTHON_CMD = """
    @echo off
    if not exist "%FAKE_VENV_VERSION%" exit /b 1
    for %%A in ("%FAKE_VENV_VERSION%") do if %%~zA==0 exit /b 1
    type "%FAKE_VENV_VERSION%"
    exit /b 0
"""

_DRIVER = """
    # Mirrors scripts/upgrade.ps1's own preamble: the "Stop" preference is exactly
    # what turned an informational `uv` banner into an aborted deploy under 5.1.
    param([string]$Common, [string]$Python, [string]$Profile = "standard", [string]$PackageVersion = "")
    $ErrorActionPreference = "Stop"
    try {
        . $Common
        Install-ExomemPackage -Python $Python -Profile $Profile -PackageVersion $PackageVersion
    } catch {
        # Report the failure ourselves. PowerShell's own error formatter hard-wraps
        # to the host width and injects ANSI, so asserting on it is console-width
        # dependent -- which is how this suite passed on a 200-column Windows
        # console and failed on a CI runner. The error id is kept because the 5.1
        # lane asserts specifically on NativeCommandError.
        Write-Host "DRIVER-ERROR[$($_.FullyQualifiedErrorId)]: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _flat(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams, ANSI stripped and whitespace collapsed.

    Anything PowerShell renders itself (warnings especially) is wrapped to the
    host width, so a phrase can be split mid-sentence by a newline that depends on
    the terminal running the suite.
    """
    return " ".join(_ANSI.sub("", result.stdout + result.stderr).split())


def _fixture(tmp_path: Path, *, installed: str | None) -> tuple[Path, Path, dict[str, str]]:
    """Return (fake python path, uv trace path, base environment)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    version_file = tmp_path / "installed-version.txt"
    trace = tmp_path / "uv-trace.log"
    trace.touch()
    if installed is not None:
        version_file.write_text(installed, encoding="utf-8")

    # Both forms always, on every platform: PowerShell resolves `uv` through
    # PATHEXT to uv.cmd and never to the extensionless file, while Git Bash
    # resolves the shebang script and never the .cmd. Writing only one of them on
    # Windows would let the bash lane fall through to the REAL uv on PATH.
    _write_executable(bin_dir / "uv", _UV_SH)
    _write_executable(bin_dir / "python", _PYTHON_SH)
    _write_executable(bin_dir / "uv.cmd", _UV_CMD)
    _write_executable(bin_dir / "python.cmd", _PYTHON_CMD)
    python = bin_dir / ("python.cmd" if os.name == "nt" else "python")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_VENV_VERSION": str(version_file),
            "FAKE_UV_TRACE": str(trace),
            "FAKE_UV_TARGET": "",
            "FAKE_UV_INSTALLS": "",
            "FAKE_UV_EXIT": "0",
            "FAKE_UV_DRYRUN_EXIT": "0",
        }
    )
    return python, trace, env


def _install(
    shell: str,
    tmp_path: Path,
    *,
    installed: str | None,
    resolves_to: str = "",
    actually_installs: str = "",
    uv_exit: str = "0",
    dry_run_exit: str = "0",
    package_version: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    python, trace, env = _fixture(tmp_path, installed=installed)
    env.update(
        {
            "FAKE_UV_TARGET": resolves_to,
            "FAKE_UV_INSTALLS": actually_installs,
            "FAKE_UV_EXIT": uv_exit,
            "FAKE_UV_DRYRUN_EXIT": dry_run_exit,
        }
    )
    driver = tmp_path / "driver.ps1"
    driver.write_text(textwrap.dedent(_DRIVER).lstrip(), encoding="utf-8")
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver),
            "-Common",
            str(COMMON),
            "-Python",
            str(python),
            "-PackageVersion",
            package_version,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return result, trace


@pytest.mark.parametrize("shell", SHELLS)
def test_install_that_changes_nothing_fails_loudly(shell: str, tmp_path: Path) -> None:
    """The exact #578 shape: uv exits 0, applies nothing, old version survives."""
    result, _ = _install(
        shell, tmp_path, installed="0.52.2", resolves_to="0.52.3", actually_installs=""
    )

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "DRIVER-COMPLETED" not in result.stdout
    combined = _flat(result)
    assert "Install did not take" in combined
    assert "0.52.3" in combined and "0.52.2" in combined


@pytest.mark.parametrize("shell", SHELLS)
def test_install_that_lands_the_target_is_accepted(shell: str, tmp_path: Path) -> None:
    result, trace = _install(
        shell, tmp_path, installed="0.52.2", resolves_to="0.52.3", actually_installs="0.52.3"
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "DRIVER-COMPLETED" in result.stdout
    # Ask 5: the target is shown BEFORE the install, next to installed:/repo:.
    assert "target:    0.52.3" in result.stdout
    assert result.stdout.index("target:") < result.stdout.index("Installing exomem")
    calls = trace.read_text(encoding="utf-8")
    assert "--dry-run" in calls
    # A target resolved through uv's cached index would agree with a stale install.
    assert calls.count("--refresh-package exomem") == 2


@pytest.mark.parametrize("shell", SHELLS)
def test_already_current_is_not_a_failure(shell: str, tmp_path: Path) -> None:
    """uv plans no change, so before == after == target is the correct outcome.

    Without this the naive "$after -eq $before is fatal" rule would fail every
    re-run of an up-to-date box.
    """
    result, _ = _install(
        shell, tmp_path, installed="0.52.3", resolves_to="", actually_installs=""
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "DRIVER-COMPLETED" in result.stdout
    assert "target:    0.52.3" in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_uv_informational_stderr_never_aborts_the_run(shell: str, tmp_path: Path) -> None:
    """Defect 2. Under 5.1 this used to die on `Using Python ... environment at:`.

    The fake writes that banner to stderr on every invocation and exits 0, which is
    what real `uv` does. The run must complete and the banner must still be logged.
    """
    result, _ = _install(
        shell, tmp_path, installed="0.52.2", resolves_to="0.52.3", actually_installs="0.52.3"
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "DRIVER-COMPLETED" in result.stdout
    assert "Using Python 3.13.11 environment at: fake-venv" in result.stdout
    assert "NativeCommandError" not in _flat(result)


@pytest.mark.parametrize("shell", SHELLS)
def test_uv_failure_is_reported_as_a_failure_with_its_exit_code(
    shell: str, tmp_path: Path
) -> None:
    """Ask 2: 'uv returned nonzero' must stay distinguishable from 'uv no-oped'."""
    result, _ = _install(
        shell,
        tmp_path,
        installed="0.52.2",
        resolves_to="0.52.3",
        actually_installs="",
        uv_exit="2",
    )

    assert result.returncode != 0
    combined = _flat(result)
    assert "uv pip install failed" in combined
    assert "uv exit 2" in combined
    assert "Install did not take" not in combined


@pytest.mark.parametrize("shell", SHELLS)
def test_nothing_installed_at_all_is_fatal(shell: str, tmp_path: Path) -> None:
    """A fresh venv (install-service.ps1's path) that ends up empty is not success."""
    result, _ = _install(
        shell, tmp_path, installed=None, resolves_to="0.52.3", actually_installs=""
    )

    assert result.returncode != 0
    combined = _flat(result)
    assert "no exomem version is importable" in combined
    assert "not installed" in combined


@pytest.mark.parametrize("shell", SHELLS)
def test_an_explicit_pin_is_asserted_even_when_the_resolve_fails(
    shell: str, tmp_path: Path
) -> None:
    """The dry-run is a convenience; it must not be the only thing holding the gate."""
    result, _ = _install(
        shell,
        tmp_path,
        installed="0.52.2",
        resolves_to="0.52.3",
        actually_installs="",
        dry_run_exit="1",
        package_version="0.52.3",
    )

    assert result.returncode != 0
    assert "Install did not take" in _flat(result)


@pytest.mark.parametrize("shell", SHELLS)
def test_unresolvable_target_degrades_out_loud_instead_of_silently(
    shell: str, tmp_path: Path
) -> None:
    """No pin and no resolve leaves nothing to compare against. Say so."""
    result, _ = _install(
        shell,
        tmp_path,
        installed=None,
        resolves_to="0.52.3",
        actually_installs="0.52.3",
        dry_run_exit="1",
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "target:    unresolved" in result.stdout
    assert "Target version unresolved" in _flat(result)


_PARSE_DRIVER = """
    param([string]$Common, [string]$LinesJson)
    $ErrorActionPreference = "Stop"
    . $Common
    $lines = @($LinesJson | ConvertFrom-Json)
    Write-Output (ConvertTo-Json @{ version = (Get-ExomemInstallPlanVersion -Lines $lines) })
"""


def _plan_version(tmp_path: Path, lines: list[str]) -> str | None:
    assert PWSH is not None
    driver = tmp_path / "parse-driver.ps1"
    driver.write_text(textwrap.dedent(_PARSE_DRIVER).lstrip(), encoding="utf-8")
    result = subprocess.run(
        [
            PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver),
            "-Common",
            str(COMMON),
            "-LinesJson",
            json.dumps(lines),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)["version"]


@pytest.mark.skipif(PWSH is None, reason="pwsh is not available")
def test_install_plan_parsing_matches_real_uv_output(tmp_path: Path) -> None:
    """Captured verbatim from `uv 0.11.28`; extras are normalised off the name."""
    assert (
        _plan_version(
            tmp_path,
            [
                "Using Python 3.13.11 environment at: .venv",
                "Resolved 135 packages in 839ms",
                "Uninstalled 1 package in 183ms",
                "Installed 1 package in 429ms",
                " - exomem==0.52.2",
                " + exomem==0.52.3",
            ]
        )
        == "0.52.3"
    )
    assert (
        _plan_version(
            tmp_path,
            [
                "Resolved 2 packages in 101ms",
                "Checked 2 packages in 0.33ms",
                "Would make no changes",
            ],
        )
        is None
    )
    # A dependency that merely starts with the same characters is not exomem.
    assert _plan_version(tmp_path, [" + exomem-plugin==1.0.0", " + colorama==0.4.6"]) is None


# --- macOS/Linux parity -------------------------------------------------------
#
# scripts/upgrade.sh never had defect 2 -- bash does not treat stderr as failure,
# and `set -euo pipefail` already stopped a nonzero `uv` -- but it carried defect 1
# verbatim: it computed BEFORE/AFTER, printed them, and checked nothing.

BASH = shutil.which("bash")

_BASH_HARNESS = """
set -euo pipefail
. "{common}"
{body}
"""


def _bash(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    script = tmp_path / "harness.sh"
    script.write_text(
        _BASH_HARNESS.format(common=(ROOT / "scripts" / "_service-common.sh").as_posix(), body=body),
        encoding="utf-8",
    )
    result = subprocess.run(
        [BASH, str(script)], text=True, capture_output=True, check=False, env=env or os.environ.copy()
    )
    # Codex's Windows sandbox resolves Git Bash but blocks its signal-pipe
    # bootstrap; that is an execution boundary, not a script result.
    if result.returncode and "couldn't create signal pipe" in result.stdout + result.stderr:
        pytest.skip("sandbox blocked Git Bash startup")
    return result


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_unix_assert_rejects_an_install_that_changed_nothing(tmp_path: Path) -> None:
    result = _bash(
        tmp_path,
        'exomem_assert_install_applied "exomem[embeddings,media]" "0.52.2" "0.52.2" "0.52.3"',
    )

    assert result.returncode != 0
    assert "install did not take" in result.stderr
    assert "0.52.3" in result.stderr


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_unix_assert_accepts_a_box_that_is_already_current(tmp_path: Path) -> None:
    result = _bash(
        tmp_path,
        'exomem_assert_install_applied "exomem" "0.52.3" "0.52.3" "0.52.3"; echo HARNESS-OK',
    )

    assert result.returncode == 0, result.stderr
    assert "HARNESS-OK" in result.stdout


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_unix_assert_rejects_an_empty_interpreter(tmp_path: Path) -> None:
    result = _bash(tmp_path, 'exomem_assert_install_applied "exomem" "" "" "0.52.3"')

    assert result.returncode != 0
    assert "no exomem version is importable" in result.stderr
    assert "not installed" in result.stderr


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_unix_target_resolution_reads_uv_and_refreshes_the_index(tmp_path: Path) -> None:
    python, trace, env = _fixture(tmp_path, installed="0.52.2")
    env["FAKE_UV_TARGET"] = "0.52.3"
    result = _bash(
        tmp_path,
        f'exomem_resolve_target_version "{python.as_posix()}" "exomem[embeddings,media]" "0.52.2"',
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.52.3"
    assert "--refresh-package exomem" in trace.read_text(encoding="utf-8")


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_unix_target_resolution_falls_back_to_the_installed_version(tmp_path: Path) -> None:
    """uv planning no change means the resolved target IS what is already there."""
    python, _, env = _fixture(tmp_path, installed="0.52.3")
    env["FAKE_UV_TARGET"] = ""
    result = _bash(
        tmp_path,
        f'exomem_resolve_target_version "{python.as_posix()}" "exomem" "0.52.3"',
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.52.3"


def test_unix_upgrade_wires_the_stopped_transition_before_cli_sync() -> None:
    """The target only reaches CLI synchronization after a complete stopped transition."""
    upgrade = (ROOT / "scripts" / "upgrade.sh").read_text(encoding="utf-8")

    assert "exomem_resolve_target_version" in upgrade
    assert "exomem_assert_install_applied" in upgrade
    assert upgrade.index("exomem_resolve_target_version") < upgrade.index("Installing $REQUIREMENT")
    assert upgrade.index("Installed version:") < upgrade.index("exomem_assert_install_applied")
    assert upgrade.index("exomem_assert_install_applied") < upgrade.index("Offline state migration...")
    assert upgrade.index("Offline state migration...") < upgrade.index("Preflight: exomem doctor")
    assert upgrade.index("Preflight: exomem doctor") < upgrade.index("Starting $SERVICE_ID...")
    assert upgrade.index("Starting $SERVICE_ID...") < upgrade.index("Serving version:")
    assert upgrade.index("Serving version:") < upgrade.index('exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"')
