"""Install the independent read-only observer and inspect its local evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
import math
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .recall_canary import _canary_cases, argument_parser, validate_target
from .service_manager import private_directory

# These names belong to this adapter's fixed systemd protocol, not vault vocabulary.
SERVICE = "exomem-recall-canary.service"
TIMER = "exomem-recall-canary.timer"
BUS = "org.freedesktop.systemd1"
MANAGER = "/org/freedesktop/systemd1"
# Offline inspection reads only bounded configuration and the last observer report.
MAX_OFFLINE_BYTES = 1024 * 1024


def unit_directory() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"


def _private_file(path: Path) -> None:
    # A wrong refusal leaves this observer unconfigured; admission and runtime stay untouched.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("owner-only regular file required")


def _quote(value) -> str:
    # systemd.syntax accepts these JSON C-style string escapes; %% escapes its specifiers.
    # ExecStart's ':' prefix disables environment expansion, so '$' remains literal.
    return json.dumps(str(value).replace("%", "%%"), ensure_ascii=False)


def render_units(
    *,
    observer_python: Path,
    environment_file: Path,
    cases: Path,
    mcp_url: str,
    report_file: Path,
    token_env: str = "EXOMEM_MCP_TOKEN",
    interval: float = 300,
    call_timeout: float = 30,
    execution_timeout: float = 120,
) -> dict[str, str]:
    if any(
        not math.isfinite(value) or value <= 0
        for value in (interval, call_timeout, execution_timeout)
    ):
        raise ValueError("finite positive observation bounds required")
    paths = (observer_python, environment_file, cases, report_file)
    if not all(path.is_absolute() for path in paths):
        raise ValueError("absolute configuration paths required")
    values = {
        name: _quote(value)
        for name, value in {
            "observer_python": observer_python,
            "environment_file": environment_file,
            "cases": cases,
            "mcp_url": mcp_url,
            "report_file": report_file,
            "token_env": token_env,
        }.items()
    }
    # EnvironmentFile consumes one literal path, unlike ExecStart's quoted words.
    environment_path = str(environment_file)
    if (
        environment_path != environment_path.strip()
        or "\n" in environment_path
        or "\r" in environment_path
        or environment_path.endswith("\\")
    ):
        raise ValueError("environment path cannot be represented by the unit directive")
    values["environment_file"] = environment_path.replace("%", "%%")
    values.update(interval=interval, call_timeout=call_timeout, execution_timeout=execution_timeout)
    templates = importlib.resources.files("exomem").joinpath("_service")
    return {
        name: templates.joinpath(name).read_text().format(**values) for name in (SERVICE, TIMER)
    }


def _run(arguments, *, timeout=10):
    return subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=timeout)


def _properties(unit: str, interface: str) -> dict:
    # LoadUnit reads the definition without starting it; installed inactive units may be uncached.
    path = json.loads(
        _run(
            [
                "busctl",
                "--user",
                "--json=short",
                "call",
                BUS,
                MANAGER,
                BUS + ".Manager",
                "LoadUnit",
                "s",
                unit,
            ]
        ).stdout
    )["data"][0]
    reply = json.loads(
        _run(
            [
                "busctl",
                "--user",
                "--json=short",
                "call",
                BUS,
                path,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                "s",
                BUS + "." + interface,
            ]
        ).stdout
    )
    # busctl's typed JSON retains each variant's data; no unit-text grammar is parsed.
    return {key: value["data"] for key, value in reply["data"][0].items()}


def _configuration(service: dict):
    commands = service["ExecStart"]
    if len(commands) != 1:
        raise ValueError("one observer command required")
    arguments = commands[0][1]
    # This module fixes the installed observer's CLI protocol.
    if arguments[1:4] != ["-I", "-m", "exomem.recall_canary"]:
        raise ValueError("installed observer command required")
    args, unknown = argument_parser(add_help=False, exit_on_error=False).parse_known_args(
        arguments[4:]
    )
    files = service["EnvironmentFiles"]
    if unknown or len(files) != 1 or files[0][1] or not args.cases or not args.report_file:
        raise ValueError("observer configuration is incomplete")
    environment = Path(files[0][0])
    _private_file(environment)
    _private_file(args.cases)
    if args.cases.stat().st_size > MAX_OFFLINE_BYTES:
        raise ValueError("case file exceeds the bounded offline read")
    validate_target(args.mcp_url)
    _canary_cases(args.cases.read_bytes())
    return Path(commands[0][0]), environment, args


def _latest(path: Path) -> dict:
    _private_file(path)
    if path.stat().st_size > MAX_OFFLINE_BYTES:
        raise ValueError("observer report exceeds the bounded offline read")
    report = json.loads(path.read_text())
    if not isinstance(report, dict):
        raise ValueError("invalid observer report")
    return report


def status(*, unit_dir: Path | None = None, now: float | None = None) -> dict:
    directory = unit_dir or unit_directory()
    base = {
        "state": "unavailable",
        "delivery_reach": "local-unit-journal-doctor-only",
        "report_age_seconds": None,
    }
    if not any((directory / name).exists() for name in (SERVICE, TIMER)):
        return {**base, "state": "absent"}
    try:
        unit = _properties(SERVICE, "Unit")
        timer = _properties(TIMER, "Unit")
        service = _properties(SERVICE, "Service")
        schedule = _properties(TIMER, "Timer")
    except Exception:  # noqa: BLE001 - absent tools/bus and private D-Bus errors stay unavailable
        return base
    try:
        _, _, args = _configuration(service)
    except Exception:  # noqa: BLE001 - never expose private paths or unit argument values
        return {**base, "state": "unconfigured"}
    # UnitFileState, ActiveState and Result are systemd's documented closed codes.
    if timer.get("UnitFileState") not in ("enabled", "enabled-runtime"):
        return {**base, "state": "disabled"}
    if timer.get("ActiveState") == "failed":
        return {**base, "state": "failed"}
    if timer.get("ActiveState") != "active":
        return {**base, "state": "stopped"}
    if unit.get("ActiveState") == "activating":
        return {**base, "state": "running"}
    failed = unit.get("ActiveState") == "failed" or service.get("Result") != "success"
    try:
        report = _latest(args.report_file)
        # missing-token is the canary protocol's explicit configuration outcome.
        if report.get("failure") == "missing-token":
            return {**base, "state": "unconfigured"}
        ended = report["ended_at"]
        if type(ended) not in (float, int) or not math.isfinite(ended):
            raise ValueError("unknown report window")
        age = (time.time() if now is None else now) - ended
        if math.isfinite(age) and age >= 0:
            base["report_age_seconds"] = age
        started = service["ExecMainStartTimestamp"] / 1_000_000
        invocation = bytes(unit["InvocationID"]).hex()
        current = (
            report.get("execution_id_sha256") == hashlib.sha256(invocation.encode()).hexdigest()
        )
        current = current and ended >= started
        current = (
            current
            and report.get("target_sha256") == hashlib.sha256(args.mcp_url.encode()).hexdigest()
        )
        current = (
            current
            and report.get("cases_sha256") == hashlib.sha256(args.cases.read_bytes()).hexdigest()
        )
        successful = (
            report.get("check") == "availability" and report.get("check_verdict") == "passed"
        )
        if failed:
            return {
                **base,
                "state": "old-success-after-failed-execution"
                if successful and not current
                else "failed",
            }
        if not current:
            return {**base, "state": "stale"}
        intervals = [
            row[1] / 1_000_000
            for row in schedule["TimersMonotonic"]
            if row[0] == "OnUnitInactiveUSec"
        ]
        bound = max(intervals) + service["TimeoutStartUSec"] / 1_000_000
        if base["report_age_seconds"] is None or age > bound:
            return {**base, "state": "stale"}
        return {
            **base,
            "state": "healthy-availability" if successful else "failed",
            "report_age_seconds": age,
        }
    except Exception:  # noqa: BLE001 - missing/malformed reports never turn into healthy evidence
        return {**base, "state": "failed" if failed else "stale"}


def install(*, unit_dir: Path | None = None, **configuration) -> dict:
    _private_file(configuration["environment_file"])
    _private_file(configuration["cases"])
    validate_target(configuration["mcp_url"])
    _canary_cases(configuration["cases"].read_bytes())
    private_directory(configuration["report_file"].parent)
    # -I verifies the explicitly selected observer without checkout or user-site imports.
    identity = json.loads(
        _run(
            [
                str(configuration["observer_python"]),
                "-I",
                "-c",
                "import json; from exomem.install_info import package_identity; "
                "print(json.dumps(package_identity('installed', role='observer-only')))",
            ]
        ).stdout
    )
    units = render_units(**configuration)
    # Native verification rejects a unit that could never run. A wrong refusal
    # leaves only this observer unconfigured; it never changes runtime admission.
    with tempfile.TemporaryDirectory(prefix="exomem-canary-unit-") as scratch:
        for name, content in units.items():
            Path(scratch, name).write_text(content)
        _run(
            [
                "systemd-analyze",
                "--user",
                "verify",
                "--recursive-errors=no",
                *(str(Path(scratch, name)) for name in units),
            ]
        )
    directory = unit_dir or unit_directory()
    if directory.is_symlink():
        raise ValueError("unit directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any((directory / name).exists() for name in (SERVICE, TIMER)):
        _run(["systemctl", "--user", "disable", "--now", TIMER])
        _run(["systemctl", "--user", "stop", SERVICE])
    for name, content in units.items():
        descriptor, temporary = tempfile.mkstemp(prefix=".canary-unit-", dir=directory)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / name)
        finally:
            Path(temporary).unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return {"state": "disabled", "observer": identity, "credential": "unverified"}


def enable() -> dict:
    observer, environment, args = _configuration(_properties(SERVICE, "Service"))
    # systemd reads its own EnvironmentFile grammar. The child returns presence only,
    # so missing credentials leave monitoring unconfigured without copying a secret.
    # This property is one raw argv value; systemd-run must receive no unit-file quoting.
    try:
        _run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--wait",
                "--collect",
                "--pipe",
                "--expand-environment=no",
                "--property=Type=oneshot",
                "--property=TimeoutStartSec=10s",
                "--property=EnvironmentFile=" + str(environment),
                str(observer),
                "-I",
                "-c",
                "import os,sys; sys.exit(0 if os.environ.get(sys.argv[1], '').strip() else 78)",
                args.token_env,
            ],
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - native credential loading errors can include private paths
        return {"state": "unconfigured", "failure": "credential-load-failed"}
    _run(["systemctl", "--user", "enable", "--now", TIMER])
    return {"state": "scheduled", "delivery_reach": "local-unit-journal-doctor-only"}


def uninstall(*, unit_dir: Path | None = None) -> dict:
    _run(["systemctl", "--user", "disable", "--now", TIMER])
    _run(["systemctl", "--user", "stop", SERVICE])
    for name in (SERVICE, TIMER):
        ((unit_dir or unit_directory()) / name).unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return {"state": "absent"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "enable", "status", "uninstall"))
    for name in ("observer-python", "environment-file", "cases", "report-file"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--mcp-url")
    parser.add_argument("--token-env", default="EXOMEM_MCP_TOKEN")
    parser.add_argument("--interval", type=float, default=300)
    parser.add_argument("--call-timeout", type=float, default=30)
    parser.add_argument("--execution-timeout", type=float, default=120)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        result = (
            install(**args)
            if command == "install"
            else {"enable": enable, "status": status, "uninstall": uninstall}[command]()
        )
    except Exception:  # noqa: BLE001 - configuration, process errors and private values stay local
        result = {"state": "unconfigured", "failure": "invalid-configuration"}
    print(json.dumps(result, sort_keys=True))
    return (
        0 if result["state"] in ("disabled", "scheduled", "healthy-availability", "absent") else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
