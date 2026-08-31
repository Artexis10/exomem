#!/usr/bin/env python3
"""Desk-side persistent-core health/readiness/resource-status acceptance check.

Run after the media idle deadline. The script is stdlib-only and intentionally
measures OS processes rather than importing Exomem or torch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exomem import process_memory, runtime_resources


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    rss_mb: float
    memory_mb: float
    memory_metric: str
    physical_footprint_mb: float | None
    cpu_percent: float
    command: str


def _posix_rows() -> list[ProcessRow]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,rss=,%cpu=,command="],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    rows: list[ProcessRow] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            rss_mb = round(int(parts[1]) / 1024, 1)
            rows.append(
                ProcessRow(
                    pid=int(parts[0]),
                    rss_mb=rss_mb,
                    **process_memory.enrich_process_memory(int(parts[0]), rss_mb),
                    cpu_percent=float(parts[2]),
                    command=parts[3],
                )
            )
        except ValueError:
            continue
    return rows


def _windows_rows() -> list[ProcessRow]:
    script = r"""
$cmd = @{}
Get-CimInstance Win32_Process | ForEach-Object { $cmd[[int]$_.ProcessId] = $_.CommandLine }
Get-CimInstance Win32_PerfFormattedData_PerfProc_Process |
  Where-Object { $_.IDProcess -gt 0 } |
  ForEach-Object {
    [pscustomobject]@{
      pid = [int]$_.IDProcess
      rss_mb = [math]::Round([double]$_.WorkingSetPrivate / 1MB, 1)
      cpu_percent = [double]$_.PercentProcessorTime
      command = [string]$cmd[[int]$_.IDProcess]
    }
  } | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    payload = json.loads(result.stdout)
    if isinstance(payload, dict):
        payload = [payload]
    rows: list[ProcessRow] = []
    for row in payload:
        pid = int(row["pid"])
        rss_mb = float(row["rss_mb"])
        rows.append(ProcessRow(
            pid=pid,
            rss_mb=rss_mb,
            **process_memory.enrich_process_memory(pid, rss_mb),
            cpu_percent=float(row["cpu_percent"]),
            command=str(row.get("command") or ""),
        ))
    return rows


def process_rows() -> list[ProcessRow]:
    return _windows_rows() if os.name == "nt" else _posix_rows()


def _is_server(row: ProcessRow) -> bool:
    command = row.command.lower()
    return "exomem" in command and "--transport" in command and "media_worker_child" not in command


def _is_media_worker(row: ProcessRow) -> bool:
    return "exomem.media_worker_child" in row.command.lower()


def _isolated_sample_environment(sample_vault: Path, state_root: Path) -> dict[str, str]:
    """Allowlist only the local state needed by active-gate children."""
    home = state_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home.resolve()),
        "EXOMEM_VAULT_PATH": str(sample_vault.resolve()),
        "EXOMEM_STATE_ROOT": str(state_root.resolve()),
        "EXOMEM_HOSTED_CELL": "0",
        "EXOMEM_DISABLE_FILE_WATCHER": "1",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_WRITER_LEASE_PREFERRED": "0",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str((state_root / "writer-lease").resolve()),
        "EXOMEM_WIDE_MUTATION_BOUNDARY": "0",
    }
    if pythonpath := os.environ.get("PYTHONPATH"):
        environment["PYTHONPATH"] = os.pathsep.join(
            str((Path.cwd() / item).resolve()) if not Path(item).is_absolute() else item
            for item in pythonpath.split(os.pathsep)
            if item
        )
    return environment


def _transient_supervisor_command(
    *, unit: str, quota: str, environment: dict[str, str], supervisor: str, command: list[str]
) -> list[str]:
    """Build a transient unit whose supervisor starts from a blank environment."""
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        f"--property=WorkingDirectory={environment['EXOMEM_STATE_ROOT']}",
        f"--property=CPUWeight={runtime_resources.SYSTEMD_CPU_WEIGHT}",
        f"--property=CPUQuota={quota}",
        "/usr/bin/env",
        "-i",
        *(f"{name}={value}" for name, value in sorted(environment.items())),
        sys.executable,
        "-c",
        supervisor,
        *command,
    ]


