"""A deploy must not report success while the old interpreter is still serving.

`scripts/deploy.ps1` step 5 is titled "Verify the RUNNING process, not the
installer" and polls `/health`. But `/health` reports
`importlib.metadata.version("exomem")`, read from disk at request time with no
relation to the code the running interpreter loaded. Observed live: `uv pip
install` swapped the wheel under a process that had been up since 12:20:10Z;
`/health` answered `0.53.0` while the interpreter still ran 0.52.3, and in fact
ran a *mixed* build, because modules imported lazily after the swap came from
the new wheel. Every latency measurement taken that day was attributed to a
release that was never running.

The same blindness is structural, not incidental: `install-info --json` and
`/health` both read the same distribution metadata, so any gate comparing one
against the other compares disk with disk and cannot fail for this reason. The
worker process identity is the only observable that separates "restarted onto
the new code" from "still running the old code".

Two gates are exercised here, both as pure before/after assertions so they run
everywhere pwsh does rather than needing a real Windows service:

1. `Assert-ExomemServiceRestarted` — the restart gate this adds.
2. `Test-ExomemAcceleratedTorch` — the accelerator gate, which read
   `.accelerated` off `install-info --json`. That key has never been emitted, so
   the property was always $null, `[bool]$null` is $false, and the guard
   documented as "a hard failure by default" could not fire at all.
"""

from __future__ import annotations

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
RESTART = ROOT / "scripts" / "restart.ps1"
DEPLOY = ROOT / "scripts" / "deploy.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh is not available")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_RESTART_DRIVER = """
    param([string]$Common, [int]$Before, [int]$After)
    $ErrorActionPreference = "Stop"
    try {
        . $Common
        Assert-ExomemServiceRestarted -Before $Before -After $After -ServiceName "exomem"
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_TORCH_DRIVER = """
    param([string]$Common, [string]$Python)
    $ErrorActionPreference = "Stop"
    . $Common
    $version = Get-ExomemTorchVersion -PythonPath $Python
    $accel = Test-ExomemAcceleratedTorch -PythonPath $Python
    Write-Host "VERSION=$version"
    Write-Host "ACCEL=$accel"
"""

# Stands in for the service venv's interpreter: the probes run
# `<python> -c "import importlib.metadata ..."` and read one line of stdout.
_PYTHON_SH = """
    #!/bin/sh
    if [ -n "$FAKE_TORCH_VERSION" ]; then echo "$FAKE_TORCH_VERSION"; exit 0; fi
    exit 1
"""

_PYTHON_CMD = """
    @echo off
    if not defined FAKE_TORCH_VERSION exit /b 1
    echo %FAKE_TORCH_VERSION%
    exit /b 0
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _flat(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams, ANSI stripped and whitespace collapsed.

    PowerShell wraps what it renders itself to the host width, so a phrase can be
    split mid-sentence by a newline that depends on the terminal running it.
    """
    return " ".join(_ANSI.sub("", result.stdout + result.stderr).split())


def _run(script: Path, *args: str, env: dict[str, str] | None = None):
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


@pytest.fixture()
def restart_driver(tmp_path: Path) -> Path:
    script = tmp_path / "restart-driver.ps1"
    _write_executable(script, _RESTART_DRIVER)
    return script


class TestRestartGate:
    def test_an_unchanged_worker_pid_fails_the_deploy(self, restart_driver: Path) -> None:
        """The live defect: wheel replaced, process never reloaded."""
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "27372", "-After", "27372")
        flat = _flat(result)
        assert result.returncode == 1, flat
        assert "still running the same worker process" in flat, flat
        assert "27372" in flat, flat

    def test_a_changed_worker_pid_is_accepted(self, restart_driver: Path) -> None:
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "27372", "-After", "31005")
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "DRIVER-COMPLETED" in flat, flat
        assert "27372 -> 31005" in flat, flat

    def test_no_worker_after_restart_is_fatal(self, restart_driver: Path) -> None:
        """A service that came back with nothing running is not a success."""
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "27372", "-After", "0")
        flat = _flat(result)
        assert result.returncode == 1, flat
        assert "no running worker process" in flat, flat

    def test_an_unknown_baseline_warns_instead_of_stranding_the_box(
        self, restart_driver: Path
    ) -> None:
        """Refusing every deploy where the probe is unavailable is worse."""
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "0", "-After", "31005")
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "DRIVER-COMPLETED" in flat, flat
        assert "No pre-restart worker pid" in flat, flat


class TestAcceleratorProbe:
    @pytest.fixture()
    def probe(self, tmp_path: Path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # Both forms on every platform: PowerShell resolves through PATHEXT to
        # the .cmd and never the extensionless file.
        _write_executable(bin_dir / "python", _PYTHON_SH)
        _write_executable(bin_dir / "python.cmd", _PYTHON_CMD)
        python = bin_dir / ("python.cmd" if os.name == "nt" else "python")
        script = tmp_path / "torch-driver.ps1"
        _write_executable(script, _TORCH_DRIVER)

        def run(torch_version: str | None):
            env = os.environ.copy()
            env["FAKE_TORCH_VERSION"] = torch_version or ""
            return _flat(_run(script, "-Common", str(COMMON), "-Python", str(python), env=env))

        return run

    @pytest.mark.parametrize(
        "version",
        ["2.13.0+cu132", "2.9.1+rocm6.2", "2.8.0+xpu"],
    )
    def test_an_accelerator_build_is_detected(self, probe, version: str) -> None:
        flat = probe(version)
        assert "ACCEL=True" in flat, flat
        assert f"VERSION={version}" in flat, flat

    def test_a_plain_pypi_wheel_reads_as_cpu_only(self, probe) -> None:
        """No local tag is exactly what a silent CPU downgrade looks like."""
        flat = probe("2.13.0")
        assert "ACCEL=False" in flat, flat

    def test_absent_torch_is_not_an_accelerator(self, probe) -> None:
        flat = probe(None)
        assert "ACCEL=False" in flat, flat


class TestScriptsAreWired:
    """The helpers only matter if the scripts actually call them."""

    def test_restart_asserts_the_worker_changed_around_the_restart(self) -> None:
        body = RESTART.read_text(encoding="utf-8")
        assert "Get-ExomemServiceWorkerPid" in body
        assert "Assert-ExomemServiceRestarted" in body
        # The baseline is worthless if it is read after the stop.
        assert body.index("$workerBefore = Get-ExomemServiceWorkerPid") < body.index(
            "sc.exe stop"
        ), "the pre-restart worker pid must be captured before the service stops"

    def test_restart_resolves_the_log_dir_instead_of_assuming_the_checkout(self) -> None:
        """#569 moved logs off <repo>/logs; the script kept its own stale copy."""
        body = RESTART.read_text(encoding="utf-8")
        assert "Get-ExomemLogDir" in body
        assert '$logDir = Join-Path (Split-Path -Parent $PSScriptRoot) "logs"' not in body.split(
            "Write-Warning"
        )[0], "the checkout path must be a fallback, not the primary resolution"

    def test_deploy_probes_torch_rather_than_a_key_install_info_never_emits(self) -> None:
        body = DEPLOY.read_text(encoding="utf-8")
        assert "Test-ExomemAcceleratedTorch" in body
        assert "$before.accelerated" not in body
        assert "$after.accelerated" not in body
