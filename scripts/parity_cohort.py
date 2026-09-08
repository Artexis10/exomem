"""Collect six serial current-source Exomem and Basic Memory observations."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import parity_basic_memory
from mixed_load_benchmark import require_external_roots

ROOT = Path(__file__).resolve().parents[1]
OUTER_TIMEOUT_S = 1800
MAX_RUNS = 6
OVERHEAD_BUDGET_S = 1800
TOTAL_MAX_BUDGET_S = MAX_RUNS * OUTER_TIMEOUT_S + OVERHEAD_BUDGET_S


def comparison_order() -> list[tuple[int, str]]:
    return [
        (1, "exomem"),
        (1, "basic_memory"),
        (2, "basic_memory"),
        (2, "exomem"),
        (3, "exomem"),
        (3, "basic_memory"),
    ]


def accepted_outcome(status: object, exit_code: int) -> bool:
    return (status, exit_code) in {("pass", 0), ("fail", 1)}


def _git(source: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=source, text=True).strip()


def source_metadata(source: Path, *, expected_revision: str | None = None) -> dict[str, str]:
    if _git(source, "status", "--porcelain"):
        raise ValueError(f"selected source is not clean: {source}")
    revision = _git(source, "rev-parse", "HEAD")
    if expected_revision is not None and revision != expected_revision:
        raise ValueError("Basic Memory source revision does not match parity_basic_memory.REVISION")
    lock = source / "uv.lock"
    if not lock.is_file():
        raise ValueError(f"selected source has no uv.lock: {source}")
    return {
        "source": str(source),
        "revision": revision,
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }


def selected_sources(basic_memory_source: Path) -> dict[str, dict[str, str]]:
    return {
        "exomem": source_metadata(ROOT),
        "basic_memory": source_metadata(
            basic_memory_source, expected_revision=parity_basic_memory.REVISION
        ),
    }


def _driver_hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "scripts").glob("parity_*.py"))
    }


def _result(result_path: Path) -> dict[str, Any]:
    if not result_path.exists():
        return {
            "status": "invalid",
            "errors": ["no final result after outer timeout; call intervals unavailable"],
        }
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "status": "invalid",
            "errors": [f"result unavailable: {type(error).__name__}: {error}"],
        }
    if not isinstance(result, dict):
        return {"status": "invalid", "errors": ["result is not a JSON object"]}
    return result


def _cleanup(process: subprocess.Popen[Any], host: dict[str, Any]) -> int:
    host["outer_timeout"] = True
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    return 124


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--basic-memory-source", type=Path, required=True)
    parser.add_argument("--basic-memory-python", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=3800)
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--startup-timeout", type=float, default=900)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("the parity cohort requires POSIX process-group cleanup")
    if not 4 <= args.pages <= 8000 or args.cycles < 1 or not 1 <= args.concurrency <= 32:
        parser.error("invalid corpus size, cycles, or concurrency")
    args.output = args.output.absolute()
    require_external_roots([args.output], [ROOT, args.basic_memory_source.absolute()])
    sources = selected_sources(args.basic_memory_source.absolute())
    args.output.mkdir(parents=True, exist_ok=False)
    order = comparison_order()
    manifest: dict[str, Any] = {
        "schema": "current-source-parity-cohort-v1",
        "sources": sources,
        "driver_sha256": _driver_hashes(),
        "configuration": {
            "pages": args.pages,
            "cycles": args.cycles,
            "concurrency": args.concurrency,
            "timeout_s": args.timeout,
            "startup_timeout_s": args.startup_timeout,
        },
        "order": order,
        "outer_timeout_s": OUTER_TIMEOUT_S,
        "overhead_budget_s": OVERHEAD_BUDGET_S,
        "total_max_budget_s": TOTAL_MAX_BUDGET_S,
        "started_utc": dt.datetime.now(dt.UTC).isoformat(),
        "collection_status": "running",
        "runs": [],
    }
    manifest_path = args.output / "cohort.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    invalid = False
    python_by_product = {
        "exomem": Path(sys.executable),
        "basic_memory": args.basic_memory_python.absolute(),
    }
    for pair, product in order:
        run = args.output / f"pair-{pair}-{product}"
        run.mkdir()
        command = [
            str(sys.executable),
            str(ROOT / "scripts/parity_benchmark.py"),
            "--product",
            product,
            "--source",
            sources[product]["source"],
            "--python",
            str(python_by_product[product]),
            "--state",
            str(run / "state"),
            "--vault",
            str(run / "vault"),
            "--output",
            str(run / "result.json"),
            "--pages",
            str(args.pages),
            "--cycles",
            str(args.cycles),
            "--concurrency",
            str(args.concurrency),
            "--timeout",
            str(args.timeout),
            "--startup-timeout",
            str(args.startup_timeout),
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("EXOMEM_", "BASIC_MEMORY_"))
        }
        env["XDG_STATE_HOME"] = str(run / "runner-xdg")
        host: dict[str, Any] = {
            "started_utc": dt.datetime.now(dt.UTC).isoformat(),
            "load_start": os.getloadavg(),
            "cpu_count": os.cpu_count(),
            "pair": pair,
            "product": product,
        }
        print(f"START pair {pair} {product}", flush=True)
        started = time.perf_counter()
        with (run / "stdout.log").open("w") as stdout, (run / "stderr.log").open("w") as stderr:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=OUTER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                exit_code = _cleanup(process, host)
        host.update(
            exit_code=exit_code,
            elapsed_s=time.perf_counter() - started,
            load_end=os.getloadavg(),
            ended_utc=dt.datetime.now(dt.UTC).isoformat(),
        )
        (run / "host.json").write_text(json.dumps(host, indent=2) + "\n", encoding="utf-8")
        result = _result(run / "result.json")
        status = result.get("status")
        row = {
            "pair": pair,
            "product": product,
            "status": status,
            "exit_code": exit_code,
            "elapsed_s": host["elapsed_s"],
            "host": host,
            "result": result,
        }
        if not accepted_outcome(status, exit_code):
            invalid = True
        manifest["runs"].append(row)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row), flush=True)
    manifest["collection_status"] = "invalid" if invalid else "complete"
    manifest["ended_utc"] = dt.datetime.now(dt.UTC).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