def _cleanup_unit(unit: str) -> str | None:
    """Bound stopping a transient unit, escalating to kill only when needed."""
    failures: list[str] = []
    try:
        stopped = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if stopped.returncode == 0:
            return None
        failures.append((stopped.stderr or stopped.stdout).strip() or "systemctl stop failed")
    except subprocess.TimeoutExpired:
        failures.append("systemctl stop timed out")
    try:
        killed = subprocess.run(
            ["systemctl", "--user", "kill", unit],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if killed.returncode:
            failures.append((killed.stderr or killed.stdout).strip() or "systemctl kill failed")
    except subprocess.TimeoutExpired:
        failures.append("systemctl kill timed out")
    return "; ".join(failures) if failures else None


def _resource_status_probe(
    executable: str,
    sample_vault: Path,
    *,
    environment: dict[str, str] | None = None,
    quota: str | None = None,
) -> float:
    """Time allocation-free resource status against the isolated sample vault."""
    started_at = time.monotonic()
    probe_environment = environment or os.environ | {
        "EXOMEM_VAULT_PATH": str(sample_vault.resolve())
    }
    result = subprocess.run(
        [
            executable,
            "-m",
            "exomem",
            "status",
            "--resources",
            "--json",
            "--vault",
            str(sample_vault.resolve()),
        ],
        env=probe_environment,
        cwd=str(Path(probe_environment.get("EXOMEM_STATE_ROOT", Path.cwd())).resolve()),
        text=True,
        capture_output=True,
        check=False,
        timeout=1,
    )
    latency = time.monotonic() - started_at
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "resource status probe failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("resource status probe did not return valid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("compute"), dict):
        raise RuntimeError("resource status probe did not return resource JSON")
    compute = payload["compute"]
    expected = {
        "cpu_threads": 1,
        "cpu_source": "default",
        "sync_workers": 8,
        "sync_source": "default",
        "model_admission": 4,
        "native_overrides_unsafe": False,
    }
    if any(compute.get(name) != value for name, value in expected.items()):
        raise RuntimeError("resource status probe policy differs from transient envelope")
    systemd = compute.get("systemd")
    if (
        not isinstance(systemd, dict)
        or systemd.get("cpu_weight") != runtime_resources.SYSTEMD_CPU_WEIGHT
        or (quota is not None and systemd.get("cpu_quota") != quota)
    ):
        raise RuntimeError("resource status probe quota differs from transient envelope")
    if latency >= 1:
        raise RuntimeError("resource status probe exceeds 1 second")
    return latency


