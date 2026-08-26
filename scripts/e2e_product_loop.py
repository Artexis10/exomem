#!/usr/bin/env python
"""Installed-wheel black-box E2E for Exomem's governed product lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratch_root  # noqa: E402

WINDOWS = sys.platform == "win32"


def _clean_env(home: Path, vault: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("EXOMEM_", "KB_MCP_", "PYTHONPATH"))
    }
    env.update(
        {
            "HOME": str(home),
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_CONFIG_PATH": str(home / "exomem-config.json"),
            # Logs are the one piece of process state `resolve_log_dir()` puts
            # somewhere machine-global rather than under HOME (#569), so
            # isolating HOME is not enough: an installed server started here
            # opens the same `exomem.log` as any exomem service already running
            # on this machine, and on Windows the second opener gets EACCES and
            # the whole run dies at `initialize` with "Connection closed". CI
            # never sees it because nothing else is running there; a developer
            # box running the service sees it every time. Isolate the log root
            # for the same reason the vault and config are isolated.
            "EXOMEM_LOG_DIR": str(home / "logs"),
            "PYTHONUTF8": "1",
        }
    )
    if WINDOWS:
        env["USERPROFILE"] = str(home)
    return env


def _write_records_fixture(_vault: Path) -> dict[str, str]:
    """Describe Records authoring inputs without pre-writing canonical files."""
    collection = "Knowledge Base/Records/Health/X3/_collection.md"
    source = "Knowledge Base/Records/Health/X3/Items"
    manifest_text = """---
type: collection
exomem_id: 9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8
title: X3 training sessions
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [occurred_on, title]
  fields:
    occurred_on: {type: date, required: true}
    title: {type: string, required: true}
    status: {type: enum, enum: [completed, partial, aborted]}
    movements: {type: array, items: {type: object}}
    note: {type: string}
    provenance: {type: string}
record_presentation:
  version: 1
  summary:
    - {field: title, label: Session}
    - {field: status, label: Status}
  tables:
    - field: movements
      label: Movements
      columns:
        - {field: movement, label: Movement, type: string}
        - {field: band, label: Band, type: string}
        - {field: repetitions, label: Repetitions, type: string}
  notes:
    - {field: note, label: Note}
  details:
    - {field: provenance, label: Provenance}
views:
  completed-sessions:
    query:
      columns: [occurred_on, title, status]
---

Human-owned X3 sessions remain ordinary Markdown.
"""
    return {
        "collection": collection,
        "source": source,
        "manifest_text": manifest_text,
        "revised_manifest_text": manifest_text.replace(
            "title: X3 training sessions", "title: X3 training sessions (revised)", 1
        ),
    }


def _write_manual_records_fixture(vault: Path) -> dict[str, str]:
    """Create the separate human-authored markdown-log/template journey."""
    collection = "Knowledge Base/Records/Health/Manual X3/_collection.md"
    log = "Knowledge Base/Records/Health/Manual X3/Training Log.md"
    template = "Knowledge Base/Templates/Records/Health/Manual X3/X3 Push.md"
    manifest_path = vault / collection
    log_path = vault / log
    template_path = vault / template
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        """---
type: collection
exomem_id: 8ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8
title: Manual X3 training sessions
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-log
  source: Training Log.md
  format_version: 1
  section:
    level: 2
    title: Sessions (newest first)
  item_heading:
    level: 3
    fields:
      - name: occurred_on
        type: date
        format: "%Y-%m-%d"
      - name: title
        type: string
    separator: " · "
  defaults:
    status: completed
  insertion: newest-first
  child_rows:
    prefix: "- "
    delimiter: "|"
    fields: [movement, band, repetitions]
    container_field: movements
item_schema:
  natural_key: [occurred_on, title]
  fields:
    occurred_on: {type: date, required: true}
    title: {type: string, required: true}
    status: {type: enum, enum: [completed, partial, aborted]}
    movements: {type: array, items: {type: object}}
templates:
  - path: Knowledge Base/Templates/Records/Health/Manual X3/X3 Push.md
links:
  plans:
    - reference: exomem://memory/81947000-4c22-46e4-9874-23fed028314b
      query: {filters: {status: completed}, limit: 24}
---
""",
        encoding="utf-8",
    )
    log_path.write_text(
        """---
type: tracker
---
# X3 Training Log

## Sessions (newest first)
""",
        encoding="utf-8",
    )
    template_path.write_text(
        """### {{date}} · Push
- Overhead Press | white short paraforce | 8
- Deadlift | grey short paraforce | e2e record row-only sentinel
""",
        encoding="utf-8",
    )
    return {"collection": collection, "log": log, "template": template}


def _insert_manual_x3_session(vault: Path, fixture: dict[str, str]) -> None:
    log = vault / fixture["log"]
    template = vault / fixture["template"]
    entry = template.read_text(encoding="utf-8").replace("{{date}}", "2026-08-03")
    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "## Sessions (newest first)\n", f"## Sessions (newest first)\n\n{entry}\n", 1
        ),
        encoding="utf-8",
    )


def _write_planning_fixtures(_vault: Path) -> dict[str, dict[str, Any]]:
    """Describe the two Planning manifests created through the public product command."""
    fields = """    title: {type: string, required: true}
    kind: {type: string}
    status: {type: string}
    lifecycle: {type: string}
    priority: {type: string}
    commitment: {type: string}
    horizon: {type: string}
    health: {type: string}
    window_start: {type: date}
    window_end: {type: date}
    area: {type: string}
    parent: {type: string}
    progress_evidence: {type: array, items: {type: object}}
    execution: {type: array, items: {type: object}}
    tags: {type: array, items: {type: string}}
"""

    def manifest(collection_id: str, title: str, *, domain: str | None = None) -> str:
        domain_line = f"domain: {domain}\n" if domain is not None else ""
        domain_field = "    domain: {type: string}\n" if domain is not None else ""
        return f"""---
type: collection
exomem_id: {collection_id}
title: {title}
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
{domain_line}storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
{fields}{domain_field}---
Planning remains ordinary human-owned Markdown.
"""

    return {
        "software": {
            "collection": "Knowledge Base/Planning/Software/_collection.md",
            "manifest_text": manifest(
                "6dd5763e-cabe-4aed-9b9b-fcb0a372e4c5", "Software delivery intent"
            ),
        },
        "nonsoftware": {
            "collection": "Knowledge Base/Planning/Home/_collection.md",
            "manifest_text": manifest(
                "1b2d04d7-0748-41da-b4f3-cb5313f38cbe", "Home resilience intent", domain="home"
            ),
            "records_view": {
                "collection": "exomem://memory/9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8",
                "role": "progress",
                "view": "completed-sessions",
            },
        },
    }


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(command)}\n"
            f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    return proc


def _result_data(result: Any) -> Any:
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool returned an error: {result}")
    data = getattr(result, "data", None)
    if data is not None:
        return _unwrap_result(data)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return _unwrap_result(structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try:
                return _unwrap_result(json.loads(text))
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"MCP result had no structured data: {result}")


def _unwrap_result(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _unwrap_result(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return _unwrap_result(value.model_dump(mode="json"))
    if isinstance(value, dict):
        value = {key: _unwrap_result(item) for key, item in value.items()}
        if "result" in value:
            return value["result"]
    elif isinstance(value, (list, tuple)):
        return [_unwrap_result(item) for item in value]
    return value


async def _call(client, name: str, arguments: dict[str, Any], timeout: float) -> Any:
    result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=timeout)
    return _result_data(result)


def _mutation_diagnostics(result: Any, *, operation: str) -> Any:
    """Validate a full committed terminal and return its leaf diagnostics."""
    if (
        not isinstance(result, dict)
        or result.get("ok") is not True
        or result.get("status") != "committed"
        or result.get("mutated") is not True
        or "diagnostics" not in result
    ):
        raise RuntimeError(
            f"{operation} mutation did not return a committed full terminal: {result!r}"
        )
    diagnostics = result["diagnostics"]
    if isinstance(diagnostics, dict) and diagnostics.get("graph_sync") == "failed":
        code = diagnostics.get("graph_sync_code", "unspecified graph synchronization error")
        raise RuntimeError(f"{operation} graph synchronization failed: {code}")
    return diagnostics


def _single_affected_path(result: Any, *, operation: str) -> str:
    """Return the one canonical item path committed by a mutation."""
    paths = result.get("affected_paths") if isinstance(result, dict) else None
    if (
        not isinstance(paths, list)
        or len(paths) != 1
        or not isinstance(paths[0], str)
        or not paths[0]
    ):
        raise RuntimeError(f"{operation} did not report one affected item path: {result!r}")
    return paths[0]


def _maintenance_diagnostics(result: Any, *, operation: str) -> Any:
    """Unwrap a tracked commit while preserving a raw no-op maintenance report."""
    terminal_fields = {"ok", "status", "mutated", "diagnostics"}
    if isinstance(result, dict) and terminal_fields.intersection(result):
        return _mutation_diagnostics(result, operation=operation)
    return result


def _remote_maintenance_refusal(result: Any) -> dict[str, Any]:
    """Prove write maintenance was refused at the remote operator boundary."""
    error = result.get("error") if isinstance(result, dict) else None
    if (
        not isinstance(error, dict)
        or result.get("success") is not False
        or error.get("code") != "MAINTENANCE_REQUIRES_CLI"
        or error.get("status") != "terminal"
        or error.get("committed") is not False
    ):
        raise RuntimeError(
            "maintain_memory did not return the operator-only refusal: "
            f"{result!r}"
        )
    return error


def _operator_reconcile_data(stdout: str) -> dict[str, Any]:
    """Require the host reconcile's exact successful derived-repair report."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError("operator reconcile returned no valid JSON envelope") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"operator reconcile did not succeed: {payload!r}")
    data = payload.get("data")
    if (
        payload.get("success") is not True
        or not isinstance(data, dict)
        or data.get("dry_run") is not False
        or data.get("graph_sync") != "completed"
        or data.get("graph_status") != "refreshed"
        or data.get("graph_refreshed") != 1
        or data.get("references_status") != "refreshed"
        or data.get("references_refreshed") != 1
    ):
        raise RuntimeError(f"operator reconcile did not succeed: {payload!r}")
    return data


