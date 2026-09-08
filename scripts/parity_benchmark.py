"""Compare current-source public Markdown outcomes in disposable Linux state.

One invocation measures one product. The cohort alternates products on a fresh
runner; this script never starts, stops or inspects a user's existing service.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import datetime as dt
import hashlib
import importlib
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import durable_closure_common as common
import parity_fixture as fixture_module
from mixed_load_benchmark import (
    PublicRefusal,
    Recorder,
    latency_summary,
    prepare_roots,
    require_external_roots,
)

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = (
    "import os,sys,pathlib; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
    "os.execv(sys.argv[2],sys.argv[2:])"
)


class ObservedPrerequisiteFailure(RuntimeError):
    """A product refusal prevents the crash phase, while remaining valid evidence."""


def verdict(checks: list[dict], calls: list[dict]) -> str:
    if not checks or any(row["outcome"] == "invalid" for row in calls):
        return "invalid"
    return "pass" if all(row["ok"] for row in checks) else "fail"


def token_proof(body: str, acknowledged: list[str]) -> bool:
    return bool(acknowledged) and all(len(re.findall(r"(?<!\w)" + re.escape(token) + r"(?!\w)", body)) == 1
                                      for token in acknowledged)


def process_environment(pid: int) -> dict[str, str]:
    return dict(item.decode().split("=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if b"=" in item)


def open_process_handle(pid: int) -> int:
    # Some standalone Python builds omit the pidfd wrappers despite host glibc
    # and the kernel supporting them. Use the glibc API, preserving errno.
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_open
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return descriptor


def send_process_kill(descriptor: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_send_signal
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(descriptor, signal.SIGKILL, None, 0) < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def kill_owned_server(pid_file: Path, token: str) -> dict:
    """Pin the PID before verifying our unguessable launch token and signalling."""
    pid = int(pid_file.read_text())
    descriptor = open_process_handle(pid)
    try:
        if process_environment(pid).get("PARITY_RUN_TOKEN") != token:
            raise RuntimeError("server process ownership did not match this run")
        send_process_kill(descriptor)
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        if not poller.poll(10_000):
            raise RuntimeError("owned server did not exit after SIGKILL")
    finally:
        os.close(descriptor)
    return {"signal": "SIGKILL", "pid": pid, "exit_observed": True,
            "ownership": "pidfd plus unique launch token"}


def provenance(source: Path, python: Path, product: str, env: dict) -> dict:
    runtime = common.runtime_provenance(executable=python, wheel=None, python=python,
        package="exomem" if product == "exomem" else "basic-memory", expected_version=None, environment=env)
    installed = runtime["installed"]
    expected = source / "src" / product
    if Path(installed.get("package_root", "")).resolve() != expected.resolve():
        raise RuntimeError("interpreter imported source outside the selected repository")
    runtime["source_identity"] = common._source_identity(installed)
    runtime["lock_sha256"] = hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest()
    probe = subprocess.run([str(python), "-c", "import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(('EXOMEM_','BASIC_MEMORY_'))}))"],
        env=env, cwd=source, check=True, text=True, capture_output=True, timeout=30)
    actual = json.loads(probe.stdout)
    if actual != {k: v for k, v in env.items() if k.startswith(("EXOMEM_", "BASIC_MEMORY_"))}:
        raise RuntimeError("interpreter startup altered the declared product environment")
    runtime["effective_environment"] = actual
    runtime["drivers"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT / "scripts").glob("parity_*.py"))}
    return runtime


async def observe(recorder: Recorder, operation: tuple[str, dict], phase: str) -> dict:
    try:
        return await recorder.call(*operation, phase=phase)
    except PublicRefusal:
        return {}


def check(report: dict, name: str, ok: bool, **evidence) -> None:
    report["checks"].append({"name": name, "ok": bool(ok), **evidence})


async def wait_for_proof(inspect, timeout: float) -> dict:
    started = time.perf_counter()
    observations = []
    while True:
        proof = await asyncio.to_thread(inspect)
        elapsed = (time.perf_counter() - started) * 1000
        observations.append({"elapsed_ms": elapsed, "ready": proof["ready"], "reason": proof.get("reason")})
        if proof["ready"] or elapsed >= timeout * 1000:
            return {"ready": proof["ready"], "observed_wait_ms": elapsed if proof["ready"] else None,
                    "elapsed_ms": elapsed, "observations": observations, "final_proof": proof}
        await asyncio.sleep(1)


async def graph_check(recorder, adapter, inspect, target, absent, timeout, phase) -> dict:
    started = time.perf_counter()
    observations = []
    while True:
        public = await observe(recorder, adapter.graph_arguments(target), phase)
        public_ok = adapter.verify_graph(public, target, absent)
        proof = await asyncio.to_thread(inspect)
        elapsed = (time.perf_counter() - started) * 1000
        ready = public_ok and proof["ready"]
        observations.append({"elapsed_ms": elapsed, "public_ok": public_ok, "store_ok": proof["ready"],
                             "reason": proof.get("reason")})
        if ready or elapsed >= timeout * 1000:
            return {"ready": ready, "observed_wait_ms": elapsed if ready else None,
                    "elapsed_ms": elapsed, "observations": observations, "public": public, "store": proof}
        await asyncio.sleep(1)


async def wait_search(recorder, adapter, marker, timeout, phase):
    started = time.perf_counter()
    observations = []
    while True:
        result = await observe(recorder, adapter.search_arguments(marker), phase)
        ready = adapter.verify_search(result, marker)
        elapsed = (time.perf_counter() - started) * 1000
        observations.append({"elapsed_ms": elapsed, "ready": ready})
        if ready or elapsed >= timeout * 1000:
            return {"ready": ready, "observed_wait_ms": elapsed if ready else None, "observations": observations}
        await asyncio.sleep(1)


async def run(args) -> dict:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    require_external_roots([args.state, args.vault, args.output], [ROOT, args.source])
    prepare_roots(args.state, args.vault)
    adapter = importlib.import_module("parity_" + args.product)
    env, fixture, corpus = adapter.prepare(args.state, args.vault, args.pages, source=args.source)
    env["PARITY_RUN_TOKEN"] = uuid.uuid4().hex
    # Read-only Exomem proof modules resolve the same disposable configuration.
    for key in tuple(os.environ):
        if key.startswith(("EXOMEM_", "BASIC_MEMORY_")):
            del os.environ[key]
    os.environ.update({k: v for k, v in env.items() if k.startswith(("EXOMEM_", "BASIC_MEMORY_", "XDG_"))})
    runtime = provenance(args.source, args.python, args.product, env)
    if args.product == "basic_memory" and runtime["source_identity"]["revision"] != adapter.REVISION:
        raise RuntimeError("Basic Memory imported source differs from the adapter's pinned revision")
    report = {"schema": "current-source-parity-v1", "product": args.product, "status": "invalid",
              "started_utc": dt.datetime.now(dt.UTC).isoformat(), "runtime": runtime, "corpus": fixture,
              "configuration": {"pages": args.pages, "cycles": args.cycles, "concurrency": args.concurrency,
                                "timeout_s": args.timeout, "startup_timeout_s": args.startup_timeout},
              "host": {"cpu_count": os.cpu_count(), "load_start": os.getloadavg()},
              "calls": [], "checks": [], "errors": []}
    command = ([str(args.python), "-m", "exomem", "--transport", "stdio"] if args.product == "exomem" else
               [str(args.python.parent / "basic-memory"), "mcp", "--transport", "stdio", "--project", "main"])

    def client_for(label):
        pid_file = args.state / f"{label}.pid"
        transport = StdioTransport(command=str(args.python), args=["-c", LAUNCHER, str(pid_file), *command],
            cwd=str(args.source), env=env, keep_alive=False, log_file=args.state / f"{label}-stdio.log")
        return Client(transport, timeout=args.timeout, init_timeout=args.startup_timeout), pid_file

    def index_inspect():
        if args.product == "exomem":
            return adapter.inspect_index(args.state, args.vault, fixture)
        return adapter.inspect_index(args.state, fixture)

    def relation_inspect(target, absent=None):
        if args.product == "exomem":
            return lambda: adapter.inspect_relation(args.vault, target, absent)
        return lambda: adapter.inspect_relation(args.state, fixture_module.TRACKER, target, absent)

    recorder = None
    crash_recorded = False
    try:
        client, pid_file = client_for("initial")
        startup = time.perf_counter()
        async with client:
            registered = {tool.name: tool for tool in await client.list_tools()}
            report["public_tools"] = sorted(registered)
            operations = [adapter.read_arguments(), adapter.edit_arguments("old", "new"),
                          adapter.append_arguments("token"), adapter.search_arguments("token"),
                          adapter.graph_arguments(fixture_module.OLD_TARGET)]
            from jsonschema import validate
            for name, arguments in operations:
                if name not in registered:
                    raise RuntimeError(f"adapter requires absent public tool {name}")
                validate(arguments, registered[name].inputSchema)
            common._permit_refusal_envelopes(client)
            setup = common.PublicClient(client)
            report["setup_calls"] = setup.calls
            report["initial_index"] = await wait_for_proof(index_inspect, args.startup_timeout)
            if not report["initial_index"]["ready"]:
                raise RuntimeError("initial corpus membership was not proven")
            await common._await_initial_index(setup, product=args.product, timeout=args.startup_timeout)
            if args.product == "exomem":
                await common._await_exomem_mutation(setup, timeout=args.startup_timeout)
            await setup.call(*adapter.edit_arguments("State: active", "State: paritymarker00000"))
            expected_body = fixture_module.tracker_body(args.pages).replace("State: active", "State: paritymarker00000")
            initial_read = await setup.call(*adapter.read_arguments())
            if not adapter.verify_read(initial_read, expected_body):
                report["initial_read"] = initial_read
                raise RuntimeError("initial exact-body proof failed")
            recorder = Recorder(client)
            report["calls"] = recorder.calls
            report["initial_graph"] = await graph_check(recorder, adapter, relation_inspect(fixture_module.OLD_TARGET),
                fixture_module.OLD_TARGET, None, args.timeout, "setup_graph")
            if not report["initial_graph"]["ready"]:
                raise RuntimeError("initial typed relation was not proven")
            report["startup_ms"] = (time.perf_counter() - startup) * 1000
            last_marker = "paritymarker00000"
            for number in range(1, args.cycles + 1):
                marker = f"paritymarker{number:05d}"
                result = await observe(recorder, adapter.edit_arguments(last_marker, marker), "edit")
                accepted = bool(result)
                check(report, f"edit-{number}", accepted)
                if accepted:
                    expected_body = expected_body.replace(last_marker, marker)
                    last_marker = marker
                read = await observe(recorder, adapter.read_arguments(), "read")
                check(report, f"read-{number}", adapter.verify_read(read, expected_body),
                      expected_sha256=hashlib.sha256(expected_body.encode()).hexdigest())
                search = await observe(recorder, adapter.search_arguments(last_marker), "search")
                check(report, f"search-{number}", adapter.verify_search(search, last_marker))

            report["eventual_search"] = await wait_search(recorder, adapter, last_marker, args.timeout, "eventual_search")
            check(report, "eventual-search", report["eventual_search"]["ready"])

            result = await observe(recorder, adapter.edit_arguments(fixture_module.OLD_LINK, fixture_module.NEW_LINK), "relation_edit")
            check(report, "relation-edit-accepted", bool(result))
            target, absent = (fixture_module.NEW_TARGET, fixture_module.OLD_TARGET) if result else (fixture_module.OLD_TARGET, fixture_module.NEW_TARGET)
            if result:
                expected_body = expected_body.replace(fixture_module.OLD_LINK, fixture_module.NEW_LINK)
            report["relation_replacement"] = await graph_check(recorder, adapter, relation_inspect(target, absent),
                target, absent, args.timeout, "graph")
            check(report, "relation-replacement", bool(result) and report["relation_replacement"]["ready"])

            tokens = [f"parityconcurrent{number:03d}" for number in range(args.concurrency)]
            results = await asyncio.gather(*(observe(recorder, adapter.append_arguments(token), "concurrent_append") for token in tokens))
            acknowledged = [token for token, result in zip(tokens, results, strict=True) if result]
            direct = await observe(recorder, adapter.read_arguments(), "concurrent_read")
            actual_body = common._read_body(direct)
            normalized = common._normalized_body(actual_body) if actual_body is not None else ""
            check(report, "concurrent-accepted-tokens", token_proof(normalized, acknowledged),
                  acknowledged=acknowledged, refused=[token for token in tokens if token not in acknowledged])
            check(report, "concurrent-all-accepted", len(acknowledged) == len(tokens))
            if not normalized:
                report["crash"] = {"skipped_reason": "public concurrent read did not establish accepted content"}
                raise ObservedPrerequisiteFailure("cannot establish accepted body before crash")
            crash_marker = "paritycrashaccepted"
            crash_expected = normalized.replace(last_marker, crash_marker)
            result = await observe(recorder, adapter.edit_arguments(last_marker, crash_marker), "crash_write")
            check(report, "crash-write-accepted", bool(result))
            if not result:
                report["crash"] = {"skipped_reason": "crash precondition write was refused"}
                raise ObservedPrerequisiteFailure("crash precondition write was not acknowledged")
            crash_started = time.perf_counter()
            report["crash"] = kill_owned_server(pid_file, env["PARITY_RUN_TOKEN"])
            crash_recorded = True
        restarted, restart_pid = client_for("restart")
        async with restarted:
            await restarted.list_tools()
            common._permit_refusal_envelopes(restarted)
            recorder.client = restarted
            direct = await observe(recorder, adapter.read_arguments(), "restart_read")
            check(report, "crash-accepted-body", adapter.verify_read(direct, crash_expected),
                  expected_sha256=hashlib.sha256(crash_expected.encode()).hexdigest())
            search = await observe(recorder, adapter.search_arguments(crash_marker), "restart_search")
            check(report, "restart-immediate-search", adapter.verify_search(search, crash_marker))
            report["restart_eventual_search"] = await wait_search(recorder, adapter, crash_marker, args.timeout, "restart_eventual_search")
            check(report, "restart-eventual-search", report["restart_eventual_search"]["ready"])
            report["restart_graph"] = await graph_check(recorder, adapter, relation_inspect(target, absent),
                target, absent, args.timeout, "restart_graph")
            check(report, "restart-graph", report["restart_graph"]["ready"])
            report["recovery_observed_ms"] = (time.perf_counter() - crash_started) * 1000
        report["status"] = verdict(report["checks"], report["calls"])
    except ObservedPrerequisiteFailure as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
        report["status"] = verdict(report["checks"], report["calls"])
    except Exception as error:  # noqa: BLE001 - unexpected faults remain invalid evidence.
        report["errors"].append(f"{type(error).__name__}: {error}")
        report["status"] = "invalid"
    report["crash_executed"] = crash_recorded
    report["latencies"] = {phase: latency_summary([row["elapsed_ms"] for row in report["calls"] if row["phase"] == phase])
                           for phase in ("edit", "read", "search", "relation_edit", "graph", "concurrent_append", "restart_read", "restart_search")}
    report["host"]["load_end"] = os.getloadavg()
    report["ended_utc"] = dt.datetime.now(dt.UTC).isoformat()
    report["limitations"] = ["Public client timings include local MCP transport.",
        "Body comparison removes metadata YAML and one terminal LF only; product serializers differ on final LF.",
        "Graph recovery bounds include polling and proof costs.", "SIGKILL tests process-crash recovery, not power loss.",
        "Embeddings disabled; the common workload is Markdown. PDF/OCR observations are separate.",
        "Concurrent token proof covers acknowledged appends; it does not prove every possible concurrent mutation."]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=("exomem", "basic_memory"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=3800)
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--startup-timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("process-crash proof requires Linux pidfd support")
    if not 4 <= args.pages <= 8000 or args.cycles < 1 or not 1 <= args.concurrency <= 32:
        parser.error("invalid corpus size, cycles, or concurrency")
    if any(not math.isfinite(value) or value <= 0 for value in (args.timeout, args.startup_timeout)):
        parser.error("timeouts must be positive and finite")
    for name in ("state", "vault", "source", "python", "output"):
        setattr(args, name, getattr(args, name).absolute())
    if args.product == "exomem" and args.source.resolve() != ROOT.resolve():
        parser.error("Exomem source must match this driver's repository and proof helpers")
    if args.output.exists() or args.output.is_relative_to(args.state) or args.output.is_relative_to(args.vault):
        parser.error("output must be new and outside disposable state/vault")
    try:
        report = asyncio.run(run(args))
    except Exception as error:  # noqa: BLE001 - retain setup faults in the output artifact.
        report = {"status": "invalid", "errors": [f"{type(error).__name__}: {error}"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report.get("errors"), "output": str(args.output)}))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
