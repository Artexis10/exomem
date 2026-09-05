#!/usr/bin/env python
"""Public-MCP durable-closure benchmark.

The timed workflow is deliberately driven through one long-lived stdio MCP
session.  It never calls command leaves or hand-writes index state: the only
pre-timing setup is a disposable Markdown corpus and public fixture handles.

Small runs (``--pages <= 50``) are smoke evidence only.  The 3,800/8,000-page
paired samples are intentionally a separate, serial release operation.
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
import sys
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PUBLIC_TOOLS = (
    "remember",
    "ask_memory",
    "read_memory",
    "preserve_artifacts",
    "process_media",
)
MODEL_FREE_PROFILE = "model-free"
REAL_EXTRACTION_PROFILE = "real-extraction"


def load_artifact_manifest(path: Path) -> list[dict[str, str]]:
    """Validate the operator-provided public file handles before timing."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("artifact manifest is unreadable") from error
    items = payload.get("artifacts") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or len(items) != 3:
        raise ValueError("artifact manifest must contain exactly one PDF and two images")
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("artifact manifest item is invalid")
        entry = {key: str(item.get(key) or "") for key in ("file_id", "download_url", "mime_type", "file_name", "sha256")}
        if item.get("expected_text") is not None:
            entry["expected_text"] = str(item["expected_text"])
        if not all(entry.values()) or not entry["download_url"].startswith("https://"):
            raise ValueError("artifact handles require non-empty HTTPS URLs and metadata")
        if len(entry["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in entry["sha256"].lower()):
            raise ValueError("artifact handle sha256 is invalid")
        normalized.append(entry)
    if sum(item["mime_type"] == "application/pdf" for item in normalized) != 1 or sum(item["mime_type"].startswith("image/") for item in normalized) != 2:
        raise ValueError("artifact manifest must contain one PDF and two images")
    return normalized


def workflow_status(*, core_passed: bool, media_status: str) -> str:
    """Do not promote a partial media workflow to an acceptance pass."""
    if not core_passed:
        return "fail"
    if media_status != "ready":
        return "blocked"
    return "pass"


def validated_evidence_paths(
    artifacts: Sequence[Mapping[str, Any]], files: Sequence[Mapping[str, Any]]
) -> list[str] | None:
    """Return citeable paths only for the exact requested stored bytes."""
    stored = {str(item.get("file_id") or ""): item for item in files}
    paths: list[str] = []
    for artifact in artifacts:
        result = stored.get(str(artifact.get("file_id") or ""))
        if not isinstance(result, Mapping) or result.get("outcome") != "stored":
            return None
        if result.get("hash") != artifact.get("sha256") or result.get("hash_algorithm", "sha256") != "sha256":
            return None
        path = result.get("stored_path")
        if not isinstance(path, str) or not path:
            return None
        paths.append(path)
    return paths if len(paths) == len(artifacts) else None


def _tool_input_schema(tool: Any) -> Mapping[str, Any]:
    if isinstance(tool, Mapping):
        schema = tool.get("inputSchema") or tool.get("input_schema")
    else:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        if schema is None and hasattr(tool, "to_mcp_tool"):
            mcp_tool = tool.to_mcp_tool()
            schema = getattr(mcp_tool, "inputSchema", None) or getattr(mcp_tool, "input_schema", None)
    return schema if isinstance(schema, Mapping) else {}


def process_media_requests(tool: Any, paths: Sequence[str]) -> list[dict[str, Any]]:
    """Select the registered batch surface when it is actually advertised."""
    properties = _tool_input_schema(tool).get("properties")
    if isinstance(properties, Mapping) and "paths" in properties:
        return [{"operation": "process", "paths": list(paths)}]
    return [{"operation": "process", "path": path} for path in paths]


def media_result_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize compact batch projection and legacy single-path media output."""
    rows = payload.get("media_results")
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    sidecar = payload.get("sidecar_path")
    path = payload.get("path")
    if isinstance(sidecar, str) and sidecar and isinstance(path, str) and path:
        return [{**payload, "outcome": str(payload.get("outcome") or "processed")}]
    return []


def media_rows_match_request(paths: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bool:
    """A batch terminal is useful only if it accounts for each selected artifact."""
    if [row.get("path") for row in rows] != list(paths):
        return False
    return all(
        str(row.get("outcome") or "").lower() in {"processed", "retried"}
        and isinstance(row.get("sidecar_path"), str)
        and bool(row.get("sidecar_path"))
        and (isinstance(row.get("job_id"), int) or row.get("state") == "completed")
        for row in rows
    )


def extraction_proof(
    artifacts: Sequence[Mapping[str, Any]], reads: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Accept completed extraction only when every expected unique text is public."""
    if len(artifacts) != len(reads):
        return None
    expected = [str(item.get("expected_text") or "").strip() for item in artifacts]
    if len(expected) != 3 or len(set(expected)) != 3 or any(not item for item in expected):
        return None
    engines: list[str] = []
    for marker, read in zip(expected, reads, strict=True):
        frontmatter = read.get("frontmatter")
        engine = frontmatter.get("extracted_by") if isinstance(frontmatter, Mapping) else None
        if not isinstance(engine, str) or not engine.strip() or engine.lower() in {"pending", "unavailable"}:
            return None
        if not _contains_marker(read.get("body"), marker):
            return None
        engines.append(engine)
    return {"engine_versions": sorted(set(engines)), "expected_text_verified": expected}


def workflow_plan(variant: str) -> list[dict[str, Any]]:
    """Declare the timed public workflow; stress probes are never subtracted."""
    if variant not in {"optimized", "stress"}:
        raise ValueError(f"unsupported variant: {variant}")
    base = [
        {"name": "bootstrap", "tool": "bootstrap", "mutates": False, "probe": False},
        {"name": "validate-remember", "tool": "remember", "mutates": False, "probe": False},
        {"name": "remember", "tool": "remember", "mutates": True, "probe": False},
        {"name": "observe", "tool": "observe_memory", "mutates": True, "probe": False},
        {"name": "ordinary-recall", "tool": "ask_memory", "mutates": False, "probe": False},
        {"name": "exact-read", "tool": "read_memory", "mutates": False, "probe": False},
    ]
    if variant == "optimized":
        return base
    stress: list[dict[str, Any]] = []
    for step in base:
        stress.append(step)
        if step["mutates"]:
            stress.append(
                {
                    "name": f"probe-after-{step['name']}",
                    "tool": "ask_memory",
                    "mutates": False,
                    "probe": True,
                }
            )
    return stress


def required_tools_missing(discovered: Iterable[str]) -> list[str]:
    """Return required product calls missing from the registered MCP surface."""
    available = set(discovered)
    return sorted(name for name in REQUIRED_PUBLIC_TOOLS if name not in available)


def evaluate_useful_closure(
    *,
    calls: Sequence[Mapping[str, Any]],
    final_read_your_write: bool,
    graph_warming_components: Sequence[str],
) -> dict[str, Any]:
    """Apply the workflow correctness gate without laundering recall refusals.

    Graph warming attached to a successful result is optional convergence lag;
    a call-level refusal is not.  This is intentionally independent of timing
    so a fast acknowledgement cannot make a broken workflow pass.
    """
    refusals: list[dict[str, Any]] = []
    for call in calls:
        tool = str(call.get("tool") or "")
        if tool not in {"ask_memory", "read_memory"}:
            continue
        if call.get("outcome") == "ok":
            continue
        started = float(call.get("started_ms", 0.0))
        ended = float(call.get("ended_ms", started))
        refusals.append(
            {
                "tool": tool,
                "code": str(call.get("error_code") or str(call.get("outcome") or "UNCLASSIFIED").upper()),
                "window_ms": [started, ended],
            }
        )
    return {
        "passed": bool(final_read_your_write) and not refusals,
        "final_read_your_write": bool(final_read_your_write),
        "refusal_observations": refusals,
        "warming_components": sorted(set(map(str, graph_warming_components))),
    }


def _interval_union(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((min(start, end), max(start, end)) for start, end in intervals)
    if not ordered:
        return 0.0
    start, end = ordered[0]
    total = 0.0
    for next_start, next_end in ordered[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return total + end - start


def summarize_ledger_calls(calls: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    """Keep ledger sums and observed interval occupancy explicitly separate."""
    if not calls:
        return {
            "server_duration_sum_ms": 0.0,
            "server_total_sum_ms": 0.0,
            "ledger_observation_span_ms": None,
            "server_occupied_union_ms": None,
            "server_idle_within_observed_span_ms": None,
        }
    for call in calls:
        for field in ("duration_ms", "total_ms"):
            value = call.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"ledger {field} is missing or invalid")
    intervals = [
        (float(call["started_ms"]), float(call["ended_ms"]))
        for call in calls
        if "started_ms" in call and "ended_ms" in call
    ]
    duration_sum = sum(float(call["duration_ms"]) for call in calls)
    total_sum = sum(float(call["total_ms"]) for call in calls)
    if not intervals:
        return {
            "server_duration_sum_ms": duration_sum,
            "server_total_sum_ms": total_sum,
            "ledger_observation_span_ms": None,
            "server_occupied_union_ms": None,
            "server_idle_within_observed_span_ms": None,
        }
    first = min(start for start, _ in intervals)
    last = max(end for _, end in intervals)
    union = _interval_union(intervals)
    span = last - first
    return {
        "server_duration_sum_ms": duration_sum,
        "server_total_sum_ms": total_sum,
        "ledger_observation_span_ms": span,
        "server_occupied_union_ms": union,
        "server_idle_within_observed_span_ms": max(0.0, span - union),
    }


def ledger_intervals(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    """Derive occupied server intervals from ledger completion UTC and total time."""
    intervals: list[dict[str, float]] = []
    for row in rows:
        timestamp = row.get("ts_utc")
        total = row.get("total_ms")
        if not isinstance(timestamp, str) or not isinstance(total, (int, float)) or total < 0:
            raise ValueError("ledger row has no usable completion timestamp and total duration")
        try:
            ended = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000.0
        except ValueError as error:
            raise ValueError("ledger row timestamp is malformed") from error
        intervals.append({"started_ms": ended - float(total), "ended_ms": ended})
    return intervals


def ledger_clock_continuous(calls: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> bool:
    """Reject UTC occupancy when its deltas disagree with the client trace."""
    if len(calls) != len(rows) or len(calls) < 2:
        return True
    intervals = ledger_intervals(rows)
    offsets: list[float] = []
    for call, interval in zip(calls, intervals, strict=True):
        ended = call.get("ended_ms")
        if not isinstance(ended, (int, float)):
            return False
        offset = interval["ended_ms"] - float(ended)
        if offsets and abs(offset - offsets[-1]) > 250.0:
            return False
        offsets.append(offset)
    return max(offsets) - min(offsets) <= 250.0


def lifecycle_timings(*, workflow_started: float, closure_finished: float, shutdown_finished: float) -> dict[str, float]:
    """Keep public-workflow wall time distinct from transport teardown."""
    return {
        "workflow_wall_ms": (closure_finished - workflow_started) * 1000.0,
        "server_shutdown_ms": (shutdown_finished - closure_finished) * 1000.0,
    }


def attach_ledger_measurements(
    calls: Sequence[Mapping[str, Any]], ledger_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Join ordered public calls to their same-process ledger rows conservatively.

    The ledger completion timestamp plus total duration supplies an occupied
    server interval. An unexpected count or tool ordering invalidates the
    measurement rather than guessing from a similarly named row.
    """
    joined: list[dict[str, Any]] = []
    if len(calls) != len(ledger_rows):
        raise ValueError("ledger row count does not match public call count")
    intervals = ledger_intervals(ledger_rows)
    row_index = 0
    for call in calls:
        result = dict(call)
        result["server_duration_ms"] = None
        result["server_total_ms"] = None
        result["server_interval"] = None
        if row_index < len(ledger_rows) and ledger_rows[row_index].get("tool") == call.get("tool"):
            row = ledger_rows[row_index]
            if "outcome" not in row or "error_code" not in row:
                raise ValueError("ledger outcome/error fields are missing")
            wire_tool_error = call.get("outcome") == "tool_error"
            if wire_tool_error:
                if row.get("outcome") not in {"error", "tool_error"}:
                    raise ValueError("ledger outcome does not match client tool error")
            elif row.get("outcome") != call.get("outcome"):
                raise ValueError("ledger outcome does not match client outcome")
            if not wire_tool_error and row.get("error_code") != call.get("error_code"):
                raise ValueError("ledger error code does not match client outcome")
            result["server_duration_ms"] = float(row["duration_ms"]) if row.get("duration_ms") is not None else None
            result["server_total_ms"] = float(row["total_ms"]) if row.get("total_ms") is not None else None
            result["server_interval"] = intervals[row_index]
            row_index += 1
        else:
            raise ValueError("ledger tool order does not match public call order")
        joined.append(result)
    return joined


def invalid_measurement_report(
    *, calls: Sequence[Mapping[str, Any]], ledger_row_count: int, reason: str
) -> dict[str, Any]:
    """Emit a usable invalid result when authoritative measurements cannot join."""
    retained_calls = [dict(call) for call in calls]
    return {
        "status": "invalid",
        "measurement": {"status": "invalid", "reason": reason},
        "public_call_count": len(retained_calls),
        "calls": retained_calls,
        "ledger": {"status": "invalid", "reason": reason, "row_count": ledger_row_count},
    }


def benchmark_environment(state: Path, vault: Path) -> dict[str, str]:
    """Create hermetic process state without disabling watchers or scheduling."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EXOMEM_") and key not in {"PYTHONPATH", "XDG_STATE_HOME"}
    }
    env.update(
        {
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_STATE_ROOT": str(state / "state"),
            "EXOMEM_CONFIG_PATH": str(state / "config.json"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(state / "leases"),
            "EXOMEM_CALL_LEDGER_DIR": str(state / "ledger"),
            "EXOMEM_LOG_DIR": str(state / "logs"),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            # Production's synchronous managed warm-up.  This happens before
            # the public workflow clock starts, never as an inline repair.
            "EXOMEM_EAGER_BOOT": "1",
            "XDG_STATE_HOME": str(state / "xdg"),
            "PYTHONPATH": str(ROOT / "src"),
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        }
    )
    # Watchers and graph scheduling intentionally retain their product defaults.
    return env


def install_subprocess_instrumentation(state: Path) -> Path:
    """Install a child-only import hook that counts actual graph/scanning calls."""
    directory = state / "instrumentation"
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / "sitecustomize.py"
    hook.write_text(
        '''import atexit, json, os, threading
from pathlib import Path

out = Path(os.environ["DURABLE_CLOSURE_INSTRUMENTATION"])
control_value = os.environ.get("DURABLE_CLOSURE_INSTRUMENTATION_CONTROL", "")
control = Path(control_value) if control_value else None
data = {"wrapper_status": "installed", "graph_drain_attempts": 0, "graph_drain_completed": 0, "graph_rebuild_attempts": 0, "graph_rebuild_completed": 0, "source_scan_pages": 0, "source_scan_bytes": 0, "snapshots": {}, "scan_coverage": "find._walk_md only; other scan consumers are unmeasured"}
lock = threading.RLock()
last_command = None
stopping = threading.Event()
owner_path = out.with_name("instrumentation-owner.pid")
try:
    owner_path.open("x", encoding="utf-8").write(str(os.getpid()))
    owner = True
except FileExistsError:
    owner = owner_path.read_text(encoding="utf-8").strip() == str(os.getpid())

def publish():
    if not owner:
        return
    with lock:
        temporary = out.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(temporary, out)

def command():
    global last_command
    if not owner or control is None or not control.is_file():
        return
    try:
        payload = json.loads(control.read_text(encoding="utf-8"))
    except Exception:
        return
    command_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(command_id, str) or command_id == last_command:
        return
    with lock:
        action = payload.get("action")
        phase = str(payload.get("phase") or "")
        if action == "reset":
            for key in ("graph_drain_attempts", "graph_drain_completed", "graph_rebuild_attempts", "graph_rebuild_completed", "source_scan_pages", "source_scan_bytes"):
                data[key] = 0
            data["snapshots"] = {}
        elif action == "snapshot" and phase:
            data["snapshots"][phase] = {key: data[key] for key in ("graph_drain_attempts", "graph_drain_completed", "graph_rebuild_attempts", "graph_rebuild_completed", "source_scan_pages", "source_scan_bytes")}
        else:
            data["instrumentation_error"] = "InvalidControl"
        data["acknowledged_command"] = command_id
        last_command = command_id
    publish()

def controller():
    while not stopping.wait(0.01):
        command()

def wrap(module, name, attempts, completed):
    original = getattr(module, name, None)
    if original is None:
        raise AttributeError(f"missing required instrumentation hook: {name}")
    def counted(*args, **kwargs):
        command()
        with lock:
            data[attempts] += 1
        publish()
        result = original(*args, **kwargs)
        with lock:
            data[completed] += 1
        publish()
        return result
    setattr(module, name, counted)

try:
    from exomem import index_sync, epistemic_graph, find
    wrap(epistemic_graph.EpistemicGraphIndex, "drain_paths", "graph_drain_attempts", "graph_drain_completed")
    wrap(epistemic_graph.EpistemicGraphIndex, "_rebuild_all_off_boundary", "graph_rebuild_attempts", "graph_rebuild_completed")
    original_walk = find._walk_md
    def walk(root):
        for path in original_walk(root):
            with lock:
                data["source_scan_pages"] += 1
                try:
                    data["source_scan_bytes"] += path.stat().st_size
                except OSError:
                    pass
            yield path
    find._walk_md = walk
except Exception as error:
    data["instrumentation_error"] = type(error).__name__

publish()
if owner:
    threading.Thread(target=controller, name="durable-closure-instrumentation", daemon=True).start()

@atexit.register
def save():
    stopping.set()
    command()
    publish()
''',
        encoding="utf-8",
    )
    return directory


def read_instrumentation(state: Path) -> dict[str, Any]:
    path = state / "instrumentation.json"
    if not path.is_file():
        raise RuntimeError("benchmark subprocess did not emit instrumentation")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("benchmark instrumentation is malformed") from error
    if not isinstance(data, dict) or any(key not in data for key in ("graph_drain_attempts", "graph_drain_completed", "graph_rebuild_attempts", "graph_rebuild_completed", "source_scan_pages", "source_scan_bytes", "wrapper_status")):
        raise RuntimeError("benchmark instrumentation is incomplete")
    if data.get("wrapper_status") != "installed":
        raise RuntimeError("benchmark instrumentation wrapper is unavailable")
    if data.get("instrumentation_error"):
        raise RuntimeError("benchmark instrumentation reported an error")
    return data


async def instrumentation_command(state: Path, *, action: str, phase: str, timeout: float) -> dict[str, Any]:
    """Synchronize a child-only counter reset/snapshot through its control file."""
    command_id = uuid.uuid4().hex
    control = state / "instrumentation-control.json"
    control.write_text(
        json.dumps({"id": command_id, "action": action, "phase": phase}, sort_keys=True),
        encoding="utf-8",
    )
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            data = read_instrumentation(state)
        except RuntimeError:
            data = {}
        if data.get("acknowledged_command") == command_id:
            return data
        await asyncio.sleep(0.01)
    raise RuntimeError(f"benchmark instrumentation did not acknowledge {action}:{phase}")


def materialize_corpus(vault: Path, *, pages: int) -> dict[str, Any]:
    """Build realistic, deterministic Markdown before timing begins.

    This fixture producer never creates evidence sidecars or extraction output.
    It records its generator identity and byte-level inventory as provenance.
    """
    if not 1 <= pages <= 8_000:
        raise ValueError("pages must be between 1 and 8000")
    kb = vault / "Knowledge Base"
    schema = kb / "_Schema"
    if not schema.exists():
        shutil.copytree(ROOT / "src" / "exomem" / "_scaffold" / "_Schema", schema)
    tracker = kb / "Notes" / "Insights" / "active-tracker.md"
    archived = kb / "Notes" / "Insights" / "archived-runbook.md"
    critique = kb / "Notes" / "Insights" / "critique.md"
    stale_relation = kb / "Reference" / "stale-relation.md"
    documents = {
        tracker: (
            "---\ntype: insight\ntitle: Active tracker\nstatus: active\nupdated: 2026-09-05\n"
            "exomem_id: 00000000-0000-4000-8000-000000000101\n---\n\n"
            "# Active tracker\n\n## Observations\n- [constraint] Keep workflow evidence durable #benchmark ^tracker\n\n"
            + ("## Workstream\nThe active tracker carries bounded operational context, owner handoffs, and a durable acceptance record. \n" * 8)
        ),
        archived: (
            "---\ntype: insight\ntitle: Archived operational note\nstatus: archived\nupdated: 2026-08-01\n"
            "exomem_id: 00000000-0000-4000-8000-000000000102\n---\n\n"
            "# Archived operational note\n\n## Observations\n- [history] Previous recovery sequence is retained #operations ^archived\n\n"
            + ("## Historical context\nThis archived runbook remains searchable as historical evidence but is not an active instruction. \n" * 11)
        ),
        critique: (
            "---\ntype: insight\ntitle: Workflow critique\nstatus: active\nupdated: 2026-09-04\n"
            "exomem_id: 00000000-0000-4000-8000-000000000103\n---\n\n"
            "# Workflow critique\n\n## Observations\n- [finding] Retrieval must survive writes #benchmark ^critique\n\n"
            + ("## Critique\nA useful closure benchmark distinguishes canonical durability from optional projection convergence. \n" * 14)
        ),
        stale_relation: (
            "---\ntype: entity\ntitle: Stale relation fixture\nstatus: active\n---\n\n"
            "# Stale relation fixture\n\n- supports [[Knowledge Base/Notes/Insights/archived-runbook]]\n\n"
            + ("## Repair context\nThis relation is intentionally stale and is repaired through the public edit operation. \n" * 9)
        ),
    }
    for index in range(max(0, pages - len(documents))):
        padding = "detail " * (1 + index % 5)
        related = f"[[Knowledge Base/Notes/Reference/reference-{max(0, index - 1):05d}]]"
        documents[kb / "Notes" / "Reference" / f"reference-{index:05d}.md"] = (
            "---\n"
            f"type: insight\ntitle: Reference {index}\nstatus: active\nupdated: 2026-08-01\n"
            "---\n\n"
            f"# Reference {index}\n\n## Observations\n- [fact] {padding.strip()} {index} #benchmark ^ref-{index}\n\n"
            f"## Links\n- depends on {related}\n"
        )
    total_bytes = 0
    inventory: list[dict[str, Any]] = []
    for path, content in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        encoded = content.encode("utf-8")
        total_bytes += len(encoded)
        inventory.append(
            {
                "path": path.relative_to(vault).as_posix(),
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    inventory.sort(key=lambda item: item["path"])
    corpus_digest = hashlib.sha256(
        "".join(f"{item['path']}:{item['sha256']}\n" for item in inventory).encode("utf-8")
    ).hexdigest()
    return {
        "generator": "durable-closure-markdown-v1",
        "pages": len(documents),
        "bytes": total_bytes,
        "inventory": inventory,
        "corpus_sha256": corpus_digest,
        "active_tracker": tracker.relative_to(vault).as_posix(),
        "archived_note": archived.relative_to(vault).as_posix(),
        "critique": critique.relative_to(vault).as_posix(),
        "stale_relation": stale_relation.relative_to(vault).as_posix(),
        "media_fixture": {
            "pdf": "fixture-required-via-public-handle",
            "images": 2,
            "sidecars_created": False,
        },
    }


def _decode_call(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result")
        return nested if isinstance(nested, dict) else structured
    content = getattr(result, "content", ()) or ()
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                nested = decoded.get("result")
                return nested if isinstance(nested, dict) else decoded
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)
    # Keep the public protocol terminal signal but never copy opaque tool text
    # (which can contain server-private exception detail) into the report.
    if is_error is True:
        return {"_wire_status": "tool_error"}
    return {"_wire_status": "unparseable"}


def _result_outcome(payload: Mapping[str, Any]) -> tuple[str, str | None]:
    wire_status = payload.get("_wire_status")
    if wire_status == "tool_error":
        return "tool_error", None
    if wire_status == "unparseable":
        return "transport_error", None
    if payload.get("success") is False:
        error = payload.get("error")
        return "refused", str(error.get("code")) if isinstance(error, Mapping) else "UNKNOWN"
    error = payload.get("error")
    if isinstance(error, Mapping):
        return "refused", str(error.get("code") or "UNKNOWN")
    terminal = payload.get("outcome")
    if isinstance(terminal, str) and terminal.lower() in {"failed", "refused", "rejected", "error"}:
        return "refused", str(payload.get("code") or terminal.upper())
    return "ok", None


def _outcome_ok(payload: Mapping[str, Any]) -> bool:
    return _result_outcome(payload)[0] == "ok"


def _contains_marker(payload: Any, marker: str) -> bool:
    try:
        return marker in json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def _contains_path(payload: Any, path: str) -> bool:
    if isinstance(payload, Mapping):
        if payload.get("path") == path or payload.get("parent_path") == path:
            return True
        return any(_contains_path(value, path) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_path(value, path) for value in payload)
    return False


def recall_has_exact_hit(payload: Mapping[str, Any], path: str, marker: str) -> bool:
    """Require a returned recall hit, never a diagnostic/overlay echo."""
    candidates: list[Any] = []
    for key in ("hits", "results", "items", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    for hit in candidates:
        if not isinstance(hit, Mapping):
            continue
        if hit.get("path") != path and hit.get("parent_path") != path:
            continue
        visible = [
            hit.get(key)
            for key in ("snippet", "excerpt", "content", "body", "text")
            if isinstance(hit.get(key), str)
        ]
        if not visible or any(marker in value for value in visible):
            return True
    return False


def _warming_components(payload: Any) -> list[str]:
    """Retain disclosed optional warming without manufacturing a default."""
    found: set[str] = set()
    if isinstance(payload, Mapping):
        warming = payload.get("warming")
        if isinstance(warming, Mapping):
            components = warming.get("components")
            if isinstance(components, list):
                found.update(str(item) for item in components)
        for value in payload.values():
            found.update(_warming_components(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_warming_components(value))
    return sorted(found)


def _semantic_diagnostics(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = payload.get("semantic")
    if isinstance(direct, Mapping):
        return direct
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping) and isinstance(diagnostics.get("semantic"), Mapping):
        return diagnostics["semantic"]
    return {}


def is_mutation_ack(tool: str, arguments: Mapping[str, Any]) -> bool:
    """Count only accepted durable mutations, never planning/read previews."""
    if tool not in {"remember", "observe_memory", "edit_memory", "preserve_artifacts", "process_media"}:
        return False
    operation = arguments.get("operation")
    if arguments.get("validate_only") or (isinstance(operation, str) and operation in {"validate", "status"}):
        return False
    if isinstance(operation, Mapping) and operation.get("validate_only"):
        return False
    return True


async def _call(client: Any, calls: list[dict[str, Any]], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    # The raw MCP response is intentional: a public refusal is valid protocol
    # data even when FastMCP's generated success-only output schema rejects it.
    result = await client.call_tool_mcp(tool, arguments)
    ended = time.perf_counter()
    payload = _decode_call(result)
    outcome, code = _result_outcome(payload)
    request_shape = {
        key: arguments[key]
        for key in ("mode", "graph", "rerank", "operation", "validate_only")
        if key in arguments
    }
    calls.append(
        {
            "tool": tool,
            "outcome": outcome,
            "error_code": code,
            "started_ms": (started - calls[0]["_origin"]) * 1000.0 if calls else 0.0,
            "ended_ms": (ended - calls[0]["_origin"]) * 1000.0 if calls else (ended - started) * 1000.0,
            "client_elapsed_ms": (ended - started) * 1000.0,
            "mutation_ack": is_mutation_ack(tool, arguments),
            "request_shape": request_shape,
            "response_keys": sorted(map(str, payload.keys())),
        }
    )
    return payload


async def _observe_reviewed(
    client: Any,
    calls: list[dict[str, Any]],
    *,
    path: str,
    category: str,
    content: str,
    anchor: str,
    call_tool: Any,
) -> dict[str, Any]:
    """Use the product's validate/transition/commit public mutation protocol."""
    preview = await call_tool(
        "observe_memory", {"path": path, "operation": "validate", "category": category, "content": content, "id": anchor}
    )
    semantic = _semantic_diagnostics(preview)
    return await call_tool(
        "observe_memory",
        {
            "path": path,
            "operation": "add",
            "category": category,
            "content": content,
            "id": anchor,
            "transition_token": semantic.get("transition_token"),
            "relation_disposition": "reviewed_none",
            "relation_review_hash": semantic.get("transition_hash"),
            "relation_review_reason": "No honest relation is added by the isolated benchmark observation.",
        },
    )


async def _warm_public_recall(client: Any, *, timeout: float) -> None:
    """Wait on the served catalog's public readiness condition before timing."""
    deadline = time.perf_counter() + timeout
    while True:
        payload = _decode_call(
            await client.call_tool_mcp(
                "ask_memory",
                {"query": "durable-closure-warmup", "mode": "keyword", "graph": False, "limit": 1},
            )
        )
        outcome, code = _result_outcome(payload)
        if outcome == "ok":
            return
        if code != "RETRIEVAL_INDEX_WARMING" or time.perf_counter() >= deadline:
            raise RuntimeError(f"managed public recall warm-up failed: {code}")
        error = payload.get("error")
        retry_ms = error.get("retry_after_ms", 250) if isinstance(error, Mapping) else 250
        await asyncio.sleep(min(1.0, max(0.01, float(retry_ms) / 1000.0)))


def _permit_refusal_envelopes(client: Any) -> None:
    """Let the MCP client deliver a public refusal envelope to the harness.

    This revision's generated ``ask_memory`` success schema excludes the
    documented warming/refusal envelope.  Leaving that client-side validator
    on turns a server response into a transport exception and loses the exact
    refusal the benchmark is intended to gate.  Tool discovery still happened
    over MCP; only local success-shape validation is suppressed.
    """
    for owner in (client, getattr(client, "session", None)):
        schemas = getattr(owner, "_tool_output_schemas", None)
        if isinstance(schemas, dict):
            for name in tuple(schemas):
                schemas[name] = None


def _read_ledger(path: Path, workflow_started: float) -> list[dict[str, Any]]:
    ledger = path / "ledger.jsonl"
    if not ledger.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"ledger row {line_number} is malformed") from error
        if not isinstance(row, dict):
            raise ValueError(f"ledger row {line_number} is not an object")
        rows.append(row)
    return rows


def _ledger_line_count(path: Path) -> int:
    """Keep a physical row count even when parsing a ledger row fails."""
    ledger = path / "ledger.jsonl"
    if not ledger.is_file():
        return 0
    return len(ledger.read_text(encoding="utf-8").splitlines())


def _ack_percentiles(calls: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    values = sorted(
        float(call["client_elapsed_ms"])
        for call in calls
        if call.get("mutation_ack") and call.get("outcome") == "ok"
    )
    if not values:
        return {"p50_ms": None, "p95_ms": None}
    at = lambda fraction: values[min(len(values) - 1, max(0, int(len(values) * fraction + 0.999) - 1))]
    return {"p50_ms": at(0.50), "p95_ms": at(0.95)}


async def run_public_workflow(
    *,
    python: Path,
    state: Path,
    vault: Path,
    pages: int,
    profile: str,
    timeout: float,
    variant: str = "optimized",
    artifacts_manifest: Path | None = None,
) -> dict[str, Any]:
    """Run the complete public workflow through one persistent stdio session."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    if profile not in {MODEL_FREE_PROFILE, REAL_EXTRACTION_PROFILE}:
        raise ValueError(f"unsupported profile: {profile}")
    workflow_plan(variant)
    state.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    corpus = materialize_corpus(vault, pages=pages)
    artifacts = load_artifact_manifest(artifacts_manifest) if artifacts_manifest else None
    env = benchmark_environment(state, vault)
    instrumentation_dir = install_subprocess_instrumentation(state)
    env["DURABLE_CLOSURE_INSTRUMENTATION"] = str(state / "instrumentation.json")
    env["DURABLE_CLOSURE_INSTRUMENTATION_CONTROL"] = str(state / "instrumentation-control.json")
    env["PYTHONPATH"] = os.pathsep.join((str(instrumentation_dir), env["PYTHONPATH"]))
    if profile == MODEL_FREE_PROFILE:
        env.update({"EXOMEM_DISABLE_MEDIA_EXTRACTION": "1", "EXOMEM_DISABLE_CLIP": "1"})
    transport = StdioTransport(
        command=str(python),
        args=["-m", "exomem", "--transport", "stdio"],
        env=env,
        cwd=str(ROOT),
        keep_alive=False,
        log_file=state / "stdio.log",
    )
    calls: list[dict[str, Any]] = []
    workflow_started = time.perf_counter()
    closure_finished = workflow_started
    media: dict[str, Any] = {
        "status": "blocked",
        "reason": "--artifacts-manifest with public HTTPS file handles is required",
        "extraction_convergence_ms": None,
        "engine_versions": None,
    }
    marker = ""
    path = ""
    remembered: dict[str, Any] = {"success": False}
    remembered_observation: dict[str, Any] = {"success": False}
    tracker_observation: dict[str, Any] = {"success": False}
    critique_observation: dict[str, Any] = {"success": False}
    tracker_correction: dict[str, Any] = {"success": False}
    archived_read: dict[str, Any] = {"success": False}
    direct: dict[str, Any] = {"success": False}
    tracker_read: dict[str, Any] = {"success": False}
    stale_relation_read: dict[str, Any] = {"success": False}
    recall: dict[str, Any] = {"success": False}
    extraction_enqueue_started: float | None = None
    extraction_sidecar_paths: list[str] = []
    media_convergence_wall_ms: float | None = None
    useful_closure_finished = workflow_started
    client = Client(transport, timeout=timeout, init_timeout=timeout)
    async with client:
        registered_tools = {tool.name: tool for tool in await client.list_tools()}
        _permit_refusal_envelopes(client)
        missing = required_tools_missing(registered_tools)
        if missing:
            raise RuntimeError(f"registered product MCP surface missing: {missing}")
        await _warm_public_recall(client, timeout=timeout)
        await instrumentation_command(state, action="reset", phase="timed", timeout=timeout)
        # Store the shared monotonic origin once before append-only call records.
        workflow_started = time.perf_counter()
        ledger_before = len(_read_ledger(state / "ledger", workflow_started))
        calls.append({"_origin": workflow_started})
        marker = f"durable-closure-{uuid.uuid4().hex}"

        async def workflow_call(tool: str, arguments: dict[str, Any], *, phase: str = "workflow") -> dict[str, Any]:
            payload = await _call(client, calls, tool, arguments)
            calls[-1].update({"phase": phase, "probe": False})
            if variant == "stress" and calls[-1]["mutation_ack"]:
                await _call(
                    client,
                    calls,
                    "ask_memory",
                    {"query": marker, "mode": "hybrid", "graph": True, "rerank": False, "limit": 5},
                )
                calls[-1].update({"phase": "probe", "probe": True})
            return payload

        await workflow_call("bootstrap", {"profile": "compact"})

        # Evidence is preserved and queued before any compiled note can cite it.
        evidence_paths: list[str] = []
        if artifacts is not None:
            preserved = await workflow_call(
                "preserve_artifacts",
                {
                    "scope": "durable-closure",
                    "category": "benchmark-evidence",
                    "files": [
                        {key: item[key] for key in ("file_id", "download_url", "mime_type", "file_name")}
                        for item in artifacts
                    ],
                }, phase="media-enqueue",
            )
            files = preserved.get("files") if isinstance(preserved.get("files"), list) else []
            evidence_paths = validated_evidence_paths(
                artifacts, [item for item in files if isinstance(item, Mapping)]
            ) or []
            hashes_match = len(evidence_paths) == len(artifacts)
            process_requests = process_media_requests(registered_tools["process_media"], evidence_paths)
            extraction_enqueue_started = time.perf_counter()
            process_payloads = [
                await workflow_call("process_media", request, phase="media-enqueue") for request in process_requests
            ]
            media_rows = [row for payload in process_payloads for row in media_result_rows(payload)]
            process_ok = (
                all(_outcome_ok(payload) for payload in process_payloads)
                and media_rows_match_request(evidence_paths, media_rows)
            )
            extraction_sidecar_paths = [
                str(row.get("sidecar_path") or "") for row in media_rows if isinstance(row.get("sidecar_path"), str)
            ]
            media = {
                "status": "ready" if hashes_match and len(evidence_paths) == 3 and process_ok else "fail",
                "fixture_provenance": [{key: item[key] for key in item} for item in artifacts],
                "preserved_hashes_match": hashes_match,
                "evidence_paths": evidence_paths,
                "process_calls": len(process_payloads),
                "process_selection": "paths" if len(process_requests) == 1 else "legacy-single-path",
                "media_results": media_rows,
                "extraction_convergence_ms": None,
                "engine_versions": None,
            }
        remember_arguments = {
            "title": "Durable closure benchmark result",
            "note_type": "insight",
            "content": "## Observations\n\n"
            f"- [finding] {marker} survives the public workflow #benchmark ^durable-closure\n",
            "response_detail": "full",
            "sources": evidence_paths,
        }
        validation = await workflow_call(
            "remember",
            {**remember_arguments, "validate_only": True},
        )
        remembered = await workflow_call(
            "remember",
            {
                **remember_arguments,
                "draft_id": validation.get("draft_id"),
                "draft_hash": validation.get("draft_hash"),
                "draft_token": validation.get("draft_token"),
                "relation_disposition": "reviewed_none",
                "relation_review_hash": validation.get("draft_hash"),
                "relation_review_reason": "No honest relation exists in the isolated benchmark fixture.",
            },
        )
        path = str(remembered.get("path") or "")
        if path:
            remembered_observation = await _observe_reviewed(
                client,
                calls,
                path=path,
                category="evidence",
                content=f"{marker} observation",
                anchor="follow-up",
                call_tool=workflow_call,
            )
        else:
            remembered_observation = {"success": False}

        tracker_path = corpus["active_tracker"]
        tracker_observation = await _observe_reviewed(
            client,
            calls,
            path=tracker_path,
            category="action",
            content=f"{marker} tracker update",
            anchor="benchmark-tracker",
            call_tool=workflow_call,
        )
        critique_observation = await _observe_reviewed(
            client,
            calls,
            path=corpus["critique"],
            category="finding",
            content=f"{marker} critique update",
            anchor="benchmark-critique",
            call_tool=workflow_call,
        )
        archived_read = await workflow_call("read_memory", {"path": corpus["archived_note"]})

        stale_line = "- supports [[Knowledge Base/Notes/Insights/archived-runbook]]"
        edit_operation = {"kind": "replace_string", "old_string": stale_line, "new_string": "", "replace_all": False}
        tracker_correction = await workflow_call(
            "edit_memory",
            {
                "path": corpus["stale_relation"],
                "why": "correct stale benchmark relation",
                "operation": edit_operation,
            },
        )

        recall = await workflow_call(
            "ask_memory",
            {"query": marker, "mode": "hybrid", "graph": True, "rerank": False, "limit": 10}, phase="verification",
        )
        direct = await workflow_call("read_memory", {"path": path}, phase="verification") if path else {"success": False}
        tracker_read = await workflow_call("read_memory", {"path": tracker_path}, phase="verification")
        stale_relation_read = await workflow_call("read_memory", {"path": corpus["stale_relation"]}, phase="verification")

        closure_instrumentation = await instrumentation_command(
            state, action="snapshot", phase="closure", timeout=timeout
        )
        useful_closure_finished = time.perf_counter()

        # Continue with independent note/relation closure before awaiting media.
        # The media convergence clock starts at its earlier public enqueue.
        if profile == REAL_EXTRACTION_PROFILE and artifacts is not None:
            proof: dict[str, Any] | None = None
            deadline = time.perf_counter() + timeout
            while extraction_sidecar_paths and time.perf_counter() < deadline:
                reads = [
                    await workflow_call("read_memory", {"path": sidecar_path}, phase="convergence-poll")
                    for sidecar_path in extraction_sidecar_paths
                ]
                proof = extraction_proof(artifacts, reads)
                if proof is not None:
                    break
                await asyncio.sleep(0.2)
            if proof is None:
                media.update(
                    {
                        "status": "blocked",
                        "reason": "public extraction-content and engine-version verification did not converge",
                    }
                )
            else:
                media.update(
                    {
                        "status": "ready",
                        "extraction_convergence_ms": (time.perf_counter() - extraction_enqueue_started) * 1000.0
                        if extraction_enqueue_started is not None
                        else None,
                        **proof,
                    }
                )
                media_convergence_wall_ms = (time.perf_counter() - workflow_started) * 1000.0

        convergence_instrumentation = await instrumentation_command(
            state, action="snapshot", phase="convergence", timeout=timeout
        )
        closure_finished = time.perf_counter()
    shutdown_finished = time.perf_counter()

    # Drop the private clock anchor before persisting/reporting measurements.
    call_records = calls[1:]
    required_payloads = (remembered, remembered_observation, tracker_observation, critique_observation, tracker_correction, archived_read)
    mutation_success = all(_outcome_ok(payload) for payload in required_payloads)
    final_exact = bool(
        path
        and mutation_success
        and _outcome_ok(direct)
        and _outcome_ok(tracker_read)
        and _contains_marker(direct, marker)
        and all(_contains_marker(direct, evidence_path) for evidence_path in evidence_paths)
        and _contains_marker(tracker_read, marker)
        and recall_has_exact_hit(recall, path, marker)
        and not _contains_marker(stale_relation_read, stale_line)
    )
    closure = evaluate_useful_closure(
        calls=call_records,
        final_read_your_write=final_exact,
        graph_warming_components=_warming_components(recall),
    )
    ledger_path = state / "ledger"
    ledger_rows: list[dict[str, Any]] = []
    try:
        ledger_rows = _read_ledger(ledger_path, workflow_started)[ledger_before:]
        ledger_clock_ok = ledger_clock_continuous(call_records, ledger_rows)
        call_records = attach_ledger_measurements(call_records, ledger_rows)
        ledger_measurements = summarize_ledger_calls(
            [{**row, **interval} for row, interval in zip(ledger_rows, ledger_intervals(ledger_rows), strict=True)]
        )
        if not ledger_clock_ok:
            for key in ("ledger_observation_span_ms", "server_occupied_union_ms", "server_idle_within_observed_span_ms"):
                ledger_measurements[key] = None
            ledger_measurements["occupancy_reason"] = "invalid: UTC ledger clock discontinuity against client trace"
        instrumentation = read_instrumentation(state)
        closure_snapshot = closure_instrumentation["snapshots"]["closure"]
        convergence_snapshot = convergence_instrumentation["snapshots"]["convergence"]
    except (KeyError, RuntimeError, TypeError, ValueError) as error:
        return invalid_measurement_report(
            calls=call_records,
            ledger_row_count=len(ledger_rows) if ledger_rows else _ledger_line_count(ledger_path),
            reason=str(error),
        )
    return {
        "variant": variant,
        "profile": profile,
        "status": workflow_status(core_passed=closure["passed"], media_status=media["status"]),
        "corpus": corpus,
        **lifecycle_timings(
            workflow_started=workflow_started,
            closure_finished=closure_finished,
            shutdown_finished=shutdown_finished,
        ),
        "useful_closure_wall_ms": (useful_closure_finished - workflow_started) * 1000.0,
        "public_call_count": len(call_records),
        "call_counts": {
            "probes": sum(1 for call in call_records if call.get("probe")),
            "verification": sum(1 for call in call_records if call.get("phase") == "verification"),
            "convergence_polls": sum(1 for call in call_records if call.get("phase") == "convergence-poll"),
            "retries": sum(
                1
                for call in call_records
                if call.get("request_shape", {}).get("operation") == "retry"
            ),
        },
        "calls": call_records,
        "write_ack": _ack_percentiles(call_records),
        "useful_closure": closure,
        "verification": {
            "direct_marker": _contains_marker(direct, marker),
            "tracker_marker": _contains_marker(tracker_read, marker),
            "recall_hit": recall_has_exact_hit(recall, path, marker),
            "evidence_citations": all(_contains_marker(direct, evidence_path) for evidence_path in evidence_paths),
            "stale_relation_absent": not _contains_marker(stale_relation_read, stale_line),
            "mutations_succeeded": mutation_success,
        },
        "ledger": ledger_measurements,
        "connector_overhead_ms": None,
        "connector_overhead_reason": "stdio client wall and server ledger are independently measured but not request-correlated",
        "graph_invocations": {
            "at_closure": {
                "drain_attempts": closure_snapshot["graph_drain_attempts"],
                "drain_completed": closure_snapshot["graph_drain_completed"],
                "rebuild_attempts": closure_snapshot["graph_rebuild_attempts"],
                "rebuild_completed": closure_snapshot["graph_rebuild_completed"],
            },
            "at_convergence": {
                "drain_attempts": convergence_snapshot["graph_drain_attempts"],
                "drain_completed": convergence_snapshot["graph_drain_completed"],
                "rebuild_attempts": convergence_snapshot["graph_rebuild_attempts"],
                "rebuild_completed": convergence_snapshot["graph_rebuild_completed"],
            },
            "after_shutdown": {
                "drain_attempts": instrumentation["graph_drain_attempts"],
                "drain_completed": instrumentation["graph_drain_completed"],
                "rebuild_attempts": instrumentation["graph_rebuild_attempts"],
                "rebuild_completed": instrumentation["graph_rebuild_completed"],
            },
        },
        "source_scans": {
            "coverage": instrumentation["scan_coverage"],
            "at_closure": {
                "pages": closure_snapshot["source_scan_pages"],
                "bytes": closure_snapshot["source_scan_bytes"],
            },
            "at_convergence": {
                "pages": convergence_snapshot["source_scan_pages"],
                "bytes": convergence_snapshot["source_scan_bytes"],
            },
            "after_shutdown": {
                "pages": instrumentation["source_scan_pages"],
                "bytes": instrumentation["source_scan_bytes"],
            },
        },
        "instrumentation": {
            "method": "child-process sitecustomize wrappers",
            "error": instrumentation.get("instrumentation_error"),
        },
        "media": media,
        "media_convergence_wall_ms": media_convergence_wall_ms,
        "full_convergence_ms": None,
        "full_convergence_reason": "graph, lexical, and embedding projection convergence are not publicly proven by this harness",
        "initial_graph_readiness": {"status": "unmeasured", "reason": "managed keyword warm-up does not establish hybrid graph readiness"},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--profile", choices=(MODEL_FREE_PROFILE, REAL_EXTRACTION_PROFILE), default=MODEL_FREE_PROFILE)
    parser.add_argument("--variant", choices=("optimized", "stress"), default="optimized")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--state", type=Path, required=True, help="empty disposable benchmark state root")
    parser.add_argument("--vault", type=Path, required=True, help="empty disposable benchmark vault")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--artifacts-manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.vault.exists() and any(args.vault.iterdir()):
        parser.error("--vault must be empty and disposable")
    if args.state.exists() and any(args.state.iterdir()):
        parser.error("--state must be empty and disposable")
    args.state.mkdir(parents=True, exist_ok=True)
    args.vault.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(
        run_public_workflow(
            python=args.python,
            state=args.state,
            vault=args.vault,
            pages=args.pages,
            profile=args.profile,
            timeout=args.timeout,
            variant=args.variant,
            artifacts_manifest=args.artifacts_manifest,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"pass", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
