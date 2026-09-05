#!/usr/bin/env python
"""Bounded public-MCP diagnostic for Exomem and Basic Memory's Markdown core.

This deliberately reports two product rows; it is not a competitive score.
Only public MCP calls take part in the timed workflow.  Corpus construction
and each product's ordinary initial indexing happen before that clock starts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MIN_PAGES = 4
MAX_PAGES = 8_000
POLL_INTERVAL_SECONDS = 0.25
BASIC_MEMORY_VERSION = "0.23.2"
BASIC_MEMORY_WHEEL_SHA256 = "a1679a16319d8a7fb9c0486033551a47dedc0fbae7f5da81444eb3c4bf0ccecb"


class AdapterFault(RuntimeError):
    """The harness could not interpret a public MCP response."""


class ProductRefusal(RuntimeError):
    """A product returned a valid public refusal; retain it as an observation."""


@dataclass
class PhaseClock:
    """Keep setup, verified closure, and client teardown on distinct clocks."""

    prepared_at: float | None = None
    timed_at: float | None = None
    closure_at: float | None = None
    teardown_at: float | None = None

    def __post_init__(self) -> None:
        if self.prepared_at is None:
            self.prepared_at = time.perf_counter()

    def start_timing(self) -> None:
        self.timed_at = time.perf_counter()

    def finish_closure(self) -> None:
        self.closure_at = time.perf_counter()

    def finish_teardown(self) -> None:
        self.teardown_at = time.perf_counter()

    def report(self) -> dict[str, float | None]:
        if self.timed_at is None or self.closure_at is None or self.teardown_at is None:
            raise ValueError("phase clock is incomplete")
        assert self.prepared_at is not None
        return {
            "pre_timing_ms": (self.timed_at - self.prepared_at) * 1000.0,
            "wall_to_verified_closure_ms": (self.closure_at - self.timed_at) * 1000.0,
            "teardown_ms": (self.teardown_at - self.closure_at) * 1000.0,
        }


def fixture_pages(pages: int) -> list[tuple[str, str]]:
    """Return a deterministic ordinary-Markdown corpus, including shared anchors."""
    if not MIN_PAGES <= pages <= MAX_PAGES:
        raise ValueError(f"pages must be between {MIN_PAGES} and {MAX_PAGES}")
    named = [
        (
            "active-tracker.md",
            "# Active tracker\n\nState: active\n\n- Current owner: operations\n",
        ),
        (
            "background.md",
            "# Background\n\nThe durable workflow uses ordinary Markdown notes.\n",
        ),
        (
            "stale-link.md",
            "# Stale link\n\nThis old reference is [[Archived Runbook]].\n",
        ),
        (
            "archived-runbook.md",
            "# Archived Runbook\n\nHistorical recovery notes remain available.\n",
        ),
    ]
    for number in range(pages - len(named)):
        named.append(
            (
                f"reference-{number:05d}.md",
                f"# Reference {number}\n\n"
                f"Ordinary background paragraph {number}; links to [[Active tracker]].\n",
            )
        )
    return named


def materialize_fixture(root: Path, *, pages: int) -> dict[str, Any]:
    """Write the pre-timing corpus and return byte-level provenance."""
    records: list[dict[str, Any]] = []
    named_pages: dict[str, str] = {}
    for name, content in fixture_pages(pages):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        raw = path.read_bytes()
        records.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        stem = path.stem.replace("-", "_")
        if stem in {"active_tracker", "background", "stale_link"}:
            named_pages["tracker" if stem == "active_tracker" else stem] = name
    digest = hashlib.sha256(
        "".join(f"{item['path']}:{item['sha256']}\n" for item in records).encode()
    ).hexdigest()
    return {
        "generator": "durable-closure-common-markdown-v1",
        "page_count": pages,
        "pages": records,
        "digest": digest,
        "named_pages": named_pages,
    }


def prepare_roots(*, state: Path, vault: Path) -> None:
    """Refuse live or reused roots before anything can be written."""
    if state.resolve() == vault.resolve():
        raise ValueError("state and vault roots must be distinct")
    for path in (state, vault):
        if path.exists() and any(path.iterdir()):
            raise ValueError(f"{path} must be empty and disposable")
        path.mkdir(parents=True, exist_ok=True)


def _base_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("EXOMEM_", "BASIC_MEMORY_"))
    }


def basic_memory_environment(state: Path, vault: Path) -> dict[str, str]:
    """Fresh Basic Memory home/config; no caller configuration can leak in."""
    del vault  # Both roots are guarded by the caller; Basic Memory owns its home below.
    env = _base_environment()
    env.update(
        {
            "BASIC_MEMORY_HOME": str(state / "home"),
            "BASIC_MEMORY_CONFIG_DIR": str(state / "config"),
            "BASIC_MEMORY_FORCE_LOCAL": "true",
            "BASIC_MEMORY_EXPLICIT_ROUTING": "true",
            "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": "false",
            "BASIC_MEMORY_AUTO_UPDATE": "false",
            "BASIC_MEMORY_NO_PROMOS": "1",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        }
    )
    return env


def exomem_environment(state: Path, vault: Path) -> dict[str, str]:
    env = _base_environment()
    env.update(
        {
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_STATE_ROOT": str(state / "state"),
            "EXOMEM_CONFIG_PATH": str(state / "config.json"),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_EAGER_BOOT": "1",
            "XDG_STATE_HOME": str(state / "xdg"),
            "PYTHONPATH": str(ROOT / "src"),
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        }
    )
    return env


def common_plan(product: str) -> list[dict[str, str]]:
    if product not in {"exomem", "basic_memory"}:
        raise ValueError("product must be exomem or basic_memory")
    return [
        {"operation": "create_completed_chapter", "surface": "public_mcp"},
        {"operation": "edit_tracker", "surface": "public_mcp"},
        {"operation": "correct_stale_link", "surface": "public_mcp"},
        {"operation": "capture_independent_note", "surface": "public_mcp"},
        {"operation": "exact_read_changed_pages", "surface": "public_mcp"},
        {"operation": "search_unique_markers", "surface": "public_mcp"},
    ]


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _decode_payload(result: Any) -> dict[str, Any]:
    """Decode a public MCP envelope, including an ordinary product refusal."""
    if isinstance(result, Mapping):
        structured = result.get("structured_content") or result.get("structuredContent")
        content = result.get("content")
    else:
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        content = getattr(result, "content", None)
    payload = _mapping(structured)
    if payload is not None:
        if "result" in payload:
            nested = payload["result"]
            payload = (
                _mapping(nested)
                if isinstance(nested, Mapping)
                else payload
                if isinstance(nested, list)
                else None
            )
    if payload is None:
        for item in content or ():
            text = item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            payload = _mapping(decoded)
            if payload is not None:
                if "result" in payload:
                    nested = payload["result"]
                    payload = (
                        _mapping(nested)
                        if isinstance(nested, Mapping)
                        else payload
                        if isinstance(nested, list)
                        else None
                    )
                break
    if payload is None:
        if getattr(result, "isError", getattr(result, "is_error", False)):
            for item in content or ():
                text = (
                    item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
                )
                if isinstance(text, str):
                    return {"success": False, "error": {"code": "MCP_TOOL_ERROR", "message": text}}
        raise AdapterFault("malformed MCP result")
    return payload


def decode_result(result: Any) -> dict[str, Any]:
    """Decode a successful public result for callers that require success."""
    payload = _decode_payload(result)
    if payload.get("success") is False or payload.get("status") in {"error", "failed"}:
        raise AdapterFault("failed MCP result")
    return payload


def _json_contains(value: Any, marker: str) -> bool:
    try:
        return marker in json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def exact_marker_present(payload: Mapping[str, Any], marker: str) -> bool:
    return isinstance(payload.get("content"), str) and marker in payload["content"]


def search_marker_present(payload: Mapping[str, Any], marker: str) -> bool:
    results = payload.get("results")
    return isinstance(results, list) and any(_json_contains(item, marker) for item in results)


def call_measurements(calls: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    acks = sorted(float(call["elapsed_ms"]) for call in calls if call.get("ack"))
    if not acks:
        return {"public_call_count": len(calls), "ack_p50_ms": None, "ack_p95_ms": None}

    def percentile(fraction: float) -> float:
        return acks[min(len(acks) - 1, max(0, int(len(acks) * fraction + 0.999) - 1))]

    return {
        "public_call_count": len(calls),
        "ack_p50_ms": percentile(0.50),
        "ack_p95_ms": percentile(0.95),
    }


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_provenance(*, executable: Path, wheel: Path | None, python: Path) -> dict[str, Any]:
    try:
        freeze = subprocess.run(
            [str(python), "-m", "pip", "freeze"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        freeze = []
    return {
        "executable": str(executable),
        "executable_sha256": _sha256(executable),
        "wheel": str(wheel) if wheel else None,
        "wheel_sha256": _sha256(wheel) if wheel else None,
        "dependencies": sorted(freeze),
    }


@dataclass
class PublicClient:
    client: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        ack: bool = False,
        allow_refusal: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            result = await self.client.call_tool_mcp(tool, arguments)
            payload = _decode_payload(result)
        except Exception as error:  # transport and adapter faults invalidate the row.
            raise AdapterFault(
                f"{tool} public MCP call failed: {type(error).__name__}: {error}"
            ) from error
        self.calls.append(
            {"tool": tool, "elapsed_ms": (time.perf_counter() - started) * 1000.0, "ack": ack}
        )
        if not allow_refusal and (
            payload.get("success") is False or payload.get("status") in {"error", "failed"}
        ):
            raise ProductRefusal(f"{tool} returned a failed public result: {payload.get('error')}")
        return payload


async def _await_search(
    client: PublicClient, *, product: str, marker: str, timeout: float
) -> tuple[bool, float, list[float]]:
    started = time.perf_counter()
    waits: list[float] = []
    while True:
        if product == "basic_memory":
            payload = await client.call(
                "search_notes",
                {
                    "query": marker,
                    "search_type": "text",
                    "project": "main",
                    "output_format": "json",
                },
            )
        else:
            payload = await client.call(
                "ask_memory",
                {"query": marker, "mode": "keyword", "graph": False, "rerank": False, "limit": 10},
                allow_refusal=True,
            )
        if search_marker_present(payload, marker) or _json_contains(payload, marker):
            return True, (time.perf_counter() - started) * 1000.0, waits
        if time.perf_counter() - started >= timeout:
            return False, (time.perf_counter() - started) * 1000.0, waits
        waits.append(POLL_INTERVAL_SECONDS * 1000.0)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _await_initial_index(client: PublicClient, *, product: str, timeout: float) -> None:
    """Wait for the native public retrieval path before starting the workflow clock."""
    deadline = time.perf_counter() + timeout
    while True:
        if product == "basic_memory":
            payload = await client.call(
                "search_notes",
                {
                    "query": "durable-common-warmup",
                    "search_type": "text",
                    "project": "main",
                    "output_format": "json",
                },
                allow_refusal=True,
            )
        else:
            payload = await client.call(
                "ask_memory",
                {
                    "query": "durable-common-warmup",
                    "mode": "keyword",
                    "graph": False,
                    "rerank": False,
                    "limit": 1,
                },
                allow_refusal=True,
            )
        if payload.get("success") is not False:
            return
        if time.perf_counter() >= deadline:
            raise AdapterFault("initial public search did not converge before timeout")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _permit_refusal_envelopes(client: Any) -> None:
    """Keep a valid Exomem warming/refusal envelope visible to this diagnostic."""
    for owner in (client, getattr(client, "session", None)):
        schemas = getattr(owner, "_tool_output_schemas", None)
        if isinstance(schemas, dict):
            for name in tuple(schemas):
                schemas[name] = None


async def _run_basic_memory(
    client: PublicClient, marker: str, fixture: Mapping[str, Any], timeout: float
) -> dict[str, Any]:
    changed = ["Completed chapter", "Active tracker", "Stale link", "Independent capture"]
    await client.call(
        "write_note",
        {
            "title": changed[0],
            "content": f"# Completed chapter\n\nStatus: completed\n\n{marker}-chapter\n",
            "directory": "notes",
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    await client.call(
        "edit_note",
        {
            "identifier": changed[1],
            "operation": "append",
            "content": f"\n{marker}-tracker\n",
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    await client.call(
        "edit_note",
        {
            "identifier": changed[2],
            "operation": "find_replace",
            "find_text": "[[Archived Runbook]]",
            "content": "Archived runbook retired",
            "expected_replacements": 1,
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    await client.call(
        "write_note",
        {
            "title": changed[3],
            "content": f"# Independent capture\n\n{marker}-capture\n",
            "directory": "notes",
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    reads = [
        await client.call(
            "read_note", {"identifier": title, "project": "main", "output_format": "json"}
        )
        for title in changed
    ]
    searches = [
        await _await_search(
            client, product="basic_memory", marker=f"{marker}-{suffix}", timeout=timeout
        )
        for suffix in ("chapter", "tracker", "capture")
    ]
    return {"changed": changed, "reads": reads, "searches": searches, "fixture": fixture}


async def _run_exomem(
    client: PublicClient, marker: str, fixture: Mapping[str, Any], timeout: float
) -> dict[str, Any]:
    # Existing raw fixture pages are intentionally ordinary Markdown, avoiding richer authoring.
    tracker = "Knowledge Base/Reference/" + str(fixture["named_pages"]["tracker"])
    stale = "Knowledge Base/Reference/" + str(fixture["named_pages"]["stale_link"])
    chapter_content = f"# Completed chapter\n\nStatus: completed\n\n## Observations\n\n- [finding] {marker}-chapter\n"
    remembered = await client.call(
        "remember",
        {
            "title": "Completed chapter",
            "note_type": "insight",
            "content": chapter_content,
            "response_detail": "full",
            "validate_only": True,
        },
    )
    commit = await client.call(
        "remember",
        _remember_commit_arguments(remembered, title="Completed chapter", content=chapter_content),
        ack=True,
    )
    completed = str(commit.get("path") or "")
    if not completed:
        raise AdapterFault("remember result omitted created path")
    await client.call(
        "edit_memory",
        {
            "path": tracker,
            "why": "record common-subset tracker state",
            "operation": {
                "kind": "edit_section",
                "heading": "# Active tracker",
                "new_string": marker + "-tracker",
                "section_position": "append",
            },
        },
        ack=True,
    )
    await client.call(
        "edit_memory",
        {
            "path": stale,
            "why": "correct common-subset stale link",
            "operation": {
                "kind": "replace_string",
                "old_string": "[[Archived Runbook]]",
                "new_string": "Archived runbook retired",
                "replace_all": False,
            },
        },
        ack=True,
    )
    capture_content = f"# Independent capture\n\n## Observations\n\n- [finding] {marker}-capture\n"
    capture = await client.call(
        "remember",
        {
            "title": "Independent capture",
            "note_type": "insight",
            "content": capture_content,
            "response_detail": "full",
            "validate_only": True,
        },
    )
    captured = await client.call(
        "remember",
        _remember_commit_arguments(capture, title="Independent capture", content=capture_content),
        ack=True,
    )
    capture_path = str(captured.get("path") or "")
    reads = [
        await client.call("read_memory", {"path": path})
        for path in (completed, tracker, stale, capture_path)
    ]
    searches = [
        await _await_search(client, product="exomem", marker=f"{marker}-{suffix}", timeout=timeout)
        for suffix in ("chapter", "tracker", "capture")
    ]
    return {
        "changed": [completed, tracker, stale, capture_path],
        "reads": reads,
        "searches": searches,
        "fixture": fixture,
    }


def _remember_commit_arguments(
    validation: Mapping[str, Any], *, title: str, content: str
) -> dict[str, Any]:
    """Replay the accepted Exomem draft, adding a review disposition only when required."""
    arguments: dict[str, Any] = {
        "title": title,
        "note_type": "insight",
        "content": content,
        "response_detail": "full",
        "draft_id": validation.get("draft_id"),
        "draft_hash": validation.get("draft_hash"),
        "draft_token": validation.get("draft_token"),
    }
    if validation.get("reviewed_none_required"):
        arguments.update(
            {
                "relation_disposition": "reviewed_none",
                "relation_review_hash": validation.get("relation_review_hash")
                or validation.get("draft_hash"),
                "relation_review_reason": "No relation is asserted by this isolated common-subset diagnostic.",
            }
        )
    return arguments


async def run_product(
    *,
    product: str,
    state: Path,
    vault: Path,
    pages: int,
    timeout: float,
    python: Path,
    basic_memory: Path,
    wheel: Path | None,
) -> dict[str, Any]:
    """Run one warm persistent public-MCP row, leaving product outcomes observable."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    prepare_roots(state=state, vault=vault)
    clock = PhaseClock()
    if product == "basic_memory":
        env = basic_memory_environment(state, vault)
        corpus_root = Path(env["BASIC_MEMORY_HOME"])
        fixture = materialize_fixture(corpus_root, pages=pages)
        command, args, executable = (
            str(basic_memory),
            ["mcp", "--transport", "stdio", "--project", "main"],
            basic_memory,
        )
        runtime_python = basic_memory.parent.parent / "bin" / "python"
    else:
        env = exomem_environment(state, vault)
        schema = vault / "Knowledge Base" / "_Schema"
        shutil.copytree(ROOT / "src" / "exomem" / "_scaffold" / "_Schema", schema)
        corpus_root = vault / "Knowledge Base" / "Reference"
        fixture = materialize_fixture(corpus_root, pages=pages)
        command, args, executable = str(python), ["-m", "exomem", "--transport", "stdio"], python
        runtime_python = python
    transport = StdioTransport(
        command=command,
        args=args,
        env=env,
        cwd=str(ROOT),
        keep_alive=False,
        log_file=state / "stdio.log",
    )
    client = Client(transport, timeout=timeout, init_timeout=timeout)
    marker = f"common-subset-{uuid.uuid4().hex}"
    row: dict[str, Any] = {
        "product": product,
        "status": "invalid",
        "reason": None,
        "plan": common_plan(product),
        "configuration": {
            key: value for key, value in env.items() if key.startswith(("EXOMEM_", "BASIC_MEMORY_"))
        },
        "corpus": fixture,
        "runtime": runtime_provenance(
            executable=executable,
            wheel=wheel if product == "basic_memory" else None,
            python=runtime_python,
        ),
    }
    try:
        async with client:
            tools = {tool.name for tool in await client.list_tools()}
            _permit_refusal_envelopes(client)
            required = (
                {"write_note", "edit_note", "read_note", "search_notes"}
                if product == "basic_memory"
                else {"remember", "edit_memory", "read_memory", "ask_memory"}
            )
            missing = sorted(required - tools)
            if missing:
                raise AdapterFault(f"registered public MCP tools missing: {missing}")
            # Discovery/initial indexing are pre-timing setup, never an inline reindex.
            await _await_initial_index(PublicClient(client), product=product, timeout=timeout)
            clock.start_timing()
            public = PublicClient(client)
            result = await (
                _run_basic_memory(public, marker, fixture, timeout)
                if product == "basic_memory"
                else _run_exomem(public, marker, fixture, timeout)
            )
            stale_path = result["changed"][2]
            exact = all(
                _json_contains(read, marker)
                if name != stale_path
                else "[[Archived Runbook]]" not in json.dumps(read)
                for name, read in zip(result["changed"], result["reads"], strict=True)
            )
            converged = all(found for found, _, _ in result["searches"])
            clock.finish_closure()
            row.update(
                {
                    "status": "pass" if exact and converged else "observed_failure",
                    "public_calls": public.calls,
                    "measurements": call_measurements(public.calls),
                    "verification": {
                        "exact_read_your_write": exact,
                        "search_converged": converged,
                        "searches": [
                            {"found": found, "convergence_ms": elapsed, "waits_ms": waits}
                            for found, elapsed, waits in result["searches"]
                        ],
                    },
                    "markers": [
                        f"{marker}-{suffix}" for suffix in ("chapter", "tracker", "capture")
                    ],
                }
            )
    except ProductRefusal as error:
        if clock.timed_at is None:
            clock.start_timing()
        clock.finish_closure()
        row.update({"status": "observed_failure", "reason": str(error)})
    except AdapterFault as error:
        if clock.timed_at is None:
            clock.start_timing()
        clock.finish_closure()
        row.update({"status": "invalid", "reason": str(error)})
    finally:
        clock.finish_teardown()
        row["phases"] = clock.report()
    return row


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--product", choices=("exomem", "basic_memory", "both"), default="both")
    parser.add_argument(
        "--state", type=Path, required=True, help="empty disposable diagnostic state root"
    )
    parser.add_argument(
        "--vault", type=Path, required=True, help="empty disposable diagnostic vault root"
    )
    parser.add_argument("--pages", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--python", type=Path, default=Path(sys.executable), help="explicit Exomem interpreter"
    )
    parser.add_argument("--basic-memory-executable", type=Path, required=True)
    parser.add_argument("--basic-memory-wheel", type=Path, default=None)
    args = parser.parse_args(argv)
    selected = ("exomem", "basic_memory") if args.product == "both" else (args.product,)
    if args.pages < MIN_PAGES or args.pages > MAX_PAGES:
        parser.error(f"--pages must be between {MIN_PAGES} and {MAX_PAGES}")
    if (
        args.state.exists()
        and any(args.state.iterdir())
        or args.vault.exists()
        and any(args.vault.iterdir())
    ):
        parser.error("--state and --vault must both be empty disposable roots")
    rows = []
    for product in selected:
        rows.append(
            asyncio.run(
                run_product(
                    product=product,
                    state=args.state / product,
                    vault=args.vault / product,
                    pages=args.pages,
                    timeout=args.timeout,
                    python=args.python,
                    basic_memory=args.basic_memory_executable,
                    wheel=args.basic_memory_wheel,
                )
            )
        )
    print(
        json.dumps(
            {"diagnostic": "durable-closure-common-subset", "rows": rows}, indent=2, sort_keys=True
        )
    )
    return 0 if all(row["status"] != "invalid" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