def _active_cgroup_gate(
    *,
    command: list[str] | None,
    sample_vault: str | None,
    health_url: str | None,
    ready_url: str | None,
    seconds: float,
) -> dict[str, object]:
    """Exercise an explicit sample-vault server in a real transient user cgroup.

    This is deliberately opt-in: the caller supplies the isolated sample-vault
    server command and its health/readiness endpoints. It also verifies the
    allocation-free resource-status command while the cgroup is noisy. Unsupported
    hosts report an unsupported result rather than treating the active gate as a pass.
    """
    if sys.platform != "linux" or not shutil.which("systemd-run") or not Path("/usr/bin/env").is_file():
        return {"supported": False, "reason": "systemd transient user units are unavailable"}
    if not command or not sample_vault or not health_url or not ready_url:
        return {
            "supported": False,
            "reason": "sample vault, command, and health/ready URLs are required",
        }
    executable = Path(command[0])
    if (
        not executable.is_file()
        or not executable.name.startswith("python")
        or command[1:3] != ["-m", "exomem"]
        or "--transport" not in command
        or not Path(sample_vault).is_dir()
    ):
        return {"supported": False, "reason": "active command must be a sample python -m exomem server"}
    unit = f"exomem-resource-envelope-{uuid.uuid4().hex[:8]}"
    quota = runtime_resources.systemd_cpu_quota()
    noise_children = runtime_resources.effective_online_cpus()
    state_root = Path(tempfile.mkdtemp(prefix="exomem-resource-envelope-"))
    environment = _isolated_sample_environment(Path(sample_vault), state_root)
    environment["EXOMEM_RESOURCE_NOISE_CHILDREN"] = str(noise_children)
    supervisor = (
        "import signal, subprocess, sys; "
        "server=subprocess.Popen(sys.argv[1:]); "
        "noise=[subprocess.Popen([sys.executable, '-c', 'while True: pass']) "
        "for _ in range(int(__import__('os').environ['EXOMEM_RESOURCE_NOISE_CHILDREN']))]; "
        "signal.signal(signal.SIGTERM, lambda *_: ([child.terminate() for child in [server, *noise]])); "
        "server.wait(); [child.terminate() for child in noise]"
    )
    outcome: dict[str, object] = {}
    try:
        started = subprocess.run(
            _transient_supervisor_command(
                unit=unit,
                quota=quota,
                environment=environment,
                supervisor=supervisor,
                command=command,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if started.returncode:
            outcome = {"supported": False, "reason": (started.stderr or started.stdout).strip()}
            return outcome

        def cpu_seconds() -> float:
            result = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=CPUUsageNSec", "--value"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "CPUUsageNSec unavailable")
            return int(result.stdout.strip()) / 1_000_000_000

        def task_count() -> int | None:
            result = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=TasksCurrent", "--value"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            try:
                return int(result.stdout.strip()) if result.returncode == 0 else None
            except ValueError:
                return None

        ready_deadline = time.monotonic() + 30
        while True:
            try:
                with urlopen(health_url, timeout=1):  # nosec B310 -- local sample URL
                    break
            except (HTTPError, OSError, TimeoutError, URLError) as error:
                if time.monotonic() >= ready_deadline:
                    raise RuntimeError(
                        "sample server did not become healthy within 30 seconds"
                    ) from error
                time.sleep(0.2)
        sample_started_at = time.monotonic()
        samples = [cpu_seconds()]
        health_readiness_latencies: list[float] = []
        resource_status_latencies: list[float] = []
        deadline = time.monotonic() + max(5.0, seconds)
        while time.monotonic() < deadline:
            for url in (health_url, ready_url):
                started_at = time.monotonic()
                with urlopen(url, timeout=1):  # nosec B310 -- caller supplies local sample URL
                    pass
                health_readiness_latencies.append(time.monotonic() - started_at)
            resource_status_latencies.append(
                _resource_status_probe(
                    command[0], Path(sample_vault), environment=environment, quota=quota
                )
            )
            time.sleep(0.2)
        samples.append(cpu_seconds())
        result = runtime_resources.evaluate_active_envelope(
            cpu_samples=samples,
            duration_seconds=time.monotonic() - sample_started_at,
            quota_percent=int(quota.removesuffix("%")),
            health_latencies=[*health_readiness_latencies, *resource_status_latencies],
        )
        outcome = {
            "supported": True,
            "unit": unit,
            "quota": quota,
            "tasks_diagnostic": task_count(),
            "health_readiness_latencies": health_readiness_latencies,
            "resource_status_latencies": resource_status_latencies,
            **result,
        }
        return outcome
    except Exception as error:  # noqa: BLE001 -- a gate must report unreadable probes
        outcome = {"supported": True, "unit": unit, "ok": False, "failures": [str(error)]}
        return outcome
    finally:
        cleanup_failure = _cleanup_unit(unit)
        if cleanup_failure:
            outcome.setdefault("failures", []).append(f"cleanup failed: {cleanup_failure}")
            outcome["ok"] = False
        shutil.rmtree(state_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--sample-seconds", type=float, default=10.0)
    parser.add_argument("--max-rss-mb", type=float, default=512.0)
    parser.add_argument("--max-memory-mb", type=float)
    parser.add_argument("--max-cpu-percent", type=float, default=1.0)
    parser.add_argument("--expected-servers", type=int, default=1)
    parser.add_argument("--active-cgroup", action="store_true")
    parser.add_argument("--active-executable")
    parser.add_argument("--active-arg", action="append", default=[])
    parser.add_argument("--active-sample-vault")
    parser.add_argument("--active-health-url")
    parser.add_argument("--active-ready-url")
    parser.add_argument("--active-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)

    if args.active_cgroup:
        active = _active_cgroup_gate(
            command=(
                [args.active_executable, *args.active_arg]
                if args.active_executable is not None
                else None
            ),
            sample_vault=args.active_sample_vault,
            health_url=args.active_health_url,
            ready_url=args.active_ready_url,
            seconds=args.active_seconds,
        )
        print(json.dumps(active, indent=2))
        return 0 if active.get("supported") and active.get("ok") else 1

    cpu_samples: dict[int, list[float]] = {}
    latest: list[ProcessRow] = []
    for sample in range(max(1, args.samples)):
        latest = process_rows()
        for row in latest:
            if _is_server(row):
                cpu_samples.setdefault(row.pid, []).append(row.cpu_percent)
        if sample + 1 < args.samples:
            time.sleep(max(0.0, args.sample_seconds))

    servers = [row for row in latest if _is_server(row)]
    workers = [row for row in latest if _is_media_worker(row)]
    server_memory = process_memory.aggregate_memory([asdict(row) for row in servers])
    failures: list[str] = []
    if len(servers) != args.expected_servers:
        failures.append(f"expected {args.expected_servers} server(s), found {len(servers)}")
    if workers:
        failures.append(f"expected no idle media worker, found {len(workers)}")
    for row in servers:
        average_cpu = sum(cpu_samples.get(row.pid, [row.cpu_percent])) / len(
            cpu_samples.get(row.pid, [row.cpu_percent])
        )
        if row.rss_mb > args.max_rss_mb:
            failures.append(f"pid {row.pid} RSS {row.rss_mb} MiB > {args.max_rss_mb} MiB")
        if average_cpu > args.max_cpu_percent:
            failures.append(
                f"pid {row.pid} CPU {average_cpu:.2f}% > {args.max_cpu_percent:.2f}%"
            )
    if (
        args.max_memory_mb is not None
        and server_memory["memory_metric"] == "physical_footprint"
        and server_memory["memory_mb_total"] > args.max_memory_mb
    ):
        failures.append(
            "physical footprint total "
            f"{server_memory['memory_mb_total']} MiB > {args.max_memory_mb} MiB"
        )

    payload = {
        "success": not failures,
        "servers": [asdict(row) for row in servers],
        "media_workers": [asdict(row) for row in workers],
        "server_memory": server_memory,
        "limits": {
            "max_rss_mb": args.max_rss_mb,
            "max_memory_mb": args.max_memory_mb,
            "max_cpu_percent": args.max_cpu_percent,
            "expected_servers": args.expected_servers,
        },
        "failures": failures,
    }
    print(json.dumps(payload, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
