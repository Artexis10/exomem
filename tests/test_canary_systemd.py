"""The observer's user units and offline evidence never mutate the runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from exomem import canary_systemd as monitor
from exomem import doctor


@pytest.fixture
def configuration(tmp_path):
    environment = tmp_path / 'private $token% "env".conf'
    environment.write_text("TEST_TOKEN=hidden\n")
    environment.chmod(0o600)
    cases = tmp_path / "cases with spaces.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "case-01",
                    "tool": "ask_memory",
                    "arguments": {},
                    "expected": {
                        "state_path": ["ok"],
                        "state": True,
                        "outcome": "ok",
                        "references": [{"path": ["hits", 0, "ref"], "value": "ref-01"}],
                        "material_paths": [["hits", 0, "text"]],
                    },
                }
            ]
        )
    )
    cases.chmod(0o600)
    observer = tmp_path / "observer $token% ;python"
    observer.symlink_to(sys.executable)
    return dict(
        observer_python=observer,
        environment_file=environment,
        cases=cases,
        mcp_url="https://memory.invalid/mcp?x=$literal%25",
        report_file=tmp_path / "latest with spaces.json",
        token_env="TEST_TOKEN",
    )


def test_systemd_verifies_rendered_units_with_literal_metacharacters(tmp_path, configuration):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze is unavailable")
    units = monitor.render_units(**configuration)
    for name, text in units.items():
        (tmp_path / name).write_text(text)
    with tempfile.TemporaryDirectory(prefix="ec-") as runtime:
        verified = subprocess.run(
            [
                "systemd-analyze",
                "--user",
                "verify",
                "--recursive-errors=no",
                str(tmp_path / monitor.SERVICE),
                str(tmp_path / monitor.TIMER),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "XDG_RUNTIME_DIR": runtime},
        )
    assert verified.returncode == 0, verified.stderr
    assert "TEST_TOKEN=hidden" not in units[monitor.SERVICE]


@pytest.fixture
def installed(tmp_path, configuration, monkeypatch):
    units = tmp_path / "units"
    units.mkdir()
    for name in (monitor.SERVICE, monitor.TIMER):
        (units / name).touch(mode=0o600)
    invocation = bytes.fromhex("1" * 32)
    report = dict(
        check="availability",
        check_verdict="passed",
        ended_at=100,
        execution_id_sha256=hashlib.sha256(invocation.hex().encode()).hexdigest(),
        target_sha256=hashlib.sha256(configuration["mcp_url"].encode()).hexdigest(),
        cases_sha256=hashlib.sha256(configuration["cases"].read_bytes()).hexdigest(),
    )
    configuration["report_file"].write_text(json.dumps(report))
    configuration["report_file"].chmod(0o600)
    arguments = [
        str(configuration["observer_python"]),
        "-I",
        "-m",
        "exomem.recall_canary",
        "--check",
        "availability",
        "--mcp-url",
        configuration["mcp_url"],
        "--cases",
        str(configuration["cases"]),
        "--report-file",
        str(configuration["report_file"]),
        "--token-env",
        "TEST_TOKEN",
    ]
    properties = {
        (monitor.SERVICE, "Unit"): dict(ActiveState="inactive", InvocationID=list(invocation)),
        (monitor.SERVICE, "Service"): dict(
            ExecStart=[[str(configuration["observer_python"]), arguments]],
            EnvironmentFiles=[[str(configuration["environment_file"]), False]],
            Result="success",
            ExecMainStartTimestamp=99_000_000,
            TimeoutStartUSec=120_000_000,
        ),
        (monitor.TIMER, "Unit"): dict(UnitFileState="enabled", ActiveState="active"),
        (monitor.TIMER, "Timer"): dict(TimersMonotonic=[["OnUnitInactiveUSec", 300_000_000, 0]]),
    }
    monkeypatch.setattr(
        monitor, "_properties", lambda unit, interface: properties[(unit, interface)]
    )
    return SimpleNamespace(
        units=units, configuration=configuration, properties=properties, report=report
    )


@pytest.mark.parametrize(
    "state",
    ["disabled", "failed", "stale", "old-success-after-failed-execution", "healthy-availability"],
)
def test_offline_status_distinguishes_execution_from_latest_report(installed, state):
    properties = installed.properties
    if state == "disabled":
        properties[(monitor.TIMER, "Unit")]["UnitFileState"] = "disabled"
    elif state in ("failed", "old-success-after-failed-execution"):
        properties[(monitor.SERVICE, "Service")]["Result"] = "exit-code"
        if state == "old-success-after-failed-execution":
            properties[(monitor.SERVICE, "Unit")]["InvocationID"] = [2] * 16
    now = 600 if state == "stale" else 110
    observed = monitor.status(unit_dir=installed.units, now=now)
    assert observed["state"] == state
    assert "private" not in json.dumps(observed)
    assert "latest with spaces" not in json.dumps(observed)


def test_absent_units_and_unavailable_manager_have_distinct_states(tmp_path, monkeypatch):
    assert monitor.status(unit_dir=tmp_path)["state"] == "absent"
    (tmp_path / monitor.SERVICE).touch()
    monkeypatch.setattr(
        monitor, "_properties", lambda *args: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert monitor.status(unit_dir=tmp_path)["state"] == "unavailable"


def test_typed_busctl_json_keeps_environment_paths_with_spaces(monkeypatch):
    def run(arguments):
        if "GetUnit" in arguments:
            return SimpleNamespace(stdout=json.dumps({"type": "o", "data": ["/unit/path"]}))
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "type": "a{sv}",
                    "data": [
                        {
                            "EnvironmentFiles": {
                                "type": "a(sb)",
                                "data": [["/private/with spaces.env", False]],
                            },
                            "Result": {"type": "s", "data": "success"},
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(monitor, "_run", run)
    properties = monitor._properties(monitor.SERVICE, "Service")
    assert properties["EnvironmentFiles"] == [["/private/with spaces.env", False]]
    assert properties["Result"] == "success"


def test_changed_case_truth_invalidates_a_prior_success(installed):
    installed.configuration["cases"].write_text(installed.configuration["cases"].read_text() + "\n")
    assert monitor.status(unit_dir=installed.units, now=110)["state"] == "stale"


def test_missing_environment_file_is_unconfigured(installed):
    installed.configuration["environment_file"].unlink()
    assert monitor.status(unit_dir=installed.units, now=110)["state"] == "unconfigured"


def test_last_execution_with_missing_credential_remains_unconfigured(installed):
    installed.configuration["report_file"].write_text(json.dumps({"failure": "missing-token"}))
    installed.properties[(monitor.SERVICE, "Service")]["Result"] = "exit-code"
    assert monitor.status(unit_dir=installed.units, now=110)["state"] == "unconfigured"


def test_enable_checks_loaded_credential_and_keeps_target_failures_scheduled(
    installed, monkeypatch
):
    commands = []
    monkeypatch.setattr(monitor, "_run", lambda command, **kwargs: commands.append(command))
    installed.properties[(monitor.SERVICE, "Service")]["Result"] = "exit-code"
    assert monitor.enable()["state"] == "scheduled"
    assert any(command[0] == "systemd-run" and "TEST_TOKEN" in command for command in commands)
    assert ["systemctl", "--user", "enable", "--now", monitor.TIMER] in commands


def test_missing_loaded_credential_leaves_timer_unconfigured(installed, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(78, command, stderr="private env path")

    monkeypatch.setattr(monitor, "_run", run)
    observed = monitor.enable()
    assert observed["state"] == "unconfigured"
    assert not any("enable" in command for command in commands)
    assert "private" not in json.dumps(observed)


def test_install_leaves_owned_units_private_and_observer_independently_pinned(
    tmp_path, configuration, monkeypatch
):
    def run(command, **kwargs):
        return SimpleNamespace(
            stdout=json.dumps({"mode": "installed", "version": "9.0.0", "role": "observer-only"})
        )

    monkeypatch.setattr(monitor, "_run", run)
    units = tmp_path / "units"
    observed = monitor.install(unit_dir=units, **configuration)
    assert observed["state"] == "disabled"
    assert observed["observer"]["version"] == "9.0.0"
    assert all(
        (units / name).stat().st_mode & 0o777 == 0o600 for name in (monitor.SERVICE, monitor.TIMER)
    )
    configuration["environment_file"].chmod(0o644)
    with pytest.raises(ValueError):
        monitor.install(unit_dir=units, **configuration)


def test_doctor_surfaces_the_adapter_state_without_a_probe(monkeypatch):
    monkeypatch.setattr(monitor, "status", lambda: {"state": "old-success-after-failed-execution"})
    check = doctor._check_recall_canary()
    assert check.id == "recall.canary"
    assert check.status == "fail"
    assert "old-success-after-failed-execution" in check.message
