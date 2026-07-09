#!/usr/bin/env python3
"""Desk-side persistent-core resource acceptance check.

Run after the media idle deadline. The script is stdlib-only and intentionally
measures OS processes rather than importing Exomem or torch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    rss_mb: float
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
            rows.append(
                ProcessRow(
                    pid=int(parts[0]),
                    rss_mb=round(int(parts[1]) / 1024, 1),
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
    return [
        ProcessRow(
            pid=int(row["pid"]),
            rss_mb=float(row["rss_mb"]),
            cpu_percent=float(row["cpu_percent"]),
            command=str(row.get("command") or ""),
        )
        for row in payload
    ]


def process_rows() -> list[ProcessRow]:
    return _windows_rows() if os.name == "nt" else _posix_rows()


def _is_server(row: ProcessRow) -> bool:
    command = row.command.lower()
    return "exomem" in command and "--transport" in command and "media_worker_child" not in command


def _is_media_worker(row: ProcessRow) -> bool:
    return "exomem.media_worker_child" in row.command.lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--sample-seconds", type=float, default=10.0)
    parser.add_argument("--max-rss-mb", type=float, default=512.0)
    parser.add_argument("--max-cpu-percent", type=float, default=1.0)
    parser.add_argument("--expected-servers", type=int, default=1)
    args = parser.parse_args(argv)

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

    payload = {
        "success": not failures,
        "servers": [asdict(row) for row in servers],
        "media_workers": [asdict(row) for row in workers],
        "limits": {
            "max_rss_mb": args.max_rss_mb,
            "max_cpu_percent": args.max_cpu_percent,
            "expected_servers": args.expected_servers,
        },
        "failures": failures,
    }
    print(json.dumps(payload, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
