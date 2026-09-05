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
import json
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
                "code": str(call.get("error_code") or "UNCLASSIFIED_REFUSAL"),
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
    intervals = [
        (float(call["started_ms"]), float(call["ended_ms"]))
        for call in calls
        if "started_ms" in call and "ended_ms" in call
    ]
    duration_sum = sum(float(call.get("duration_ms") or 0.0) for call in calls)
    total_sum = sum(float(call.get("total_ms") or 0.0) for call in calls)
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


def attach_ledger_measurements(
    calls: Sequence[Mapping[str, Any]], ledger_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Join ordered public calls to their same-process ledger rows conservatively.

    The ledger records no client monotonic timestamps, so this adds per-call
    server durations but leaves an interval explicitly null.  An unexpected
    tool ordering is unmeasured rather than guessed from a similarly named row.
    """
    joined: list[dict[str, Any]] = []
    row_index = 0
    for call in calls:
        result = dict(call)
        result["server_duration_ms"] = None
        result["server_total_ms"] = None
        result["server_interval"] = None
        if row_index < len(ledger_rows) and ledger_rows[row_index].get("tool") == call.get("tool"):
            row = ledger_rows[row_index]
            result["server_duration_ms"] = float(row["duration_ms"]) if row.get("duration_ms") is not None else None
            result["server_total_ms"] = float(row["total_ms"]) if row.get("total_ms") is not None else None
            row_index += 1
        joined.append(result)
    return joined


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
    tracker = kb / "Notes" / "Operations" / "active-tracker.md"
    archived = kb / "Notes" / "Operations" / "archived-runbook.md"
    critique = kb / "Notes" / "Research" / "critique.md"
    documents = {
        tracker: (
            "---\ntype: insight\ntitle: Active tracker\nstatus: active\nupdated: 2026-09-05\n---\n\n"
            "# Active tracker\n\n## Observations\n- [constraint] Keep workflow evidence durable #benchmark ^tracker\n"
        ),
        archived: (
            "---\ntype: insight\ntitle: Archived operational note\nstatus: archived\nupdated: 2026-08-01\n---\n\n"
            "# Archived operational note\n\n## Observations\n- [history] Previous recovery sequence is retained #operations ^archived\n"
        ),
        critique: (
            "---\ntype: insight\ntitle: Workflow critique\nstatus: active\nupdated: 2026-09-04\n---\n\n"
            "# Workflow critique\n\n## Observations\n- [finding] Retrieval must survive writes #benchmark ^critique\n"
        ),
    }
    for index in range(max(0, pages - len(documents))):
        documents[kb / "Notes" / "Reference" / f"reference-{index:05d}.md"] = (
            "---\n"
            f"type: insight\ntitle: Reference {index}\nstatus: active\nupdated: 2026-08-01\n"
            "---\n\n"
            f"# Reference {index}\n\n## Observations\n- [fact] Heterogeneous reference {index} #benchmark ^ref-{index}\n"
        )
    total_bytes = 0
    for path, content in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        total_bytes += len(content.encode("utf-8"))
    return {
        "generator": "durable-closure-markdown-v1",
        "pages": len(documents),
        "bytes": total_bytes,
        "active_tracker": tracker.relative_to(vault).as_posix(),
        "archived_note": archived.relative_to(vault).as_posix(),
        "critique": critique.relative_to(vault).as_posix(),
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
    return {"success": False, "error": {"code": "UNPARSEABLE_MCP_RESULT"}}


def _result_outcome(payload: Mapping[str, Any]) -> tuple[str, str | None]:
    if payload.get("success") is False:
        error = payload.get("error")
        return "refused", str(error.get("code")) if isinstance(error, Mapping) else "UNKNOWN"
    error = payload.get("error")
    if isinstance(error, Mapping):
        return "refused", str(error.get("code") or "UNKNOWN")
    return "ok", None


async def _call(client: Any, calls: list[dict[str, Any]], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    # The raw MCP response is intentional: a public refusal is valid protocol
    # data even when FastMCP's generated success-only output schema rejects it.
    result = await client.call_tool_mcp(tool, arguments)
    ended = time.perf_counter()
    payload = _decode_call(result)
    outcome, code = _result_outcome(payload)
    calls.append(
        {
            "tool": tool,
            "outcome": outcome,
            "error_code": code,
            "started_ms": (started - calls[0]["_origin"]) * 1000.0 if calls else 0.0,
            "ended_ms": (ended - calls[0]["_origin"]) * 1000.0 if calls else (ended - started) * 1000.0,
            "client_elapsed_ms": (ended - started) * 1000.0,
        }
    )
    return payload


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
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            # Ledger lacks a monotonic client start, so only server sums are
            # measured here. Interval occupancy remains null unless a trace has
            # an observed interval.
            rows.append(row)
    return rows


def _ack_percentiles(calls: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    values = sorted(
        float(call["client_elapsed_ms"])
        for call in calls
        if call.get("tool") in {"remember", "observe_memory", "edit_memory"}
        and call.get("outcome") == "ok"
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
) -> dict[str, Any]:
    """Run the non-media core through one persistent installed stdio MCP session.

    A media-handle adapter is deliberately a separate concern: absent a real
    HTTPS handle it returns a blocked media row rather than pretending a local
    fixture file exercised public preservation.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    if profile not in {MODEL_FREE_PROFILE, REAL_EXTRACTION_PROFILE}:
        raise ValueError(f"unsupported profile: {profile}")
    workflow_plan(variant)
    state.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    corpus = materialize_corpus(vault, pages=pages)
    env = benchmark_environment(state, vault)
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
    client = Client(transport, timeout=timeout, init_timeout=timeout)
    async with client:
        tools = {tool.name for tool in await client.list_tools()}
        _permit_refusal_envelopes(client)
        missing = required_tools_missing(tools)
        if missing:
            raise RuntimeError(f"registered product MCP surface missing: {missing}")
        await _warm_public_recall(client, timeout=timeout)
        # Store the shared monotonic origin once before append-only call records.
        workflow_started = time.perf_counter()
        ledger_before = len(_read_ledger(state / "ledger", workflow_started))
        calls.append({"_origin": workflow_started})
        await _call(client, calls, "bootstrap", {"profile": "compact"})
        marker = f"durable-closure-{uuid.uuid4().hex}"
        remember_arguments = {
            "title": "Durable closure benchmark result",
            "note_type": "insight",
            "content": "## Observations\n\n"
            f"- [finding] {marker} survives the public workflow #benchmark ^durable-closure\n",
            "response_detail": "full",
        }
        validation = await _call(
            client,
            calls,
            "remember",
            {**remember_arguments, "validate_only": True},
        )
        remembered = await _call(
            client,
            calls,
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
        if variant == "stress":
            await _call(
                client, calls, "ask_memory", {"query": marker, "mode": "keyword", "graph": True, "limit": 5}
            )
        if path:
            await _call(
                client,
                calls,
                "observe_memory",
                {"path": path, "category": "evidence", "content": f"{marker} observation", "id": "follow-up"},
            )
            if variant == "stress":
                await _call(
                    client, calls, "ask_memory", {"query": marker, "mode": "keyword", "graph": True, "limit": 5}
                )
        await _call(client, calls, "ask_memory", {"query": marker, "mode": "keyword", "graph": True, "limit": 5})
        direct = await _call(client, calls, "read_memory", {"path": path}) if path else {"success": False}

    # Drop the private clock anchor before persisting/reporting measurements.
    call_records = calls[1:]
    final_exact = bool(path and direct and direct.get("success") is not False)
    closure = evaluate_useful_closure(
        calls=call_records,
        final_read_your_write=final_exact,
        graph_warming_components=(),
    )
    ledger_rows = _read_ledger(state / "ledger", workflow_started)[ledger_before:]
    call_records = attach_ledger_measurements(call_records, ledger_rows)
    ledger_measurements = summarize_ledger_calls(ledger_rows)
    return {
        "variant": variant,
        "profile": profile,
        "status": "pass" if closure["passed"] else "fail",
        "corpus": corpus,
        "workflow_wall_ms": (time.perf_counter() - workflow_started) * 1000.0,
        "public_call_count": len(call_records),
        "calls": call_records,
        "write_ack": _ack_percentiles(call_records),
        "useful_closure": closure,
        "ledger": ledger_measurements,
        "connector_overhead_ms": None,
        "connector_overhead_reason": "stdio client wall and server ledger are independently measured but not request-correlated",
        "graph_invocations": {"incremental": None, "rebuild": None, "reason": "no measured hook installed"},
        "source_scans": {"pages": None, "bytes": None, "reason": "no measured hook installed"},
        "media": {
            "status": "blocked",
            "reason": "a real HTTPS client-file handle adapter is required; local fixture bytes are not public preservation evidence",
            "extraction_convergence_ms": None,
            "engine_versions": None,
        },
        "full_convergence_ms": None,
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
    args = parser.parse_args(argv)
    if args.pages > 50:
        parser.error("workers may only run <=50 pages; root owns serial large-corpus samples")
    if args.vault.exists() and any(args.vault.iterdir()):
        parser.error("--vault must be empty and disposable")
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
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
