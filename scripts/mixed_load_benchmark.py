"""Measure public read/write latency during a finite PDF/OCR ingest burst.

All writes use a persistent public MCP session in fresh disposable state. The
read-only graph proof runs outside foreground timing. This is a diagnostic of
the local server, not a connector, GPU, or competitive product benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import durable_closure_benchmark as media_helpers
import durable_closure_common as common

ROOT = Path(__file__).resolve().parents[1]
TRACKER = "Knowledge Base/Reference/active-tracker.md"
TARGET = "Knowledge Base/Reference/archived-runbook.md"
RELATION = "\n## Relations\n\n- supports [[Knowledge Base/Reference/archived-runbook]]\n"


def require_external_roots(paths: list[Path], sources: list[Path]) -> None:
    """Generated state and output must not overlap a measured source tree."""
    for path in paths:
        candidate = path.resolve()
        for source in sources:
            root = source.resolve()
            if candidate.is_relative_to(root) or root.is_relative_to(candidate):
                raise ValueError("disposable state, vault and output must be outside source checkouts")


def prepare_roots(state: Path, vault: Path) -> None:
    """Validate both roots before creating either; never adopt a live tree."""
    for path in (state, vault):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("disposable roots must not traverse symlinks")
    a, b = state.resolve(), vault.resolve()
    if a.is_relative_to(b) or b.is_relative_to(a):
        raise ValueError("disposable roots must not overlap")
    for path in (a, b):
        if path.exists():
            raise ValueError("state and vault must be new disposable directories")
    for path in (a, b):
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise ValueError("disposable root was claimed by another run") from error


def latency_summary(values: list[float]) -> dict[str, Any]:
    values = sorted(values)
    enough = len(values) >= 100
    return {
        "count": len(values),
        "median_ms": statistics.median(values) if values else None,
        "p95_ms": values[math.ceil(len(values) * .95) - 1] if enough else None,
        "max_ms": max(values) if values else None,
        "p95_reason": None if enough else "requires at least 100 observations",
    }


def summarize_calls(calls: list[dict[str, Any]], pending_window: tuple[float, float] | None) -> dict[str, Any]:
    result = {}
    for tool in ("edit_memory", "read_memory", "ask_memory"):
        rows = [row for row in calls if row["phase"] == "foreground" and row["tool"] == tool]
        overlaps = [row for row in rows if pending_window and
                    row["started_ms"] < pending_window[1] and row["ended_ms"] > pending_window[0]]
        result[tool] = {
            "all": latency_summary([row["elapsed_ms"] for row in rows]),
            "successful": latency_summary([row["elapsed_ms"] for row in rows if row["outcome"] == "ok"]),
            "pending_window": latency_summary([row["elapsed_ms"] for row in overlaps]),
            "failed": sum(row["outcome"] != "ok" for row in rows),
        }
    return result


def verify_read(payload: dict[str, Any], body: str) -> bool:
    return common.read_body_equals(payload, body)


def verify_search(payload: dict[str, Any], path: str, marker: str) -> bool:
    for key in ("hits", "results", "items", "result"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for hit in rows:
            if not isinstance(hit, dict) or hit.get("path") != path:
                continue
            if any(isinstance(hit.get(field), str) and marker in hit[field]
                   for field in ("snippet", "excerpt", "content", "body", "text")):
                return True
    return False


class ProofFailure(RuntimeError):
    """An observed product correctness failure in an otherwise valid run."""


class PublicRefusal(ProofFailure):
    """An observed server refusal, distinct from an invalid transport sample."""


class Recorder:
    def __init__(self, client: Any):
        self.client = client
        self.origin = time.perf_counter()
        self.calls: list[dict[str, Any]] = []

    def elapsed(self) -> float:
        return (time.perf_counter() - self.origin) * 1000

    async def call(self, tool: str, arguments: dict[str, Any], *, phase: str) -> dict[str, Any]:
        row: dict[str, Any] = {"id": len(self.calls), "tool": tool, "phase": phase,
                               "started_ms": self.elapsed(), "outcome": "invalid"}
        self.calls.append(row)
        try:
            payload = common._decode_payload(await self.client.call_tool_mcp(tool, arguments))
            row["outcome"] = common.result_classification(payload)
            if row["outcome"] == "refused":
                row["error"] = payload.get("error")
                raise PublicRefusal(f"{tool} refused: {row['error']}")
            if row["outcome"] != "ok":
                raise RuntimeError(f"{tool} returned an invalid public result")
            return payload
        except Exception as error:
            row["exception"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            row["ended_ms"] = self.elapsed()
            row["elapsed_ms"] = row["ended_ms"] - row["started_ms"]


def _fixture(vault: Path, pages: int) -> dict[str, Any]:
    shutil.copytree(ROOT / "src/exomem/_scaffold/_Schema", vault / "Knowledge Base/_Schema")
    fixture = common.materialize_fixture(vault / "Knowledge Base/Reference", pages=pages)
    tracker = vault / TRACKER
    tracker.write_text(tracker.read_text(encoding="utf-8") + RELATION, encoding="utf-8")
    for row in fixture["pages"]:
        raw = (vault / "Knowledge Base/Reference" / row["path"]).read_bytes()
        row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    fixture["digest"] = hashlib.sha256(json.dumps(fixture["pages"], sort_keys=True).encode()).hexdigest()
    fixture["generator"] = "mixed-load-common-with-tracker-relation-v1"
    fixture["total_bytes"] = sum(row["bytes"] for row in fixture["pages"])
    return fixture


def _runtime(python: Path, env: dict[str, str]) -> dict[str, Any]:
    runtime = media_helpers.runtime_provenance(ROOT, python)
    # Inspect flags inside the selected interpreter: startup .pth files can set
    # variables that were absent from the shell and the supplied environment.
    probe = subprocess.run([str(python), "-c", "import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith('EXOMEM_')}))"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=True)
    actual = json.loads(probe.stdout)
    expected = {key: value for key, value in env.items() if key.startswith("EXOMEM_")}
    if actual != expected:
        raise RuntimeError("selected interpreter changed the declared EXOMEM environment")
    runtime["effective_environment"] = actual
    runtime["tesseract"] = media_helpers._command_output(["tesseract", "--version"], cwd=ROOT)
    runtime["drivers"] = {
        name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
        for name in ("mixed_load_benchmark.py", "mixed_load_graph.py", "durable_closure_common.py", "durable_closure_benchmark.py")
    }
    return runtime


async def wait_graph(vault: Path, timeout: float) -> dict[str, Any]:
    from mixed_load_graph import inspect_graph

    started = time.perf_counter()
    deadline = started + timeout
    observations = []
    while True:
        proof = await asyncio.to_thread(inspect_graph, vault, expected_link=(TRACKER, TARGET), expected_relation="supports")
        elapsed = (time.perf_counter() - started) * 1000
        observations.append({"elapsed_ms": elapsed, "ready": proof["ready"], "reason": proof.get("reason"),
                             "proof_elapsed_ms": proof.get("proof_elapsed_ms")})
        if proof["ready"] or time.perf_counter() >= deadline:
            return {"ready": proof["ready"], "observed_wait_ms": elapsed if proof["ready"] else None,
                    "wait_elapsed_ms": elapsed, "timeout_seconds": timeout, "poll_interval_ms": 1000,
                    "observations": observations, "final_proof": proof}
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.perf_counter())))


async def foreground(recorder: Recorder, initial_body: str, cycles: int, result: dict[str, Any]) -> None:
    body = initial_body
    last_marker = "mixedloadmarker00000"
    for number in range(1, cycles + 1):
        new = f"mixedloadmarker{number:05d}"
        refused = []
        try:
            await recorder.call("edit_memory", {
                "path": TRACKER, "why": "advance the disposable mixed-load tracker",
                "operation": {"kind": "replace_string", "old_string": last_marker, "new_string": new, "replace_all": False},
            }, phase="foreground")
            body = body.replace(last_marker, new)
            last_marker = new
        except PublicRefusal as error:
            refused.append(str(error))
        try:
            direct = await recorder.call("read_memory", {"path": TRACKER}, phase="foreground")
        except PublicRefusal as error:
            refused.append(str(error))
            direct = {}
        read_ok = verify_read(direct, body)
        try:
            recall = await recorder.call("ask_memory", {
                "query": last_marker, "mode": "keyword", "detail": "full", "graph": False, "rerank": False, "limit": 5,
            }, phase="foreground")
        except PublicRefusal as error:
            refused.append(str(error))
            recall = {}
        search_ok = verify_search(recall, TRACKER, last_marker)
        result["cycles"].append({"cycle": number, "exact_body": read_ok, "exact_search": search_ok,
                                 "refusals": refused,
                                 "expected_body_sha256": hashlib.sha256(body.encode()).hexdigest()})
    result["finished_ms"] = recorder.elapsed()
    failures = sum(bool(row["refusals"]) or not row["exact_body"] or not row["exact_search"] for row in result["cycles"])
    if failures:
        raise ProofFailure(f"{failures} foreground cycles failed correctness")


def preservation_proof(artifacts: list[dict[str, str]], files: Any) -> tuple[list[str], list[dict[str, Any]]]:
    if not isinstance(files, list) or len(files) != len(artifacts) or not all(isinstance(item, dict) for item in files):
        raise ProofFailure("preservation receipt membership invalid")
    by_id = {item.get("file_id"): item for item in files}
    if len(by_id) != len(artifacts) or set(by_id) != {item["file_id"] for item in artifacts}:
        raise ProofFailure("preservation receipt membership invalid")
    paths = media_helpers.validated_evidence_paths(artifacts, files)
    if not paths or len(set(paths)) != len(artifacts):
        raise ProofFailure("preservation artifact hash/path proof failed")
    ordered = [by_id[item["file_id"]] for item in artifacts]
    if any(type(item.get("size")) is not int or item["size"] <= 0 or item.get("hash_algorithm") != "sha256" for item in ordered):
        raise ProofFailure("preservation byte count or hash algorithm invalid")
    return paths, [{key: item[key] for key in ("file_id", "stored_path", "size", "hash", "hash_algorithm")} for item in ordered]


async def media_burst(recorder: Recorder, registered: dict[str, Any], artifacts: list[dict[str, str]],
                      groups: int, timeout: float, result: dict[str, Any]) -> None:
    result["started_ms"] = recorder.elapsed()
    for number in range(groups):
        record: dict[str, Any] = {"group": number, "started_ms": recorder.elapsed()}
        result["groups"].append(record)
        preserved = await recorder.call("preserve_artifacts", {
            "scope": f"mixed-load-{number:03d}", "category": "benchmark-evidence",
            "files": [{key: item[key] for key in ("file_id", "download_url", "mime_type", "file_name")} for item in artifacts],
        }, phase="media-submit")
        paths, preserved_rows = preservation_proof(artifacts, preserved.get("files"))
        record["paths"] = paths
        record["preserved"] = preserved_rows
        rows = []
        for request in media_helpers.process_media_requests(registered["process_media"], paths):
            payload = await recorder.call("process_media", request, phase="media-submit")
            rows.extend(media_helpers.media_result_rows(payload))
        if not media_helpers.media_rows_match_request(paths, rows):
            raise ProofFailure("media enqueue omitted or failed an expected artifact")
        sidecars = [row.get("sidecar_path") for row in rows]
        if len(set(sidecars)) != 3 or not all(isinstance(path, str) and path for path in sidecars):
            raise ProofFailure("media sidecar identity is incomplete")
        record.update(sidecars=sidecars, submitted_ms=recorder.elapsed(), enqueue_rows=rows)
    result["submitted_ms"] = recorder.elapsed()
    deadline = time.perf_counter() + timeout
    while True:
        for record in result["groups"]:
            if "completed_ms" in record:
                continue
            reads = [await recorder.call("read_memory", {"path": path}, phase="media-poll") for path in record["sidecars"]]
            for item, receipt, path, read in zip(artifacts, record["preserved"], record["paths"], reads, strict=True):
                frontmatter = read.get("frontmatter", {})
                if frontmatter.get("processing_state") in {"blocked", "failed"}:
                    raise ProofFailure(f"media extraction {frontmatter.get('processing_state')}")
                if frontmatter.get("processing_state") == "completed" and (
                    frontmatter.get("binary_sha256") != item["sha256"]
                    or frontmatter.get("binary_size") != receipt["size"]
                    or frontmatter.get("evidence_file") != path
                ):
                    raise ProofFailure("completed sidecar binary identity mismatch")
            proof = media_helpers.extraction_proof(artifacts, reads)
            if proof:
                record.update(completed_ms=recorder.elapsed(), extraction_proof=proof)
        if all("completed_ms" in record for record in result["groups"]):
            result.update(ready=True, completed_ms=recorder.elapsed())
            return
        if time.perf_counter() >= deadline:
            raise ProofFailure("media completion was not publicly proven before timeout")
        await asyncio.sleep(1)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    require_external_roots([args.state, args.vault, args.output], [ROOT])
    prepare_roots(args.state, args.vault)
    env = media_helpers.benchmark_environment(args.state, args.vault, server_root=ROOT)
    env["EXOMEM_DISABLE_CLIP"] = "1"
    # This runner imports only read-only proof helpers under the same isolated
    # configuration. Its environment is process-local and never the live cell's.
    for key in tuple(os.environ):
        if key.startswith("EXOMEM_"):
            del os.environ[key]
    os.environ.update({key: value for key, value in env.items() if key.startswith("EXOMEM_") or key == "XDG_STATE_HOME"})
    fixture = _fixture(args.vault, args.pages)
    report: dict[str, Any] = {
        "schema": "mixed-load-v1", "status": "invalid", "case": args.case,
        "started_utc": dt.datetime.now(dt.UTC).isoformat(),
        "configuration": {"pages": args.pages, "cycles": args.cycles,
                          "media_groups": args.media_groups if args.case == "media" else 0,
                          "call_timeout_seconds": args.timeout, "startup_timeout_seconds": args.startup_timeout,
                          "graph_timeout_seconds": args.graph_timeout},
        "corpus": fixture, "runtime": _runtime(args.python, env),
        "host": {"cpu_count": os.cpu_count(), "load_start": os.getloadavg() if hasattr(os, "getloadavg") else None},
        "foreground": {"cycles": []}, "media": {"ready": args.case == "idle", "groups": []},
        "calls": [], "errors": [],
    }
    transport = StdioTransport(command=str(args.python), args=["-m", "exomem", "--transport", "stdio"],
                               env=env, cwd=str(ROOT), keep_alive=False, log_file=args.state / "stdio.log")
    client = Client(transport, timeout=args.timeout, init_timeout=args.startup_timeout)
    startup = time.perf_counter()
    recorder = None
    try:
        async with client:
            registered = {tool.name: tool for tool in await client.list_tools()}
            common._permit_refusal_envelopes(client)
            setup = common.PublicClient(client)
            report["setup_calls"] = setup.calls
            deadline = time.perf_counter() + args.startup_timeout
            report["initial_index"] = {}
            await common._await_indexed_fixture(product="exomem", state=args.state, vault=args.vault,
                                                fixture=fixture, deadline=deadline, evidence=report["initial_index"])
            await common._await_initial_index(setup, product="exomem", deadline=deadline)
            await common._await_exomem_mutation(setup, deadline=deadline)
            await setup.call("edit_memory", {"path": TRACKER, "why": "initialize disposable graph lineage",
                "operation": {"kind": "replace_string", "old_string": "State: active",
                              "new_string": "State: mixedloadmarker00000", "replace_all": False}})
            report["initial_graph"] = await wait_graph(args.vault, args.graph_timeout)
            if not report["initial_graph"]["ready"]:
                raise RuntimeError("initial graph did not reach proven freshness")
            initial = await setup.call("read_memory", {"path": TRACKER})
            initial_body = (dict(common.fixture_pages(args.pages))["active-tracker.md"] + RELATION).replace(
                "State: active", "State: mixedloadmarker00000"
            )
            if not verify_read(initial, initial_body):
                raise RuntimeError("initial tracker read failed")
            report["startup_ms"] = (time.perf_counter() - startup) * 1000
            recorder = Recorder(client)
            report["calls"] = recorder.calls
            jobs = [foreground(recorder, initial_body, args.cycles, report["foreground"])]
            if args.case == "media":
                artifacts = media_helpers.load_artifact_manifest(args.artifacts_manifest)
                report["artifacts"] = artifacts
                jobs.append(media_burst(recorder, registered, artifacts, args.media_groups, args.timeout, report["media"]))
            outcomes = await asyncio.gather(*jobs, return_exceptions=True)
            report["writers_finished_ms"] = recorder.elapsed()
            report["errors"].extend(f"{type(error).__name__}: {error}" for error in outcomes if isinstance(error, BaseException))
            if report["foreground"].get("finished_ms") is not None and report["media"]["ready"]:
                report["final_graph"] = await wait_graph(args.vault, args.graph_timeout)
                report["graph_observed_at_ms"] = recorder.elapsed() if report["final_graph"]["ready"] else None
                if not report["final_graph"]["ready"]:
                    report["errors"].append("final graph catch-up unproven before timeout")
        # Only product refusals and public correctness proof failures are valid
        # failed observations. Decode, transport, and harness faults are invalid.
        invalid_outcome = any(call.get("outcome") == "invalid" for call in report["calls"])
        invalid_exception = any(
            isinstance(error, BaseException) and not isinstance(error, ProofFailure)
            for error in outcomes
        )
        report["status"] = "invalid" if invalid_outcome or invalid_exception else (
            "pass" if not report["errors"] else "fail"
        )
    except Exception as error:  # noqa: BLE001 - preserve transport/setup faults as invalid evidence.
        report["errors"].append(f"{type(error).__name__}: {error}")
    if recorder:
        pending = (report["media"].get("started_ms"), report["media"].get("completed_ms"))
        window = pending if all(value is not None for value in pending) else None
        report["latencies"] = summarize_calls(recorder.calls, window)
        report["observed_media_window_ms"] = window
    report["host"]["load_end"] = os.getloadavg() if hasattr(os, "getloadavg") else None
    report["ended_utc"] = dt.datetime.now(dt.UTC).isoformat()
    report["limitations"] = [
        "Finite ingest burst: preservation/network/queue/extraction/publication costs are combined.",
        "Observed media window bounds completion detection; it does not prove continuous extraction CPU activity.",
        "Client monotonic timings include MCP transport; no connector/model overhead is inferred.",
        "Graph wait includes polling and full-source proof cost; semantic/full queue counts are reported separately.",
        "Embeddings and CLIP disabled; no claim about GPU load or global projection convergence.",
    ]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("idle", "media"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--pages", type=int, default=3800)
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--media-groups", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--startup-timeout", type=float, default=600)
    parser.add_argument("--graph-timeout", type=float, default=300)
    parser.add_argument("--artifacts-manifest", type=Path, default=ROOT / "docs/benchmarks/durable-closure-public-artifacts.json")
    args = parser.parse_args(argv)
    if not common.MIN_PAGES <= args.pages <= common.MAX_PAGES or args.cycles < 1 or not 1 <= args.media_groups <= 10:
        parser.error("invalid pages, cycles, or media-groups (1..10)")
    if any(not math.isfinite(value) or value <= 0 for value in (args.timeout, args.startup_timeout, args.graph_timeout)):
        parser.error("timeouts must be positive and finite")
    args.state, args.vault = args.state.absolute(), args.vault.absolute()
    args.python = media_helpers.normalize_python_launcher(args.python, Path.cwd())
    if args.output.exists() or args.output.absolute().is_relative_to(args.state) or args.output.absolute().is_relative_to(args.vault):
        parser.error("output must be new and outside disposable state/vault")
    try:
        report = asyncio.run(run(args))
    except Exception as error:  # noqa: BLE001 - emit invalid evidence and a nonzero CLI status.
        report = {"schema": "mixed-load-v1", "status": "invalid", "errors": [f"{type(error).__name__}: {error}"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report.get("errors"), "output": str(args.output)}))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