def _run_operator_reconcile(
    executable: Path,
    env: dict[str, str],
    work: Path,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Run write-capable maintenance through its host-operator CLI surface."""
    completed = subprocess.run(
        [str(executable), "maintain", "--reconcile", "--json"],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        timeout=max(120.0, timeout * 6),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "operator reconcile command failed "
            f"({completed.returncode}): {completed.stderr[-2000:]!r}"
        )
    return _operator_reconcile_data(completed.stdout)


async def _call_mutation(
    client,
    name: str,
    arguments: dict[str, Any],
    timeout: float,
) -> Any:
    """Call a mutation with full detail while proving its decisive envelope."""
    result = await _call(
        client,
        name,
        {**arguments, "response_detail": "full"},
        timeout,
    )
    return _mutation_diagnostics(result, operation=name)


async def _call_maintenance(
    client,
    name: str,
    arguments: dict[str, Any],
    timeout: float,
) -> Any:
    """Call maintenance with full detail across no-op and tracked-commit paths."""
    result = await _call(
        client,
        name,
        {**arguments, "response_detail": "full"},
        timeout,
    )
    return _maintenance_diagnostics(result, operation=name)


#: How long the graph may take to converge after a write before the product is
#: considered broken. A write no longer waits for its own graph rebuild, so the
#: sidecar genuinely is unavailable for a moment afterwards -- that is designed
#: behaviour, not a defect. What must still hold is that it converges without
#: anyone asking it to, so this stays a real gate: generous enough that a
#: healthy rebuild over the E2E's small vault always fits, short enough that a
#: graph which never converges fails the run instead of hanging it.
_GRAPH_CONVERGENCE_SECONDS = 120.0
_GRAPH_POLL_SECONDS = 0.5


def _server_side_graph_state() -> str:
    """Read the graph's own state from disk for a convergence failure report.

    Necessary because the failure is otherwise undiagnosable from the artifact
    it produces. `connect_memory` answers `available: false` with a `reason`
    string and nothing behind it, so a CI failure could not distinguish "the
    rebuild is still running" from "the graph is fenced and nothing will
    retry before this deadline" -- which are different bugs with different
    fixes. That ambiguity already cost one wrong diagnosis.

    Read-only, best-effort, and never raises: a diagnostic that can fail the
    run it is explaining is worse than no diagnostic.
    """
    vault = os.environ.get("EXOMEM_VAULT_PATH")
    if not vault:
        return "EXOMEM_VAULT_PATH unset; no server-side state read"
    root = Path(vault)
    facts: list[str] = []
    try:
        from exomem import graph_sync

        facts.append(f"graph_sync.status={graph_sync.status(root)!r}")
        # The status string is lossy in exactly the place that matters.
        # `recovery_required` is three different epoch kinds collapsed into one
        # word -- `pre_floor`, an unacknowledged `coherent`, and a
        # checkpointless `recoverable` -- and only `coherent` is in
        # `REPAIRABLE_EPOCH_KINDS`. So the status alone cannot say whether
        # queued paths are blocked from draining or merely waiting their turn,
        # which is the whole question when the queue is non-empty.
        facts.append(f"epoch_kind={graph_sync.classify_epoch(root).kind!r}")
    except Exception as error:  # noqa: BLE001 - diagnostics never fail the run
        facts.append(f"graph_sync.status unavailable ({error!r})")
    try:
        from exomem import freshness

        # The term that fences every reader. If this is true, no reader sees
        # the graph until something clears it, and the only thing that does is
        # the watcher's periodic reconcile.
        facts.append(f"external_pending={freshness.external_pending(root)!r}")
    except Exception as error:  # noqa: BLE001
        facts.append(f"external_pending unavailable ({error!r})")
    try:
        from exomem import deferred_index

        queued = deferred_index.list_graph_paths(root)
        facts.append(f"graph_queue_depth={len(queued)} sample={queued[:5]!r}")
    except Exception as error:  # noqa: BLE001
        facts.append(f"graph queue unavailable ({error!r})")
    try:
        from exomem import mode

        # Recovery cadence. A deadline shorter than this cannot observe a
        # recovery that only the periodic pass performs, so the interval
        # belongs in the failure text next to the deadline it is compared with.
        facts.append(
            "reconcile_interval_seconds="
            f"{mode.watcher_policy().reconcile_interval_seconds!r}"
        )
    except Exception as error:  # noqa: BLE001
        facts.append(f"reconcile interval unavailable ({error!r})")
    return "; ".join(facts)


async def _await_graph_convergence(
    client,
    *,
    relation_ref: str,
    timeout: float,
) -> None:
    """Poll a relation context until its graph is published, or fail saying so.

    Before the graph came off the write path, the write's own join meant the
    sidecar was always published by the time the next request arrived. It is not
    anymore, so a client reading graph-backed context immediately after a write
    can legitimately observe `available: False` with the rebuild still running.
    Polling is what a real client does; asserting the old timing would be
    asserting a guarantee the design deliberately gave up.

    Deliberately still an assertion, not a tolerance: if the graph never
    converges, this fails. Only the *synchronous* guarantee was given up.
    """
    deadline = time.monotonic() + _GRAPH_CONVERGENCE_SECONDS
    while True:
        context = await _call(
            client,
            "connect_memory",
            {
                "operation": "context",
                "path": relation_ref,
                "depth": 1,
                "traversal_profile": "epistemic",
            },
            timeout,
        )
        graph = context.get("graph")
        if isinstance(graph, dict) and graph.get("available") is True:
            return
        if time.monotonic() >= deadline:
            reason = graph.get("reason") if isinstance(graph, dict) else "no graph in response"
            raise RuntimeError(
                f"graph did not converge within {_GRAPH_CONVERGENCE_SECONDS:.0f}s of "
                f"the write that changed it ({reason!r}) -- a rebuild that never "
                f"lands is exactly what this waits for. Server-side state: "
                f"{_server_side_graph_state()}. Response: {context!r}"
            )
        await asyncio.sleep(_GRAPH_POLL_SECONDS)


async def _assert_relation_contexts(
    client,
    *,
    relation_ref: str,
    timeout: float,
) -> None:
    await _await_graph_convergence(client, relation_ref=relation_ref, timeout=timeout)
    expected = {
        "epistemic": ("science.replicates", "supports"),
        "provenance": ("records.traces_to", "derived_from"),
        "causal": ("systems.triggers", "causes"),
    }
    for profile, (canonical, parent) in expected.items():
        context = await _call(
            client,
            "connect_memory",
            {
                "operation": "context",
                "path": relation_ref,
                "depth": 1,
                "traversal_profile": profile,
            },
            timeout,
        )
        graph = context.get("graph", {})
        if not isinstance(graph, dict):
            raise RuntimeError(
                f"installed context returned no graph for {profile!r}: {context!r}"
            )
        profile_data = graph.get("profile", {})
        if not isinstance(profile_data, dict) or profile_data.get("name") != profile:
            raise RuntimeError(
                f"installed context did not resolve {profile!r} profile: {context!r}"
            )
        edge = next(
            (item for item in graph.get("edges", []) if item.get("relation_type") == canonical),
            None,
        )
        if edge is None:
            raise RuntimeError(f"installed {profile} context omitted {canonical}")
        if edge.get("parent_relation") != parent:
            raise RuntimeError(f"installed edge {canonical} lost parent {parent}")
        if edge.get("registry_status") != "extension":
            raise RuntimeError(f"installed edge {canonical} was not registry-resolved")
        if edge.get("raw_relation") != canonical:
            raise RuntimeError(f"installed edge {canonical} lost raw observation identity")


def _record_snapshot(result: dict[str, Any]) -> str:
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot):
        raise RuntimeError(f"record query omitted exact snapshot hash: {result!r}")
    return snapshot


def _assert_records_rebaseline(inspection: dict[str, Any], history: dict[str, Any]) -> None:
    audit = inspection.get("audit")
    guards = inspection.get("lifecycle_guards")
    if not isinstance(audit, dict) or audit.get("status") != "acknowledged_gap":
        raise RuntimeError("installed Records rebaseline did not retain an acknowledged audit gap")
    discontinuity = audit.get("discontinuity")
    if (
        not isinstance(discontinuity, dict)
        or discontinuity.get("provenance_continuity") is not False
        or not isinstance(discontinuity.get("acknowledged_gap_codes"), list)
        or not discontinuity["acknowledged_gap_codes"]
    ):
        raise RuntimeError("installed Records rebaseline did not retain its discontinuity")
    if (
        not isinstance(guards, dict)
        or not all(
            isinstance(guards.get(key), str) and len(guards[key]) == 64
            for key in ("expected_manifest_hash", "expected_container_hash")
        )
    ):
        raise RuntimeError("installed Records inspection omitted exact lifecycle hashes")
    events = history.get("events") if isinstance(history, dict) else None
    if not isinstance(events, list) or not any(
        isinstance(event, dict)
        and event.get("operation") == "rebaseline"
        and event.get("minimum_reader_version") == 2
        for event in events
    ):
        raise RuntimeError("installed Records rebaseline did not retain reader marker 2")


def _assert_records_presentation_rows(
    unexpanded: dict[str, Any], expanded_pages: list[dict[str, Any]]
) -> None:
    """Assert the installed Records safe projection in both query modes."""
    rows = unexpanded.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("installed Records unexpanded query lost its parent row")
    movements = rows[0].get("movements") if isinstance(rows[0], dict) else None
    if not isinstance(movements, list) or len(movements) != 3:
        raise RuntimeError("installed Records unexpanded query lost safe nested rows")
    serialized_parent = json.dumps(rows, sort_keys=True)
    if "e2e undeclared child sentinel" in serialized_parent:
        raise RuntimeError("installed Records unexpanded query exposed an undeclared child field")
    expanded = [row for page in expanded_pages for row in page.get("rows", [])]
    if [row.get("child_index") for row in expanded if isinstance(row, dict)] != [0, 1, 2]:
        raise RuntimeError(
            "installed Records child pagination duplicated or skipped a child row: "
            f"{expanded_pages!r}"
        )
    if any(
        not isinstance(row, dict)
        or row.get("child_field") != "movements"
        or "movements" in row
        or "private" in row
        for row in expanded
    ):
        raise RuntimeError("installed Records expanded query escaped its safe child projection")
    if (
        expanded_pages[0].get("continuation") is None
        or expanded_pages[-1].get("continuation") is not None
    ):
        raise RuntimeError(
            "installed Records child pagination did not expose a bounded terminal page"
        )


async def _planning_first_session(client, state: dict[str, Any], timeout: float) -> None:
    fixtures = state["planning"]
    software = fixtures["software"]
    nonsoftware = fixtures["nonsoftware"]
    for fixture in (software, nonsoftware):
        created = await _call_mutation(
            client,
            "plan_memory",
            {
                "action": "create",
                "manifest_path": fixture["collection"],
                "manifest_text": fixture["manifest_text"],
                "why": "create installed Planning product collection",
            },
            timeout,
        )
        if created.get("operation") != "create":
            raise RuntimeError(f"installed Planning create returned unexpected data: {created!r}")

    software_collection = software["collection"]
    outcome_id = "b4596ce9-10fd-4856-a26e-89d8be72b0db"
    initiative_id = "3f578f1c-49ba-4c37-a2e7-90a5114e4e23"
    work_item_id = "d8d3f3c1-1601-4b1d-9a5d-694dc9dc2ed3"
    bug_id = "95a51c93-7873-4262-a9ba-bc3ae7ed362b"
    await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "add",
            "collection": software_collection,
            "plan_id": outcome_id,
            "item": {
                "title": "Reliable Planning delivery",
                "kind": "outcome",
                "status": "planned",
                "priority": "high",
                "commitment": "considering",
                "horizon": "year",
            },
            "why": "capture software delivery outcome",
        },
        timeout,
    )
    await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "add",
            "collection": software_collection,
            "plan_id": initiative_id,
            "item": {
                "title": "Planning v1 initiative",
                "kind": "initiative",
                "status": "planned",
                "priority": "high",
                "commitment": "considering",
                "horizon": "quarter",
                "parent": f"exomem://plan/6dd5763e-cabe-4aed-9b9b-fcb0a372e4c5/{outcome_id}",
            },
            "why": "capture software Planning initiative",
        },
        timeout,
    )
    await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "add",
            "collection": software_collection,
            "plan_id": bug_id,
            "item": {"title": "Investigate planning query edge case", "tags": ["bug"]},
            "why": "capture software bug candidate",
        },
        timeout,
    )
    work_item_added = await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "add",
            "collection": software_collection,
            "plan_id": work_item_id,
            "item": {
                "title": "Ship Planning query surface",
                "status": "planned",
                "priority": "high",
                "commitment": "considering",
                "horizon": "quarter",
                "parent": f"exomem://plan/6dd5763e-cabe-4aed-9b9b-fcb0a372e4c5/{initiative_id}",
                "tags": ["feature"],
            },
            "why": "capture software feature candidate",
        },
        timeout,
    )
    snapshot = await _call(
        client,
        "plan_memory",
        {"action": "query", "collection": software_collection, "limit": 20, "lifecycle": "all"},
        timeout,
    )
    item = next(
        (row for row in snapshot.get("rows", []) if row.get("plan_id") == work_item_id), None
    )
    if not isinstance(item, dict) or not isinstance(item.get("item_version"), str):
        raise RuntimeError("installed Planning query did not return the feature item version")
    updated = await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "update",
            "collection": software_collection,
            "plan_id": work_item_id,
            "changes": {
                "execution": [
                    {
                        "kind": "openspec",
                        "ref": "openspec/changes/add-multi-horizon-planning",
                        "label": "Planning v1 contract",
                    },
                    {"kind": "repository", "ref": "exomem"},
                ]
            },
            "expected_container_hash": snapshot["snapshot"],
            "expected_item_version": item["item_version"],
            "why": "link thin software execution pointers",
        },
        timeout,
    )
    if updated.get("operation") != "update":
        raise RuntimeError("installed Planning update did not commit")
    quarter = await _call(
        client,
        "plan_memory",
        {
            "action": "query",
            "collection": software_collection,
            "view": "quarter",
        },
        timeout,
    )
    if not any(row.get("plan_id") == work_item_id for row in quarter.get("rows", [])):
        raise RuntimeError("installed Planning quarter view omitted the feature item")

    home_collection = nonsoftware["collection"]
    home_outcome_id = "aeab0d41-bbfe-4f40-9b04-cd7ab79d1a6c"
    home_initiative_id = "d6e67537-1ea4-4733-b4eb-4ae26f770eee"
    await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "add",
            "collection": home_collection,
            "plan_id": home_outcome_id,
            "item": {
                "title": "Resilient home over five years",
                "kind": "outcome",
                "status": "planned",
                "priority": "medium",
                "commitment": "considering",
                "horizon": "multi-year",
                "domain": "home",
                "progress_evidence": [nonsoftware["records_view"]],
            },
            "why": "capture non-software multi-year outcome",
        },
        timeout,
    )
    await _call_mutation(
        client,
        "plan_memory",
        {
            "action": "add",
            "collection": home_collection,
            "plan_id": home_initiative_id,
            "item": {
                "title": "Improve home exercise space",
                "kind": "initiative",
                "status": "planned",
                "priority": "medium",
                "commitment": "considering",
                "horizon": "year",
                "domain": "home",
                "parent": f"exomem://plan/1b2d04d7-0748-41da-b4f3-cb5313f38cbe/{home_outcome_id}",
            },
            "why": "capture non-software initiative",
        },
        timeout,
    )
    home = await _call(
        client,
        "plan_memory",
        {"action": "query", "collection": home_collection, "view": "multi-year"},
        timeout,
    )
    if not any(row.get("plan_id") == home_outcome_id for row in home.get("rows", [])):
        raise RuntimeError("installed Planning multi-year view omitted the home outcome")
    software["work_item_id"] = work_item_id
    software["work_item_path"] = _single_affected_path(
        work_item_added, operation="plan_memory add"
    )


async def _planning_restart_session(client, state: dict[str, Any], timeout: float) -> None:
    software = state["planning"]["software"]
    query = await _call(
        client,
        "plan_memory",
        {"action": "query", "collection": software["collection"], "limit": 20},
        timeout,
    )
    item = next(
        (row for row in query.get("rows", []) if row.get("plan_id") == software["work_item_id"]),
        None,
    )
    if (
        not isinstance(item, dict)
        or item.get("title") != "Ship Planning query surface (human edit)"
    ):
        raise RuntimeError("direct Planning edit was not visible after restart")
    inspection = await _call(
        client,
        "plan_memory",
        {"action": "inspect", "collection": software["collection"]},
        timeout,
    )
    if inspection.get("audit", {}).get("status") != "gap":
        raise RuntimeError("direct Planning edit did not retain a positive audit gap")


async def _records_first_session(client, state: dict[str, Any], timeout: float) -> None:
    fixture = state["records"]
    collection = fixture["collection"]
    described = await _call(client, "record_memory", {"action": "describe"}, timeout)
    if not isinstance(described, dict):
        raise RuntimeError("installed Records describe did not return a contract")
    validated = await _call(
        client,
        "record_memory",
        {
            "action": "validate",
            "manifest_path": collection,
            "manifest_text": fixture["manifest_text"],
        },
        timeout,
    )
    if not isinstance(validated, dict) or validated.get("valid") is not True:
        raise RuntimeError("installed Records create-mode validation failed")
    created = await _call_mutation(
        client,
        "record_memory",
        {
            "action": "create",
            "manifest_path": collection,
            "manifest_text": fixture["manifest_text"],
            "why": "author installed Records collection",
        },
        timeout,
    )
    if created.get("operation") != "create":
        raise RuntimeError("installed Records create did not commit")
    inspected = await _call(
        client, "record_memory", {"action": "inspect", "collection": collection}, timeout
    )
    if not isinstance(inspected.get("contract"), dict):
        raise RuntimeError("installed Records inspect did not return the created contract")
    before = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": collection,
            "columns": ["occurred_on", "title", "status", "movements"],
            "limit": 20,
        },
        timeout,
    )
    if before.get("rows"):
        raise RuntimeError("installed Records fixture bypassed collection authoring")
    item_key = "77777777-7777-4777-8777-777777777777"
    appended = await _call_mutation(
        client,
        "record_memory",
        {
            "action": "append",
            "collection": collection,
            "item": {
                "occurred_on": "2026-08-04",
                "title": "Pull",
                "status": "completed",
                "movements": [
                    {
                        "movement": "Deadlift",
                        "band": "grey",
                        "repetitions": "22",
                        "private": "e2e undeclared child sentinel",
                    },
                    {"movement": "Row", "band": "blue", "repetitions": "12"},
                    {"movement": "Curl", "band": "white", "repetitions": "8"},
                ],
                "note": "Stopped exactly at the recorded count.",
                "provenance": "Captured by the installed-wheel Records journey.",
            },
            "item_key": item_key,
            "expected_container_hash": _record_snapshot(before),
            "body": "e2e record row-only sentinel",
            "why": "record installed product session",
        },
        timeout,
    )
    if appended.get("operation") != "append":
        raise RuntimeError(f"installed Records append returned unexpected data: {appended!r}")
    affected_paths = appended.get("affected_paths")
    if not isinstance(affected_paths, list) or len(affected_paths) != 1:
        raise RuntimeError("installed Records append did not identify its canonical item")
    fixture["item_path"] = affected_paths[0]
    after_append = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": collection,
            "columns": ["occurred_on", "title", "movements"],
            "limit": 20,
        },
        timeout,
    )
    item = next(
        (row for row in after_append.get("rows", []) if row.get("record_id") == item_key),
        None,
    )
    if not isinstance(item, dict) or not isinstance(item.get("item_version"), str):
        raise RuntimeError("installed Records query did not return the appended item version")
    first_children = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": collection,
            "expand_child": "movements",
            "limit": 2,
        },
        timeout,
    )
    second_children = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": collection,
            "expand_child": "movements",
            "limit": 2,
            "continuation": first_children.get("continuation"),
        },
        timeout,
    )
    _assert_records_presentation_rows(
        after_append,
        [first_children, second_children],
    )
    item_text = (Path(state["vault"]) / fixture["item_path"]).read_text(encoding="utf-8")
    if (
        "<!-- exomem-record-presentation:v1" not in item_text
        or "### Movements" not in item_text
        or "e2e record row-only sentinel" not in item_text
    ):
        raise RuntimeError(
            "installed Records append did not preserve authored prose and managed view"
        )
    updated = await _call_mutation(
        client,
        "record_memory",
        {
            "action": "update",
            "collection": collection,
            "item_key": item_key,
            "changes": {"title": "Push corrected"},
            "expected_container_hash": _record_snapshot(after_append),
            "expected_item_version": item["item_version"],
            "why": "correct installed product session",
        },
        timeout,
    )
    if updated.get("operation") != "update":
        raise RuntimeError(f"installed Records update returned unexpected data: {updated!r}")
    updated_item_text = (Path(state["vault"]) / fixture["item_path"]).read_text(encoding="utf-8")
    if "**Session:** Push corrected" not in updated_item_text:
        raise RuntimeError("installed Records value update did not refresh managed presentation")
    derived = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": collection,
            "view": "completed-sessions",
            "output_format": "markdown",
        },
        timeout,
    )
    if derived.get("derived") is not True or "| occurred_on" not in derived.get("rendered", ""):
        raise RuntimeError("installed Records query did not return a bounded derived Markdown view")
    await _call(
        client, "record_memory", {"action": "inspect", "collection": collection}, timeout
    )
    revision = await _call(
        client,
        "record_memory",
        {
            "action": "validate",
            "collection": collection,
            "manifest_text": fixture["revised_manifest_text"],
        },
        timeout,
    )
    guards = revision.get("lifecycle_guards") if isinstance(revision, dict) else None
    if not isinstance(guards, dict):
        raise RuntimeError("installed Records revision validation did not return guards")
    revised = await _call_mutation(
        client,
        "record_memory",
        {
            "action": "revise",
            "collection": collection,
            "manifest_text": fixture["revised_manifest_text"],
            **guards,
            "why": "confirm installed Records lifecycle",
        },
        timeout,
    )
    if revised.get("operation") != "revise":
        raise RuntimeError("installed Records revise did not commit")
    revised_inspection = await _call(
        client, "record_memory", {"action": "inspect", "collection": collection}, timeout
    )
    if revised_inspection.get("contract", {}).get("title") != "X3 training sessions (revised)":
        raise RuntimeError("installed Records inspection did not retain the revised manifest")
    revised_view = await _call(
        client,
        "record_memory",
        {"action": "query", "collection": collection, "view": "completed-sessions"},
        timeout,
    )
    if not any(
        row.get("record_id") == item_key and row.get("title") == "Push corrected"
        for row in revised_view.get("rows", [])
        if isinstance(row, dict)
    ):
        raise RuntimeError("installed Records saved view lost revised manifest/item parity")
    recalled = await _call(
        client,
        "ask_memory",
        {"query": "e2e record row-only sentinel", "mode": "keyword"},
        timeout,
    )
    hits = recalled.get("hits", []) if isinstance(recalled, dict) else recalled
    if fixture["item_path"] in {hit.get("path") for hit in hits if isinstance(hit, dict)}:
        raise RuntimeError("ordinary recall exposed the raw Records log")
    state["records"].update({"item_key": item_key})


async def _manual_records_session(client, state: dict[str, Any], timeout: float) -> None:
    fixture = state["manual_records"]
    queried = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": fixture["collection"],
            "columns": ["occurred_on", "title", "movements"],
            "limit": 20,
        },
        timeout,
    )
    if not any(row.get("occurred_on") == "2026-08-03" for row in queried.get("rows", [])):
        raise RuntimeError("installed Records query did not return the manual template session")
    inspection = await _call(
        client, "record_memory", {"action": "inspect", "collection": fixture["collection"]}, timeout
    )
    expected_plan = {
        "reference": "exomem://memory/81947000-4c22-46e4-9874-23fed028314b",
        "query": {"filters": {"status": "completed"}, "limit": 24},
    }
    if inspection.get("contract", {}).get("plans") != [expected_plan]:
        raise RuntimeError(
            "installed Records inspection did not round-trip the Planning descriptor"
        )


async def _records_restart_session(client, state: dict[str, Any], timeout: float) -> None:
    fixture = state["records"]
    result = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": fixture["collection"],
            "columns": ["occurred_on", "title", "movements"],
            "limit": 30,
        },
        timeout,
    )
    rows = result.get("rows", [])
    if not any(row.get("occurred_on") == "2026-08-04" for row in rows):
        raise RuntimeError("created Records state did not survive restart")
    if not any(
        row.get("record_id") == fixture["item_key"]
        and row.get("title") == "Push corrected (human edit)"
        for row in rows
    ):
        raise RuntimeError("guarded Records mutation did not survive restart")
    if not any(
        row.get("record_id") == fixture["item_key"]
        and row.get("title") == "Push corrected (human edit)"
        for row in rows
        if isinstance(row, dict)
    ):
        raise RuntimeError("direct Records edit was not visible after restart")
    inspection = await _call(
        client,
        "record_memory",
        {"action": "inspect", "collection": fixture["collection"]},
        timeout,
    )
    history = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": fixture["collection"],
            "view": "completed-sessions",
            "include_agent_history": True,
        },
        timeout,
    )
    _assert_records_rebaseline(inspection, history.get("agent_history", {}))


async def _records_rebaseline_session(client, state: dict[str, Any], timeout: float) -> None:
    fixture = state["records"]
    inspection = await _call(
        client,
        "record_memory",
        {"action": "inspect", "collection": fixture["collection"]},
        timeout,
    )
    audit = inspection.get("audit")
    guards = inspection.get("lifecycle_guards")
    if (
        not isinstance(audit, dict)
        or audit.get("status") != "gap"
        or not isinstance(audit.get("gaps"), list)
        or not audit["gaps"]
        or not isinstance(guards, dict)
    ):
        raise RuntimeError("direct Records edit did not produce an inspectable guarded audit gap")
    presentation = inspection.get("presentation")
    if (
        not isinstance(presentation, dict)
        or presentation.get("counts", {}).get("stale") != 1
        or presentation.get("items", [{}])[0].get("remedy") != "rebaseline_then_refresh"
    ):
        raise RuntimeError("direct Records edit did not produce one actionable stale presentation")
    rebaselined = await _call_mutation(
        client,
        "record_memory",
        {
            "action": "rebaseline",
            "collection": fixture["collection"],
            **guards,
            "acknowledged_gap_codes": audit["gaps"],
            "why": "acknowledge direct installed edit",
        },
        timeout,
    )
    if rebaselined.get("operation") != "rebaseline":
        raise RuntimeError("installed Records rebaseline did not commit")
    repair_inspection = await _call(
        client,
        "record_memory",
        {"action": "inspect", "collection": fixture["collection"]},
        timeout,
    )
    repair_read = await _call(
        client,
        "record_memory",
        {"action": "query", "collection": fixture["collection"], "limit": 20},
        timeout,
    )
    repair_item = next(
        (
            row
            for row in repair_read.get("rows", [])
            if isinstance(row, dict) and row.get("record_id") == fixture["item_key"]
        ),
        None,
    )
    repair_guards = repair_inspection.get("lifecycle_guards")
    if not isinstance(repair_item, dict) or not isinstance(repair_guards, dict):
        raise RuntimeError("installed Records repair did not reacquire exact guards")
    refreshed = await _call_mutation(
        client,
        "record_memory",
        {
            "action": "update",
            "collection": fixture["collection"],
            "item_key": fixture["item_key"],
            "changes": {},
            "expected_container_hash": repair_guards["expected_container_hash"],
            "expected_item_version": repair_item["item_version"],
            "refresh_presentation": True,
            "why": "refresh installed managed presentation after rebaseline",
        },
        timeout,
    )
    if refreshed.get("operation") != "update":
        raise RuntimeError("installed Records presentation refresh did not commit")
    readback = await _call(
        client,
        "record_memory",
        {
            "action": "query",
            "collection": fixture["collection"],
            "view": "completed-sessions",
            "include_agent_history": True,
        },
        timeout,
    )
    if not any(
        row.get("record_id") == fixture["item_key"]
        and row.get("title") == "Push corrected (human edit)"
        for row in readback.get("rows", [])
        if isinstance(row, dict)
    ):
        raise RuntimeError("installed Records rebaseline readback lost the governed item")
    rebaselined_inspection = await _call(
        client,
        "record_memory",
        {"action": "inspect", "collection": fixture["collection"]},
        timeout,
    )
    _assert_records_rebaseline(rebaselined_inspection, readback.get("agent_history", {}))
    if rebaselined_inspection.get("presentation", {}).get("items") != []:
        raise RuntimeError("installed Records presentation remained stale after guarded refresh")


async def _stdio_session(
    executable: Path,
    env: dict[str, str],
    work: Path,
    log_file: Path,
    *,
    timeout: float,
    first_run: bool,
    state: dict[str, Any],
    records_rebaseline: bool = False,
) -> dict[str, Any]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(executable),
        args=["--transport", "stdio"],
        env=env,
        cwd=str(work),
        keep_alive=False,
        log_file=log_file,
    )
    client = Client(transport, timeout=timeout, init_timeout=timeout)
    async with asyncio.timeout(timeout * 12):
        async with client:
            tools = {tool.name for tool in await asyncio.wait_for(client.list_tools(), timeout)}
            required = {
                "capture_source",
                "remember",
                "ask_memory",
                "read_memory",
                "preserve_evidence",
                "replace_memory",
                "edit_memory",
                "connect_memory",
                "review_memory",
                "maintain_memory",
                "schema_memory",
                "record_memory",
                "plan_memory",
            }
            missing = required - tools
            if missing:
                raise RuntimeError(f"installed stdio server missing tools: {sorted(missing)}")

            if first_run:
                relation_proposal = {
                    "schema_version": 1,
                    "extensions": {
                        "science.replicates": {
                            "parent": "supports",
                            "description": "Reports an independent reproduction",
                        },
                        "records.traces_to": {
                            "parent": "derived_from",
                            "description": "Traces a record to its source",
                        },
                        "systems.triggers": {
                            "parent": "causes",
                            "description": "Triggers a system transition",
                        },
                    },
                }
                registry_before = await _call(
                    client,
                    "schema_memory",
                    {"operation": "infer", "subject": "relations"},
                    timeout,
                )
                registry_result = await _call_mutation(
                    client,
                    "schema_memory",
                    {
                        "operation": "infer",
                        "subject": "relations",
                        "save": True,
                        "expected_hash": registry_before["content_hash"],
                        "proposal": relation_proposal,
                    },
                    timeout,
                )
                if (
                    registry_result.get("saved", {}).get("previous_hash")
                    != registry_before["content_hash"]
                ):
                    raise RuntimeError(
                        "installed schema governance did not hash-guard relation registry"
                    )
                source = await _call_mutation(
                    client,
                    "capture_source",
                    {
                        "content": "Project Lantern uses governed references across restarts.",
                        "source_type": "article",
                        "title": "Lantern architecture source",
                        "url": "https://example.com/lantern",
                    },
                    timeout,
                )
                if isinstance(source, dict) and isinstance(source.get("source"), dict):
                    source = source["source"]
                if not isinstance(source, dict) or "path" not in source:
                    raise RuntimeError(f"capture_source returned unexpected data: {source!r}")
                memory = await _call_mutation(
                    client,
                    "remember",
                    {
                        "content": (
                            "# Lantern identity\n\n## Claim\n\n"
                            "Project Lantern requires stable governed identity.\n"
                        ),
                        "title": "Lantern identity",
                        "note_type": "insight",
                        "sources": [source["path"]],
                    },
                    timeout,
                )
                recalled = await _call(
                    client,
                    "ask_memory",
                    {"query": "Lantern stable governed identity", "mode": "keyword"},
                    timeout,
                )
                recalled_hits = recalled.get("hits", []) if isinstance(recalled, dict) else recalled
                if memory["path"] not in {hit["path"] for hit in recalled_hits}:
                    raise RuntimeError("fresh memory was not recalled through stdio MCP")
                read = await _call(
                    client,
                    "read_memory",
                    {"path": memory["ref"], "include_history": True},
                    timeout,
                )
                if read.get("ref") != memory["ref"]:
                    raise RuntimeError("canonical memory reference did not round-trip")
                evidence = await _call_mutation(
                    client,
                    "preserve_evidence",
                    {
                        "scope": "Lantern",
                        "category": "verification",
                        "filename": "restart-proof.txt",
                        "content": "restart persistence verified by the product E2E",
                        "description": "Evidence used by the Lantern conclusion.",
                    },
                    timeout,
                )
                replacement = await _call_mutation(
                    client,
                    "replace_memory",
                    {
                        "old_path": memory["ref"],
                        "content": (
                            "# Lantern identity v2\n\n## Claim\n\n"
                            "Project Lantern uses stable references plus governed evidence.\n"
                        ),
                        "title": "Lantern identity v2",
                        "note_type": "insight",
                        "reason": "add verified evidence and restart persistence",
                        "sources": [source["path"]],
                    },
                    timeout,
                )
                new_ref = replacement.get("new_ref")
                if not new_ref:
                    raise RuntimeError("replacement did not return a canonical new_ref")
                await _call_mutation(
                    client,
                    "edit_memory",
                    {
                        "path": new_ref,
                        "why": "attach proof to the active conclusion",
                        "operation": {
                            "kind": "patch_frontmatter",
                            "field": "evidence",
                            "value": [f"[[{evidence['sidecar_path']}]]"],
                        },
                    },
                    timeout,
                )
                targets: dict[str, dict[str, Any]] = {}
                for key, title in (
                    ("study", "Lantern replication study"),
                    ("record", "Lantern source record"),
                    ("event", "Lantern trigger event"),
                ):
                    targets[key] = await _call_mutation(
                        client,
                        "remember",
                        {
                            "content": (
                                f"# {title}\n\n## Record\n\nCross-file graph target.\n\n"
                                "## Relations\n\n"
                                f"- supports [[{replacement['new_path'].removesuffix('.md')}]]\n"
                            ),
                            "title": title,
                            "note_type": "insight",
                        },
                        timeout,
                    )
                relation_memory = await _call_mutation(
                    client,
                    "remember",
                    {
                        "content": (
                            "# Lantern governed relations\n\n"
                            "## Finding\n"
                            f"- relations: science.replicates: [[{targets['study']['path']}]]\n\n"
                            "The study independently reproduced the result.\n\n"
                            f"- records.traces_to: [[{targets['record']['path']}]]\n"
                            f"- systems.triggers: [[{targets['event']['path']}]]\n"
                        ),
                        "title": "Lantern governed relations",
                        "note_type": "insight",
                    },
                    timeout,
                )
                context = await _call(
                    client,
                    "connect_memory",
                    {"operation": "context", "path": new_ref, "depth": 2},
                    timeout,
                )
                provenance = context["provenance"][0]
                if not provenance["sources"] or not provenance["evidence"]:
                    raise RuntimeError("unified context lost source/evidence provenance")
                evolution = await _call(
                    client,
                    "review_memory",
                    {"mode": "evolution", "query": "Lantern identity", "limit": 10},
                    timeout,
                )
                if not evolution:
                    raise RuntimeError("evolution review returned no lifecycle data")
                reconcile_preview = await _call_maintenance(
                    client,
                    "maintain_memory",
                    {"mode": "reconcile", "dry_run": True},
                    timeout,
                )
                remote_reconcile = await _call(
                    client,
                    "maintain_memory",
                    {
                        "mode": "reconcile",
                        "dry_run": False,
                        "response_detail": "full",
                    },
                    timeout,
                )
                _remote_maintenance_refusal(remote_reconcile)
                await _assert_relation_contexts(
                    client,
                    relation_ref=relation_memory["ref"],
                    timeout=timeout,
                )
                state.update(
                    {
                        "source_ref": source["ref"],
                        "old_ref": memory["ref"],
                        "new_ref": new_ref,
                        "evidence_ref": evidence["ref"],
                        "new_path": replacement["new_path"],
                        "references_status": reconcile_preview.get("references_status"),
                        "relation_ref": relation_memory["ref"],
                        "relation_path": relation_memory["path"],
                        "registry_hash": registry_result["saved"]["content_hash"],
                    }
                )
                await _records_first_session(client, state, timeout)
                await _manual_records_session(client, state, timeout)
                await _planning_first_session(client, state, timeout)
            else:
                operator_reconcile = state.get("operator_reconcile")
                if not isinstance(operator_reconcile, dict):
                    raise RuntimeError("restart session is missing host operator reconcile proof")
                active = await _call(
                    client,
                    "read_memory",
                    {"path": state["new_ref"], "include_history": True},
                    timeout,
                )
                old = await _call(
                    client,
                    "read_memory",
                    {"path": state["old_ref"], "include_history": True},
                    timeout,
                )
                context = await _call(
                    client,
                    "connect_memory",
                    {"operation": "context", "path": state["new_ref"], "depth": 2},
                    timeout,
                )
                if active["path"] != state["new_path"]:
                    raise RuntimeError("active reference resolved to the wrong path after restart")
                if old["frontmatter"].get("status") != "superseded":
                    raise RuntimeError("superseded conclusion lost lifecycle status after restart")
                if not old["frontmatter"].get("superseded_by"):
                    raise RuntimeError("supersession link did not survive restart")
                if not context["history"] or not context["provenance"][0]["evidence"]:
                    raise RuntimeError("context history/provenance did not survive restart")
                await _assert_relation_contexts(
                    client,
                    relation_ref=state["relation_ref"],
                    timeout=timeout,
                )
                if records_rebaseline:
                    await _records_rebaseline_session(client, state, timeout)
                else:
                    await _records_restart_session(client, state, timeout)
                await _manual_records_session(client, state, timeout)
                await _planning_restart_session(client, state, timeout)
                if operator_reconcile.get("graph_sync") == "failed":
                    raise RuntimeError(
                        "host operator reconcile reported graph synchronization failure: "
                        f"{operator_reconcile!r}"
                    )
                # The host reconcile ran before this server session, and the
                # records/manual/planning sessions above write repeatedly.
                # Availability is vault-global, so every one of those clears it
                # until the rebuild republishes -- and rebuilds no longer finish
                # inside the write that caused them. The CLI exit and the
                # sidecar existence checks in `_installed_stdio` prove the
                # deleted indexes were rebuilt; the only honest proof that the
                # later writes also converge is to poll the server doing it.
                await _await_graph_convergence(
                    client,
                    relation_ref=state["relation_ref"],
                    timeout=timeout,
                )
    return state


def _installed_stdio(args: argparse.Namespace) -> int:
    executable = Path(args.executable)
    vault = Path(args.vault)
    work = Path(args.work)
    home = Path(args.home)
    env = _clean_env(home, vault)
    state: dict[str, Any] = {}
    state["vault"] = str(vault)
    state["records"] = _write_records_fixture(vault)
    state["manual_records"] = _write_manual_records_fixture(vault)
    state["planning"] = _write_planning_fixtures(vault)
    _insert_manual_x3_session(vault, state["manual_records"])
    asyncio.run(
        _stdio_session(
            executable,
            env,
            work,
            work / "stdio-first.log",
            timeout=args.request_timeout,
            first_run=True,
            state=state,
        )
    )
    refs_sidecar = vault / "Knowledge Base" / ".refs.sqlite"
    refs_sidecar.unlink(missing_ok=True)
    graph_sidecar = vault / "Knowledge Base" / ".graph.sqlite"
    graph_sidecar.unlink(missing_ok=True)
    record_item = vault / state["records"]["item_path"]
    item_text = record_item.read_text(encoding="utf-8")
    updated_item_text = item_text.replace(
        "title: Push corrected", "title: Push corrected (human edit)", 1
    )
    if updated_item_text == item_text:
        raise RuntimeError("installed Records item did not preserve a direct-editable title")
    record_item.write_text(updated_item_text, encoding="utf-8")
    planning_item = vault / state["planning"]["software"]["work_item_path"]
    planning_item.write_text(
        planning_item.read_text(encoding="utf-8").replace(
            "title: Ship Planning query surface",
            "title: Ship Planning query surface (human edit)",
            1,
        ),
        encoding="utf-8",
    )
    state["operator_reconcile"] = _run_operator_reconcile(
        executable,
        env,
        work,
        timeout=args.request_timeout,
    )
    if not refs_sidecar.exists() or not graph_sidecar.exists():
        raise RuntimeError(
            "host operator reconcile did not rebuild the deleted reference and graph sidecars"
        )
    asyncio.run(
        _stdio_session(
            executable,
            env,
            work,
            work / "stdio-restart.log",
            timeout=args.request_timeout,
            first_run=False,
            records_rebaseline=True,
            state=state,
        )
    )
    asyncio.run(
        _stdio_session(
            executable,
            env,
            work,
            work / "stdio-readback-restart.log",
            timeout=args.request_timeout,
            first_run=False,
            state=state,
        )
    )
    print(json.dumps({"success": True, "transport": "stdio", "state": state}))
    return 0


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode_json(response.read(), url=url, status=response.status)
    except urllib.error.HTTPError as exc:
        return exc.code, _decode_json(exc.read(), url=url, status=exc.code)


def _decode_json(raw: bytes, *, url: str, status: int) -> dict[str, Any]:
    """Parse a response body, reporting the body itself when it is not JSON.

    A bare `JSONDecodeError` here says only "column 1 char 0" and throws the
    server's actual complaint away -- which is the half of the failure worth
    reading, and it is not recoverable afterwards because the harness tears its
    temporary vault, home and logs down on the way out.
    """
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{_redacted_url(url)} returned {status} with a non-JSON body: {text[:2000]!r}"
        ) from error


def _redacted_url(url: str) -> str:
    """Drop any query string so a token in one cannot reach the failure output."""
    return url.split("?", 1)[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses instead of following them (Studio redirect proof)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def _http_get_raw(
    url: str,
    *,
    token: str | None = None,
    timeout: float,
    follow_redirects: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    """GET a non-JSON asset (Studio HTML/JS), returning status, headers, body."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirect)
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, response.read()
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, headers, exc.read()


def _http_post_raw(
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """POST a protocol payload and retain its unparsed response boundary."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response_headers, response.read()
    except urllib.error.HTTPError as exc:
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, response_headers, exc.read()


def _first_review_item(base_url: str, token: str, timeout: float) -> dict[str, Any]:
    """Resolve one current review item (activation queue, then attention)."""
    for mode in ("activation", "attention"):
        status, payload = _http_json(
            f"{base_url}/api/review_memory",
            method="POST",
            body={"mode": mode, "state": "all", "limit": 50},
            token=token,
            timeout=timeout,
        )
        if status != 200 or not payload.get("success"):
            raise RuntimeError(f"review_memory {mode} failed over REST: {status} {payload}")
        for item in payload["data"].get("items", []):
            if item.get("ref") and item.get("fingerprint"):
                return item
    raise RuntimeError("no review item surfaced from the seeded corpus over REST")


def _studio_and_review_checks(base_url: str, *, token: str, timeout: float) -> None:
    """Prove #200 Studio: offline shell, REST data boundary, bounded context, triage."""
    # Packaged Studio shell + versioned asset are served from the installed wheel.
    redirect_status, redirect_headers, _ = _http_get_raw(
        f"{base_url}/studio", timeout=timeout, follow_redirects=False
    )
    if redirect_status != 307 or not redirect_headers.get("location", "").endswith("/studio/"):
        raise RuntimeError(
            "/studio did not redirect to /studio/ "
            f"({redirect_status} {redirect_headers.get('location')})"
        )
    shell_status, shell_headers, shell_body = _http_get_raw(
        f"{base_url}/studio/", timeout=timeout
    )
    if shell_status != 200 or not shell_headers.get("content-type", "").startswith("text/html"):
        raise RuntimeError(f"/studio/ shell not served as HTML: {shell_status}")
    app_asset = re.search(rb"/studio/assets/app\.v\d+\.js", shell_body)
    if b"Exomem Review Studio" not in shell_body or app_asset is None:
        raise RuntimeError("Studio shell body missing packaged markers")
    app_asset_path = app_asset.group(0).decode("ascii")
    asset_status, asset_headers, asset_body = _http_get_raw(
        f"{base_url}{app_asset_path}", timeout=timeout
    )
    if asset_status != 200 or "javascript" not in asset_headers.get("content-type", ""):
        raise RuntimeError(f"Studio {app_asset_path} not served: {asset_status}")
    if b"/studio/assets/api.v1.js" not in asset_body:
        raise RuntimeError(f"Studio {app_asset_path} missing packaged module import")

    # Authenticated data boundary: /api reads are rejected without a bearer key.
    unauth_status, unauth_payload = _http_json(
        f"{base_url}/api/review_item_context",
        method="POST",
        body={"ref": "exomem://review/" + "0" * 24},
        timeout=timeout,
    )
    if unauth_status != 401 or unauth_payload.get("success"):
        raise RuntimeError(
            f"review_item_context served without a bearer key: {unauth_status} {unauth_payload}"
        )

    item = _first_review_item(base_url, token, timeout)
    ref = item["ref"]
    fingerprint = item["fingerprint"]

    # Bounded, deterministic composed context for the seeded review item.
    ctx_status, ctx_payload = _http_json(
        f"{base_url}/api/review_item_context",
        method="POST",
        body={
            "ref": ref,
            "expected_fingerprint": fingerprint,
            "max_body_chars": 200,
            "max_related_pages": 2,
        },
        token=token,
        timeout=timeout,
    )
    if ctx_status != 200 or not ctx_payload.get("success"):
        raise RuntimeError(f"review_item_context failed: {ctx_status} {ctx_payload}")
    context = ctx_payload["data"]
    required_sections = {
        "item",
        "target",
        "related",
        "provenance",
        "graph",
        "history",
        "evolution",
        "availability",
        "truncation",
    }
    missing = required_sections - set(context)
    if missing:
        raise RuntimeError(f"review_item_context omitted sections: {sorted(missing)}")
    if not str(context["target"].get("ref", "")).startswith(("exomem://", "vault://")):
        raise RuntimeError("review_item_context target lost its canonical reference")
    if not isinstance(context["truncation"], list):
        raise RuntimeError("review_item_context truncation is not an explicit list")
    if context["target"].get("body_chars", 0) > 200 and not context["target"].get("body_truncated"):
        raise RuntimeError("review_item_context did not honor the target body bound")
    # Path-specific recorded evolution: present and honest (recorded chain or empty state).
    evolution = context["evolution"]
    availability = context["availability"]
    if "available" not in evolution or not isinstance(evolution.get("timelines"), list):
        raise RuntimeError(
            "review_item_context evolution section is not an honest supersession state"
        )
    if not isinstance(availability.get("evolution"), bool):
        raise RuntimeError("review_item_context availability omitted the evolution flag")

    # Fingerprint-guarded triage round-trip through the REST surface.
    stale_fp = ("1" if fingerprint[0] != "1" else "0") + fingerprint[1:]
    stale_status, stale_payload = _http_json(
        f"{base_url}/api/triage_memory",
        method="POST",
        body={"ref": ref, "action": "dismiss", "expected_fingerprint": stale_fp},
        token=token,
        timeout=timeout,
    )
    if stale_payload.get("success"):
        raise RuntimeError("stale-fingerprint triage was accepted instead of refused")
    stale_message = json.dumps(stale_payload.get("error") or {})
    if "REVIEW_ITEM_CHANGED" not in stale_message:
        raise RuntimeError(
            f"stale-fingerprint triage lacked the changed-item contract: {stale_payload}"
        )
    fresh_status, fresh_payload = _http_json(
        f"{base_url}/api/triage_memory",
        method="POST",
        body={"ref": ref, "action": "dismiss", "expected_fingerprint": fingerprint},
        token=token,
        timeout=timeout,
    )
    if fresh_status != 200 or not fresh_payload.get("success"):
        raise RuntimeError(f"fresh-fingerprint triage failed: {fresh_status} {fresh_payload}")
    if fresh_payload["data"].get("state") != "dismissed":
        raise RuntimeError(f"triage dismiss did not record a dismissed state: {fresh_payload}")


async def _initialize_http_mcp(base_url: str, timeout: float) -> None:
    from fastmcp import Client

    client = Client(f"{base_url}/mcp", timeout=timeout, init_timeout=timeout)
    async with asyncio.timeout(timeout * 3):
        async with client:
            tools = await asyncio.wait_for(client.list_tools(), timeout)
            if not any(tool.name == "bootstrap" for tool in tools):
                raise RuntimeError("HTTP MCP initialization omitted bootstrap")


def _assert_unauthenticated_records_refusal(base_url: str, timeout: float) -> None:
    """Prove the installed remote route refuses a real Records MCP request."""
    metadata_url = f"{base_url}/.well-known/oauth-protected-resource/mcp"
    status, headers, response = _http_post_raw(
        f"{base_url}/mcp",
        body={
            "jsonrpc": "2.0",
            "id": "records-auth-refusal",
            "method": "tools/call",
            "params": {
                "name": "record_memory",
                "arguments": {
                    "action": "inspect",
                    "collection": "Knowledge Base/Records/Health/X3/_collection.md",
                },
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    expected_challenge = f'Bearer resource_metadata="{metadata_url}"'
    if status != 401 or headers.get("www-authenticate") != expected_challenge:
        raise RuntimeError(
            "unauthenticated installed HTTP Records request did not receive the expected "
            f"401 Bearer challenge: {status} {headers.get('www-authenticate')!r}"
        )
    for forbidden in (b"Records/Health/X3", b"Planning", b"rows", b"aggregate"):
        if forbidden in response:
            raise RuntimeError("unauthenticated HTTP Records refusal disclosed collection content")


def _assert_unauthenticated_planning_refusal(base_url: str, timeout: float) -> None:
    """Prove the installed remote route refuses a real Planning MCP request."""
    metadata_url = f"{base_url}/.well-known/oauth-protected-resource/mcp"
    status, headers, response = _http_post_raw(
        f"{base_url}/mcp",
        body={
            "jsonrpc": "2.0",
            "id": "planning-auth-refusal",
            "method": "tools/call",
            "params": {
                "name": "plan_memory",
                "arguments": {
                    "action": "inspect",
                    "collection": "Knowledge Base/Planning/Software/_collection.md",
                },
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    expected_challenge = f'Bearer resource_metadata="{metadata_url}"'
    if status != 401 or headers.get("www-authenticate") != expected_challenge:
        raise RuntimeError(
            "unauthenticated installed HTTP Planning request did not receive the expected "
            f"401 Bearer challenge: {status} {headers.get('www-authenticate')!r}"
        )
    for forbidden in (b"Planning/Software", b"Records", b"rows", b"aggregate"):
        if forbidden in response:
            raise RuntimeError("unauthenticated HTTP Planning refusal disclosed collection content")


def _reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reserve_port_reservations(count: int) -> list[socket.socket]:
    """Hold distinct local ports until their respective server launches."""
    reservations: list[socket.socket] = []
    try:
        for _ in range(count):
            reservation = socket.socket()
            try:
                reservation.bind(("127.0.0.1", 0))
            except OSError:
                reservation.close()
                raise
            reservations.append(reservation)
    except OSError:
        for reservation in reservations:
            reservation.close()
        raise
    return reservations


def _installed_auth_required_records_refusal(args: argparse.Namespace) -> None:
    """Start the installed auth-required HTTP server and verify its ingress boundary."""
    vault = Path(args.vault)
    work = Path(args.work)
    home = Path(args.home)
    port = _reserve_port()
    base_url = f"http://127.0.0.1:{port}"
    env = _clean_env(home, vault)
    env.update(
        {
            "EXOMEM_BASE_URL": base_url,
            "GITHUB_CLIENT_ID": "e2e-github-client-id",
            "GITHUB_CLIENT_SECRET": "e2e-github-client-secret",
            "EXOMEM_GITHUB_USERNAME": "e2e-github-user",
            "EXOMEM_GITHUB_USER_ID": "123456",
            "EXOMEM_JWT_SIGNING_KEY": "e2e-jwt-signing-key",
        }
    )
    server_log = work / "auth-required-http-server.log"
    with server_log.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [
                args.python,
                "-m",
                "exomem",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=work,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        clean_shutdown = False
        try:
            deadline = time.monotonic() + args.request_timeout
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"auth-required HTTP server exited during startup ({proc.returncode}):\n"
                        + server_log.read_text(encoding="utf-8")[-4000:]
                    )
                try:
                    _assert_unauthenticated_records_refusal(base_url, args.request_timeout)
                    _assert_unauthenticated_planning_refusal(base_url, args.request_timeout)
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "auth-required HTTP server did not become ready before timeout"
                        ) from None
                    time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=args.request_timeout)
                    clean_shutdown = True
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            else:
                clean_shutdown = proc.returncode == 0
        if not clean_shutdown:
            raise RuntimeError(
                "auth-required HTTP server did not shut down cleanly:\n"
                + server_log.read_text(encoding="utf-8")[-4000:]
            )


def _installed_http(args: argparse.Namespace) -> int:
    vault = Path(args.vault)
    work = Path(args.work)
    home = Path(args.home)
    env = _clean_env(home, vault)
    env["EXOMEM_REST_API_KEY"] = "e2e-rest-key"
    port = _reserve_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = work / "http-server.log"
    with server_log.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [
                args.python,
                args.http_server,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=work,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        clean_shutdown = False
        try:
            deadline = time.monotonic() + args.request_timeout
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"HTTP server exited during startup ({proc.returncode}):\n"
                        + server_log.read_text(encoding="utf-8")[-4000:]
                    )
                try:
                    status, openapi = _http_json(
                        f"{base_url}/api/openapi.json",
                        timeout=1.0,
                    )
                    if status == 200:
                        break
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("HTTP server did not become ready before timeout")
                time.sleep(0.1)

            serialized_openapi = json.dumps(openapi, sort_keys=True)
            for parameter in ("traversal_profile", "subject", "proposal"):
                if f'"{parameter}"' not in serialized_openapi:
                    raise RuntimeError(f"installed OpenAPI omitted {parameter}")

            wrong_status, _ = _http_json(
                f"{base_url}/api/bootstrap",
                method="POST",
                body={},
                token="wrong",
                timeout=args.request_timeout,
            )
            if wrong_status != 401:
                raise RuntimeError(f"REST wrong-key request returned {wrong_status}, expected 401")
            status, payload = _http_json(
                f"{base_url}/api/bootstrap",
                method="POST",
                body={},
                token="e2e-rest-key",
                timeout=args.request_timeout,
            )
            if status != 200 or not payload.get("success"):
                raise RuntimeError(f"authenticated REST read failed: {status} {payload}")
            status, payload = _http_json(
                f"{base_url}/api/remember",
                method="POST",
                body={
                    "content": "# HTTP lifecycle\n\n## Claim\n\nHTTP writes complete cleanly.\n",
                    "title": "HTTP lifecycle",
                    "note_type": "insight",
                    "status": "draft",
                },
                token="e2e-rest-key",
                timeout=args.request_timeout,
            )
            if status != 200 or not payload.get("success"):
                raise RuntimeError(f"authenticated REST write failed: {status} {payload}")
            _studio_and_review_checks(
                base_url, token="e2e-rest-key", timeout=args.request_timeout
            )
            asyncio.run(_initialize_http_mcp(base_url, args.request_timeout))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=args.request_timeout)
                    clean_shutdown = True
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            else:
                clean_shutdown = proc.returncode == 0
        if not clean_shutdown:
            raise RuntimeError(
                "HTTP server did not shut down cleanly:\n"
                + server_log.read_text(encoding="utf-8")[-4000:]
            )
    _installed_auth_required_records_refusal(args)
    print(json.dumps({"success": True, "transport": "http", "clean_shutdown": True}))
    return 0


_LEASE_VAULT_ID = "e2e-lease-vault"
_LEASE_TOKEN = "e2e-coord-token"
_LEASE_TTL = 4.0


def _lease_replica_env(
    home: Path, vault: Path, *, replica_id: str, coord_url: str, state_dir: Path
) -> dict[str, str]:
    env = _clean_env(home, vault)
    env["EXOMEM_REST_API_KEY"] = "e2e-rest-key"
    env["EXOMEM_WRITER_LEASE_URL"] = coord_url
    env["EXOMEM_WRITER_LEASE_VAULT_ID"] = _LEASE_VAULT_ID
    env["EXOMEM_WRITER_LEASE_REPLICA_ID"] = replica_id
    env["EXOMEM_WRITER_LEASE_TOKEN"] = _LEASE_TOKEN
    env["EXOMEM_WRITER_LEASE_TTL"] = str(_LEASE_TTL)
    env["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(state_dir)
    return env


def _wait_http_ready(
    check, *, proc: subprocess.Popen, deadline: float, log: Path, label: str
) -> None:
    while True:
        if proc.poll() is not None:
            raise RuntimeError(
                f"{label} exited during startup ({proc.returncode}):\n"
                + log.read_text(encoding="utf-8")[-3000:]
            )
        try:
            if check():
                return
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label} did not become ready before timeout")
        time.sleep(0.1)


def _terminate(proc: subprocess.Popen, timeout: float) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _installed_lease(args: argparse.Namespace) -> int:
    """Prove #201: two replicas serialize writes through the lease-wrapped surface."""
    vault = Path(args.vault)
    work = Path(args.work)
    home = Path(args.home)
    timeout = args.request_timeout
    reservations = _reserve_port_reservations(3)
    coordinator_reservation, replica_a_reservation, replica_b_reservation = reservations
    coord_port = int(coordinator_reservation.getsockname()[1])
    port_a = int(replica_a_reservation.getsockname()[1])
    port_b = int(replica_b_reservation.getsockname()[1])
    coord_url = f"http://127.0.0.1:{coord_port}"
    url_a = f"http://127.0.0.1:{port_a}"
    url_b = f"http://127.0.0.1:{port_b}"

    coord_env = _clean_env(home, vault)
    coord_env["EXOMEM_LEASE_COORDINATOR_DB"] = str(work / "writer-leases.sqlite")
    coord_env["EXOMEM_LEASE_COORDINATOR_TOKEN"] = _LEASE_TOKEN
    env_a = _lease_replica_env(
        home, vault, replica_id="replica-a", coord_url=coord_url, state_dir=work / "lease-a"
    )
    env_b = _lease_replica_env(
        home, vault, replica_id="replica-b", coord_url=coord_url, state_dir=work / "lease-b"
    )

    coord_log = work / "coordinator.log"
    log_a = work / "replica-a.log"
    log_b = work / "replica-b.log"
    procs: list[subprocess.Popen] = []
    handles = []
    try:
        coord_handle = coord_log.open("w", encoding="utf-8")
        handles.append(coord_handle)
        coordinator_reservation.close()
        coordinator = subprocess.Popen(
            [
                args.python,
                "-m",
                "exomem.lease_coordinator",
                "--host",
                "127.0.0.1",
                "--port",
                str(coord_port),
                "--database",
                str(work / "writer-leases.sqlite"),
            ],
            cwd=work,
            env=coord_env,
            stdout=coord_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(coordinator)
        lease_status_url = f"{coord_url}/v1/vaults/{_LEASE_VAULT_ID}/lease"
        _wait_http_ready(
            lambda: _http_json(lease_status_url, token=_LEASE_TOKEN, timeout=1.0)[0] == 200,
            proc=coordinator,
            deadline=time.monotonic() + timeout,
            log=coord_log,
            label="lease coordinator",
        )

        for url, env, log, handle_label, reservation in (
            (url_a, env_a, log_a, port_a, replica_a_reservation),
            (url_b, env_b, log_b, port_b, replica_b_reservation),
        ):
            handle = log.open("w", encoding="utf-8")
            handles.append(handle)
            reservation.close()
            replica = subprocess.Popen(
                [args.python, args.http_server, "--host", "127.0.0.1", "--port", str(handle_label)],
                cwd=work,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            procs.append(replica)
            _wait_http_ready(
                lambda u=url: _http_json(f"{u}/api/openapi.json", timeout=1.0)[0] == 200,
                proc=replica,
                deadline=time.monotonic() + timeout,
                log=log,
                label=f"lease replica on {handle_label}",
            )
        replica_a, replica_b = procs[1], procs[2]

        def _remember(url: str, title: str) -> tuple[int, dict[str, Any]]:
            return _http_json(
                f"{url}/api/remember",
                method="POST",
                body={
                    "content": f"# {title}\n\n## Claim\n\nWriter-lease serialization.\n",
                    "title": title,
                    "note_type": "insight",
                    "status": "draft",
                },
                token="e2e-rest-key",
                timeout=timeout,
            )

        # Replica A acquires the lease lazily on its first mutation and becomes writer.
        a_status, a_payload = _remember(url_a, "Lease writer A")
        if a_status != 200 or not a_payload.get("success"):
            raise RuntimeError(f"first writer was refused the lease: {a_status} {a_payload}")

        # Replica B is a readable follower: its write is refused before the leaf runs.
        b_status, b_payload = _remember(url_b, "Lease follower B")
        b_code = (b_payload.get("error") or {}).get("code")
        if b_payload.get("success") or b_code != "WRITER_LEASE_REQUIRED":
            raise RuntimeError(f"follower write was not lease-gated: {b_status} {b_payload}")

        # Coordination status reports the single holder and each replica's role.
        _, status_a = _http_json(
            f"{url_a}/api/coordination_status",
            method="POST",
            body={},
            token="e2e-rest-key",
            timeout=timeout,
        )
        _, status_b = _http_json(
            f"{url_b}/api/coordination_status",
            method="POST",
            body={},
            token="e2e-rest-key",
            timeout=timeout,
        )
        if (
            status_a["data"].get("role") != "writer"
            or status_a["data"].get("holder") != "replica-a"
        ):
            raise RuntimeError(f"writer replica misreported coordination status: {status_a}")
        if (
            status_b["data"].get("role") != "follower"
            or status_b["data"].get("holder") != "replica-a"
        ):
            raise RuntimeError(f"follower replica misreported coordination status: {status_b}")

        # Followers still serve reads while another replica holds the lease.
        read_status, read_payload = _http_json(
            f"{url_b}/api/bootstrap", method="POST", body={}, token="e2e-rest-key", timeout=timeout
        )
        if read_status != 200 or not read_payload.get("success"):
            raise RuntimeError(f"follower could not serve a read: {read_status} {read_payload}")

        # Release/expiry: once A stops, B acquires the lease and takes over writing.
        _terminate(replica_a, timeout)
        deadline = time.monotonic() + _LEASE_TTL * 3 + 5
        takeover: tuple[int, dict[str, Any]] | None = None
        while time.monotonic() < deadline:
            status, payload = _remember(url_b, "Lease takeover B")
            if status == 200 and payload.get("success"):
                takeover = (status, payload)
                break
            time.sleep(0.25)
        if takeover is None:
            raise RuntimeError("second replica never acquired the lease after the writer stopped")

        # The serialized writes left the vault consistent (both reads succeed via B).
        verify_status, verify_payload = _http_json(
            f"{url_b}/api/ask_memory",
            method="POST",
            body={"query": "Lease writer takeover", "mode": "keyword"},
            token="e2e-rest-key",
            timeout=timeout,
        )
        if verify_status != 200 or not verify_payload.get("success"):
            raise RuntimeError(f"post-takeover read failed: {verify_status} {verify_payload}")

        _terminate(replica_b, timeout)
        _terminate(coordinator, timeout)
    finally:
        for proc in procs:
            _terminate(proc, 5)
        for handle in handles:
            handle.close()
        for reservation in reservations:
            reservation.close()
    print(json.dumps({"success": True, "transport": "lease", "takeover": True}))
    return 0


SERVER_LOG_TAIL_CHARS = 20_000


#: Lines worth pulling out of a log whose tail is all polling. Deliberately a
#: coarse net: over-including a few lines costs nothing next to a digest that
#: misses the one line that explains the failure.
CONVERGENCE_DIGEST_PATTERN = re.compile(
    r"graph|drain|rebuild|epoch|defer|barrier|publication|stabiliz|converge",
    re.IGNORECASE,
)

#: Cap the digest so a genuinely chatty log cannot bury the tail beneath it.
CONVERGENCE_DIGEST_LINES = 200


def _print_convergence_digest(relative: Path, text: str) -> None:
    """Pull the convergence story out of the whole log, ahead of the tail.

    The tail alone is the wrong 20 000 characters for the failure it most often
    accompanies. When the graph does not converge, the harness then polls
    `connect_memory` every 540 ms for two minutes, so the tail is a hundred
    percent polling and the writes, fallbacks, drains and rebuild outcomes that
    explain the failure have scrolled out of it. A real run showed exactly
    that: the artifact held the whole story in fifteen lines, and the inline
    tail held none of them.

    Scanning the whole file for those lines costs nothing here -- this runs once
    on a failure -- and it puts the evidence on the failing run's own page,
    which is the difference between reading a failure and rerunning it.
    """
    matches = [line for line in text.splitlines() if CONVERGENCE_DIGEST_PATTERN.search(line)]
    if not matches:
        return
    shown = matches[-CONVERGENCE_DIGEST_LINES:]
    dropped = len(matches) - len(shown)
    elided = "" if not dropped else f", {dropped} earlier match(es) elided"
    print(
        f"--- product-e2e convergence digest: {relative} "
        f"({len(shown)} of {len(matches)} matching line(s){elided}) ---",
        flush=True,
    )
    for line in shown:
        print(line, flush=True)
    print(f"--- end convergence digest: {relative} ---", flush=True)


def _publish_server_logs(root: Path) -> None:
    """Rescue the server's own log before the scratch root is deleted.

    The harness deletes everything it built, the failing server's log included,
    so a red CI run arrives carrying the child's captured stderr and nothing
    else: no fan-out warnings, no deferral reasons, no timings, no way to tell a
    server that refused work from one that never got asked. That is the gap that
    has made graph-convergence failures cost a rerun each to guess at.

    Copy it where the workflow can upload it and print the tail inline, so the
    log is on the failing run's own page rather than a rerun away. Best-effort
    throughout: this runs while an exception is already propagating and must
    never replace the real failure with one of its own.
    """
    source = root / "home" / "logs"
    if not source.is_dir():
        return
    destination = REPO_ROOT / "test-results" / "e2e-logs"
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001 - diagnostics must not mask the failure
        print(f"product-e2e: could not create {destination} ({exc})")
        return
    for log_file in sorted(source.rglob("*")):
        if not log_file.is_file():
            continue
        relative = log_file.relative_to(source)
        try:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(log_file, target)
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # noqa: BLE001 - one unreadable log is not fatal
            print(f"product-e2e: could not publish {relative} ({exc})")
            continue
        _print_convergence_digest(relative, text)
        tail = text[-SERVER_LOG_TAIL_CHARS:]
        elided = "" if len(tail) == len(text) else f" (last {len(tail)} of {len(text)} chars)"
        print(f"--- product-e2e server log: {relative}{elided} ---", flush=True)
        print(tail, flush=True)
        print(f"--- end {relative} ---", flush=True)


@contextlib.contextmanager
def _e2e_workdir(*, keep: bool):
    """The harness's scratch root, optionally retained for a post-mortem.

    Everything this run can be diagnosed from -- the server log, the vault it
    built, the home it isolated -- lives in here and is deleted on the way out,
    including when the run fails. For a deterministic failure that costs a
    re-run; for an intermittent one it can cost many, and it is why an
    `Internal Server Error` from the triage lane had to be chased by rerunning
    until it reproduced rather than by reading the traceback it had already
    written down.
    """
    with scratch_root.scratch_root("exomem-product-e2e-", keep=keep) as path:
        try:
            yield str(path)
        except BaseException:
            _publish_server_logs(path)
            raise


def _orchestrate(args: argparse.Namespace) -> int:
    started = time.monotonic()
    with _e2e_workdir(keep=args.keep_work) as tmp_raw:
        tmp = Path(tmp_raw)
        home = tmp / "home"
        work = tmp / "work"
        vault = tmp / "vault"
        dist = tmp / "dist"
        for path in (home, work, dist):
            path.mkdir()
        env = _clean_env(home, vault)
        print("product-e2e: build installed wheel")
        _run(
            ["uv", "build", "--out-dir", str(dist)],
            env=env,
            cwd=REPO_ROOT,
            timeout=min(args.budget_seconds, 120),
        )
        wheels = sorted(dist.glob("exomem-*.whl"))
        if not wheels:
            raise RuntimeError("uv build produced no wheel")
        venv = tmp / "venv"
        bin_dir = venv / ("Scripts" if WINDOWS else "bin")
        python = bin_dir / ("python.exe" if WINDOWS else "python")
        executable = bin_dir / ("exomem.exe" if WINDOWS else "exomem")
        _run(["uv", "venv", str(venv)], env=env, cwd=work, timeout=30)
        _run(
            ["uv", "pip", "install", "--python", str(python), str(wheels[-1])],
            env=env,
            cwd=work,
            timeout=min(args.budget_seconds, 120),
        )
        _run(
            [str(executable), "init", "--vault", str(vault)],
            env=env,
            cwd=work,
            timeout=30,
        )
        child_timeout = max(30.0, args.request_timeout * 14)
        common = [
            "--vault",
            str(vault),
            "--work",
            str(work),
            "--home",
            str(home),
            "--request-timeout",
            str(args.request_timeout),
        ]
        print("product-e2e: stdio governed lifecycle + restart")
        _run(
            [
                str(python),
                str(Path(__file__).resolve()),
                "--installed-stdio",
                "--executable",
                str(executable),
                *common,
            ],
            env=env,
            cwd=work,
            timeout=child_timeout,
        )
        print("product-e2e: HTTP auth + Studio + review context + triage + shutdown")
        _run(
            [
                str(python),
                str(Path(__file__).resolve()),
                "--installed-http",
                "--python",
                str(python),
                "--http-server",
                str(REPO_ROOT / "scripts" / "e2e_http_server.py"),
                *common,
            ],
            env=env,
            cwd=work,
            timeout=max(30.0, args.request_timeout * 6),
        )
        print("product-e2e: writer-lease coordination (two replicas)")
        _run(
            [
                str(python),
                str(Path(__file__).resolve()),
                "--installed-lease",
                "--python",
                str(python),
                "--http-server",
                str(REPO_ROOT / "scripts" / "e2e_http_server.py"),
                *common,
            ],
            env=env,
            cwd=work,
            timeout=max(60.0, args.request_timeout * 8),
        )
    elapsed = time.monotonic() - started
    if elapsed > args.budget_seconds:
        raise TimeoutError(
            f"product E2E took {elapsed:.1f}s, over {args.budget_seconds:.1f}s budget"
        )
    print(f"product-e2e: PASS ({elapsed:.1f}s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installed-stdio", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--installed-http", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--installed-lease", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="retain the scratch root (vault, home, logs) instead of deleting it on exit",
    )
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--executable", default="")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--http-server", default="")
    parser.add_argument("--vault", default="")
    parser.add_argument("--work", default="")
    parser.add_argument("--home", default="")
    args = parser.parse_args()
    if args.installed_stdio:
        return _installed_stdio(args)
    if args.installed_http:
        return _installed_http(args)
    if args.installed_lease:
        return _installed_lease(args)
    return _orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
