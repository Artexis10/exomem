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
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MIN_PAGES = 4
MAX_PAGES = 8_000
POLL_INTERVAL_SECONDS = 0.25
WARM_SENTINEL = "durable-closure-common-warm-sentinel"
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
        assert self.prepared_at is not None
        return {
            "pre_timing_ms": (self.timed_at - self.prepared_at) * 1000.0
            if self.timed_at is not None
            else None,
            "wall_to_verified_closure_ms": (self.closure_at - self.timed_at) * 1000.0
            if self.closure_at is not None and self.timed_at is not None
            else None,
            "teardown_ms": (self.teardown_at - self.closure_at) * 1000.0
            if self.teardown_at is not None and self.closure_at is not None
            else None,
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
            f"# Background\n\nThe durable workflow uses ordinary Markdown notes. {WARM_SENTINEL}\n",
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
    named = [
        (
            name,
            body
            + "\n<!-- deterministic fixture padding\n"
            + ("x" * (index + 1) * 1024)
            + "\n-->"
            + ("" if name == "stale-link.md" else "\n"),
        )
        for index, (name, body) in enumerate(named)
    ]
    for number in range(pages - len(named)):
        variants = (
            f"- constraint: bounded retries {number}\n",
            f"| field | value |\n| --- | --- |\n| batch | {number} |\n",
            f"```text\nreference-{number}\n```\n",
        )
        named.append(
            (
                f"reference-{number:05d}.md",
                f"# Reference {number}\n\n"
                f"Ordinary background paragraph {number}; links to [[Active tracker]].\n"
                f"{variants[number % len(variants)]}\n<!-- deterministic fixture padding\n"
                + ("x" * ((number % 4) + 5) * 1024)
                + "\n-->",
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
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(state / "leases"),
            "EXOMEM_LOG_DIR": str(state / "logs"),
            "EXOMEM_CALL_LEDGER_DIR": str(state / "ledger"),
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


def common_markdown_payload(marker: str) -> dict[str, str]:
    """The literal Markdown bodies shared by the two public adapters."""
    return {
        "chapter": (
            "# Completed chapter\n\nStatus: completed\n\n## Observations\n\n"
            f"- [finding] {marker}-chapter\n"
        ),
        "tracker_append": f"## Observations\n\n- [finding] {marker}-tracker\n",
        "capture": (f"# Independent capture\n\n## Observations\n\n- [finding] {marker}-capture\n"),
        "stale_replacement": "Archived runbook retired",
    }


def expected_changed_bodies(fixture: Mapping[str, Any], marker: str) -> list[str]:
    """Expected normalized Markdown after the shared operations, before product frontmatter."""
    source = dict(fixture_pages(int(fixture["page_count"])))
    markdown = common_markdown_payload(marker)
    tracker = source[str(fixture["named_pages"]["tracker"])] + markdown["tracker_append"]
    stale = stale_replacement_body(fixture, markdown)
    return [markdown["chapter"], tracker, stale, markdown["capture"]]


def stale_replacement_body(fixture: Mapping[str, Any], markdown: Mapping[str, str]) -> str:
    """Apply the common stale-link correction with its explicit final newline."""
    source = dict(fixture_pages(int(fixture["page_count"])))
    return (
        source[str(fixture["named_pages"]["stale_link"])].replace(
            "[[Archived Runbook]]", markdown["stale_replacement"], 1
        )
        + "\n"
    )


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _decode_payload(result: Any) -> dict[str, Any]:
    """Decode a public MCP envelope, including an ordinary product refusal."""
    if isinstance(result, Mapping):
        is_error = bool(result.get("is_error", result.get("isError", False)))
        structured = result.get("structured_content") or result.get("structuredContent")
        content = result.get("content")
    else:
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        content = getattr(result, "content", None)
    if is_error:
        message = next(
            (
                text
                for item in content or ()
                if isinstance(
                    text := (
                        item.get("text")
                        if isinstance(item, Mapping)
                        else getattr(item, "text", None)
                    ),
                    str,
                )
            ),
            "MCP tool reported an error",
        )
        return {"success": False, "error": {"code": "MCP_TOOL_ERROR", "message": message}}
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
        raise AdapterFault("malformed MCP result")
    return payload


def decode_result(result: Any) -> dict[str, Any]:
    """Decode a successful public result for callers that require success."""
    payload = _decode_payload(result)
    if result_classification(payload) != "ok":
        raise AdapterFault("failed MCP result")
    return payload


def _json_contains(value: Any, marker: str) -> bool:
    try:
        return marker in json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def exact_marker_present(payload: Mapping[str, Any], marker: str) -> bool:
    return read_body_has_markers(payload, [marker])


def _read_body(payload: Mapping[str, Any]) -> str | None:
    for key in ("content", "body"):
        body = payload.get(key)
        if isinstance(body, str):
            return body
    return None


def _normalized_body(body: str) -> str:
    body = body.removeprefix("\n")
    if body.startswith("---\n"):
        _, separator, remainder = body[4:].partition("\n---\n")
        if separator:
            return remainder.removeprefix("\n")
    return body


def read_body_has_markers(payload: Mapping[str, Any], markers: Sequence[str]) -> bool:
    body = _read_body(payload)
    return body is not None and all(marker in _normalized_body(body) for marker in markers)


def read_body_equals(payload: Mapping[str, Any], expected: str) -> bool:
    body = _read_body(payload)
    return body is not None and _normalized_body(body) == expected


def body_proof(payload: Mapping[str, Any], expected: str) -> dict[str, Any]:
    body = _read_body(payload)
    normalized = _normalized_body(body) if body is not None else None
    proof: dict[str, Any] = {
        "matches": normalized == expected,
        "actual_bytes": len(normalized.encode()) if normalized is not None else None,
        "expected_bytes": len(expected.encode()),
        "actual_sha256": hashlib.sha256(normalized.encode()).hexdigest() if normalized else None,
        "expected_sha256": hashlib.sha256(expected.encode()).hexdigest(),
    }
    if normalized is not None and normalized != expected:
        mismatch = next(
            (
                index
                for index, (actual, wanted) in enumerate(zip(normalized, expected, strict=False))
                if actual != wanted
            ),
            min(len(normalized), len(expected)),
        )
        proof["first_mismatch"] = {
            "offset": mismatch,
            "actual": normalized[mismatch : mismatch + 80],
            "expected": expected[mismatch : mismatch + 80],
        }
    return proof


def stale_replacement_verified(payload: Mapping[str, Any], *, old: str, replacement: str) -> bool:
    body = _read_body(payload)
    return (
        body is not None
        and old not in _normalized_body(body)
        and replacement in _normalized_body(body)
    )


def search_marker_present(payload: Mapping[str, Any], marker: str) -> bool:
    results = payload.get("results") or payload.get("hits") or payload.get("result")
    if not isinstance(results, list):
        return False
    return any(
        isinstance(hit, Mapping)
        and any(
            marker in hit[key]
            for key in ("body", "content", "excerpt", "snippet", "summary")
            if isinstance(hit.get(key), str)
        )
        for hit in results
    )


def result_classification(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if (
        payload.get("success") is False
        or payload.get("ok") is False
        or isinstance(error, Mapping)
        or (isinstance(error, str) and bool(error.strip()))
    ):
        return "refused"
    if str(payload.get("outcome") or "").lower() in {"error", "failed", "refused", "rejected"}:
        return "refused"
    if str(payload.get("status") or "").lower() in {"error", "failed", "refused", "rejected"}:
        return "refused"
    return "ok"


def call_measurements(calls: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None | str]:
    acks = sorted(
        float(call["elapsed_ms"])
        for call in calls
        if call.get("ack") and call.get("classification", "ok") == "ok"
    )
    measurements: dict[str, float | int | None | str] = {
        "public_call_count": len(calls),
        "ack_p50_ms": None,
        "ack_p95_ms": None,
        "shared_server_ms": None,
        "shared_server_reason": "not exposed by the public MCP protocol",
        "connector_ms": None,
        "connector_reason": "not exposed by the public MCP protocol",
    }
    if not acks:
        return measurements

    def percentile(fraction: float) -> float:
        return acks[min(len(acks) - 1, max(0, int(len(acks) * fraction + 0.999) - 1))]

    measurements.update({"ack_p50_ms": percentile(0.50), "ack_p95_ms": percentile(0.95)})
    return measurements


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digest(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_identity(installed: Mapping[str, Any]) -> dict[str, Any]:
    package_root = Path(str(installed.get("package_root") or ""))
    source_digest = _source_digest(package_root)
    identity: dict[str, Any] = {
        "runtime_pythonpath_root": installed.get("runtime_pythonpath_root"),
        "package_root": str(package_root) if package_root.is_dir() else None,
        "revision": None,
        "tree": None,
        "source_digest": source_digest,
        "dirty_source_digest": source_digest,
        "dirty": None,
    }
    if not package_root.is_dir():
        identity["reason"] = "runtime package root unavailable"
        return identity
    try:
        repository = subprocess.run(
            ["git", "-C", str(package_root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        relative = str(package_root.relative_to(repository))
        identity.update(
            {
                "revision": subprocess.run(
                    ["git", "-C", repository, "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip(),
                "tree": subprocess.run(
                    ["git", "-C", repository, "rev-parse", "HEAD^{tree}"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip(),
                "dirty": bool(
                    subprocess.run(
                        ["git", "-C", repository, "status", "--porcelain", "--", relative],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    ).stdout.strip()
                ),
            }
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        identity["reason"] = type(error).__name__
    return identity


def runtime_provenance(
    *,
    executable: Path,
    wheel: Path | None,
    python: Path,
    package: str,
    expected_version: str | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    probe = (
        "import hashlib, importlib.metadata as m, json, os, pathlib, "
        f"{package.replace('-', '_')} as p; "
        "path=pathlib.Path(p.__file__); "
        "deps=sorted(f'{d.metadata[\"Name\"]}=={d.version}' for d in m.distributions() "
        'if d.metadata.get("Name")); '
        "print(json.dumps({'version':m.version('"
        + package
        + "'),'module_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),"
        "'dependencies':deps,'package_root':str(path.parent),"
        "'runtime_pythonpath_root':os.environ.get('PYTHONPATH')}))"
    )
    try:
        installed = json.loads(
            subprocess.run(
                [str(python), "-c", probe],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=dict(environment) if environment is not None else None,
            ).stdout
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        installed = {"status": "unavailable", "reason": type(error).__name__}
    inventory = (
        {"status": "ok", "entries": installed.pop("dependencies")}
        if isinstance(installed.get("dependencies"), list)
        else {"status": "unavailable", "reason": "metadata_probe_failed", "entries": []}
    )
    provenance = {
        "executable": str(executable),
        "executable_sha256": _sha256(executable),
        "wheel": str(wheel) if wheel else None,
        "wheel_sha256": _sha256(wheel) if wheel else None,
        "wheel_matches_pinned_digest": _sha256(wheel) == BASIC_MEMORY_WHEEL_SHA256
        if wheel
        else None,
        "installed": installed,
        "installed_version_matches_expected": installed.get("version") == expected_version
        if expected_version and isinstance(installed, Mapping)
        else None,
        "dependency_inventory": inventory,
    }
    if package == "exomem":
        provenance["source_identity"] = _source_identity(installed)
    return provenance


def basic_memory_provenance_is_pinned(provenance: Mapping[str, Any]) -> bool:
    inventory = provenance.get("dependency_inventory")
    return (
        provenance.get("wheel_matches_pinned_digest") is True
        and provenance.get("installed_version_matches_expected") is True
        and isinstance(inventory, Mapping)
        and inventory.get("status") == "ok"
    )


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
            self.calls.append(
                {
                    "tool": tool,
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "ack": ack,
                    "classification": "invalid",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            raise AdapterFault(
                f"{tool} public MCP call failed: {type(error).__name__}: {error}"
            ) from error
        classification = result_classification(payload)
        self.calls.append(
            {
                "tool": tool,
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                "ack": ack,
                "classification": classification,
            }
        )
        if not allow_refusal and classification == "refused":
            raise ProductRefusal(f"{tool} returned a failed public result: {payload.get('error')}")
        return payload


async def _await_search(
    client: PublicClient, *, product: str, marker: str, timeout: float
) -> tuple[bool, float, list[float], dict[str, Any]]:
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
                {
                    "query": marker,
                    "mode": "keyword",
                    "detail": "full",
                    "graph": False,
                    "rerank": False,
                    "limit": 10,
                },
                allow_refusal=True,
            )
        proof = _search_proof(payload)
        if search_marker_present(payload, marker):
            return True, (time.perf_counter() - started) * 1000.0, waits, proof
        if time.perf_counter() - started >= timeout:
            return False, (time.perf_counter() - started) * 1000.0, waits, proof
        waits.append(POLL_INTERVAL_SECONDS * 1000.0)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _search_proof(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize a final search response into enough evidence to diagnose a miss."""
    container = (
        "results"
        if isinstance(payload.get("results"), list)
        else "hits"
        if isinstance(payload.get("hits"), list)
        else "result"
    )
    hits = payload.get(container)
    return {
        "classification": result_classification(payload),
        "container": container if isinstance(hits, list) else None,
        "hit_count": len(hits) if isinstance(hits, list) else None,
        "top_level_keys": sorted(str(key) for key in payload)[:16],
    }


def _membership_digest(values: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _contained_path(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise AdapterFault("indexed store escaped the disposable state root")
    return resolved_candidate


def _indexed_store(product: str, state: Path) -> Path | None:
    if product == "basic_memory":
        candidate = state / "config" / "memory.db"
        return _contained_path(state, candidate) if candidate.is_file() else None
    root = state / "state"
    if not root.is_dir():
        return None
    candidates = [
        _contained_path(root, path) for path in root.glob("*/.lexical.sqlite") if path.is_file()
    ]
    if len(candidates) > 1:
        raise AdapterFault("ambiguous Exomem indexed stores under the disposable state root")
    return candidates[0] if candidates else None


def _schema_identity(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'virtual table') "
        f"AND name IN ({','.join('?' for _ in tables)}) ORDER BY name",
        tuple(tables),
    ).fetchall()
    identity = {str(name): str(sql or "") for name, sql in rows}
    return {
        "tables": sorted(identity),
        "digest": _membership_digest([json.dumps(identity, sort_keys=True)]),
    }


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = sorted(required - columns)
    if missing:
        raise AdapterFault(f"incompatible indexed-store schema: {table} missing {missing}")


def _fixture_paths(product: str, fixture: Mapping[str, Any]) -> list[str]:
    names = [str(page["path"]) for page in fixture["pages"]]
    if product == "exomem":
        return ["Knowledge Base/Reference/" + name for name in names]
    return names


def inspect_indexed_fixture(
    *, product: str, state: Path, vault: Path, fixture: Mapping[str, Any]
) -> dict[str, Any]:
    """Read one coherent snapshot of the current disposable derived store."""
    if product not in {"basic_memory", "exomem"}:
        raise ValueError(f"unknown product {product}")
    store = _indexed_store(product, state)
    expected_paths = _fixture_paths(product, fixture)
    proof: dict[str, Any] = {
        "ready": False,
        "expected_path_count": len(expected_paths),
        "expected_path_digest": _membership_digest(expected_paths),
        "observed_path_count": 0,
        "observed_path_digest": _membership_digest([]),
        "store": str(store) if store else None,
    }
    if store is None:
        proof["reason"] = "indexed store is not present"
        return proof
    try:
        with sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True) as conn:
            conn.execute("BEGIN")
            if product == "basic_memory":
                _require_columns(conn, "project", {"id", "name", "path"})
                _require_columns(conn, "entity", {"id", "file_path", "project_id"})
                _require_columns(
                    conn, "search_index", {"id", "file_path", "project_id", "entity_id", "type"}
                )
                proof["schema_identity"] = _schema_identity(
                    conn, ("project", "entity", "search_index")
                )
                project_rows = conn.execute(
                    "SELECT id, path FROM project WHERE name = 'main'"
                ).fetchall()
                expected_root = _contained_path(state, state / "home")
                if (
                    len(project_rows) != 1
                    or _contained_path(state, Path(str(project_rows[0][1]))) != expected_root
                ):
                    proof["reason"] = (
                        "configured main project does not bind to the disposable fixture root"
                    )
                    return proof
                project_id = project_rows[0][0]
                entities = [
                    (int(row[0]), str(row[1]), int(row[2]))
                    for row in conn.execute(
                        "SELECT id, file_path, project_id FROM entity WHERE project_id = ?",
                        (project_id,),
                    )
                ]
                search_rows = [
                    (int(row[0]), str(row[1]), int(row[2]), int(row[3]))
                    for row in conn.execute(
                        "SELECT id, file_path, project_id, entity_id FROM search_index "
                        "WHERE type = 'entity' AND project_id = ?",
                        (project_id,),
                    )
                ]
                search = [(row[0], row[1], row[2]) for row in search_rows]
                observed_paths = [row[1] for row in entities]
                proof.update(
                    {
                        "observed_path_count": len(observed_paths),
                        "observed_path_digest": _membership_digest(observed_paths),
                        "proof_method": "entity/search_index identity multiset join",
                        "identity_join_proof": "entity(id,file_path,project_id) = search_index(id,file_path,project_id); entity_id = id",
                        "product_binding": {"project": "main", "project_id": project_id},
                    }
                )
                proof["ready"] = (
                    Counter(observed_paths) == Counter(expected_paths)
                    and Counter(entities) == Counter(search)
                    and all(row[0] == row[3] for row in search_rows)
                )
            else:
                _require_columns(conn, "pages", {"path", "in_kb", "in_vault"})
                _require_columns(conn, "fts", {"stemmed"})
                proof["schema_identity"] = _schema_identity(conn, ("pages", "fts"))
                prefix = "Knowledge Base/Reference/"
                rows = conn.execute(
                    "SELECT pages.rowid, pages.path, pages.in_kb, pages.in_vault, fts.rowid "
                    "FROM pages LEFT JOIN fts ON fts.rowid = pages.rowid "
                    "WHERE pages.path LIKE ?",
                    (prefix + "%",),
                ).fetchall()
                observed_paths = [str(row[1]) for row in rows]
                proof.update(
                    {
                        "observed_path_count": len(observed_paths),
                        "observed_path_digest": _membership_digest(observed_paths),
                        "proof_method": "pages/fts rowid identity join with recall admission",
                        "identity_join_proof": "pages.rowid = fts.rowid",
                        "product_binding": {
                            "fixture_prefix": prefix,
                            "vault": str(vault.resolve()),
                        },
                    }
                )
                proof["ready"] = Counter(observed_paths) == Counter(expected_paths) and all(
                    row[2] == 1 and row[3] == 1 and row[4] is not None for row in rows
                )
    except sqlite3.Error as error:
        raise AdapterFault(f"incompatible indexed-store schema: {error}") from error
    proof.setdefault(
        "reason", "fixture membership, identity join, or recall admission is incomplete"
    )
    return proof


async def _await_indexed_fixture(
    *,
    product: str,
    state: Path,
    vault: Path,
    fixture: Mapping[str, Any],
    deadline: float,
    evidence: dict[str, Any],
) -> None:
    while True:
        evidence.clear()
        evidence.update(
            inspect_indexed_fixture(product=product, state=state, vault=vault, fixture=fixture)
        )
        if evidence["ready"]:
            return
        if time.perf_counter() >= deadline:
            raise AdapterFault(
                "initial indexed-corpus proof incomplete before startup timeout: "
                + str(evidence.get("reason", "fixture identity proof did not converge"))
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _await_initial_index(
    client: PublicClient,
    *,
    product: str,
    timeout: float | None = None,
    deadline: float | None = None,
) -> None:
    """Wait for the native public retrieval path before starting the workflow clock."""
    deadline = deadline if deadline is not None else time.perf_counter() + (timeout or 0)
    proof: dict[str, Any] = {"classification": "unobserved"}
    while True:
        if product == "basic_memory":
            payload = await client.call(
                "search_notes",
                {
                    "query": WARM_SENTINEL,
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
                    "query": WARM_SENTINEL,
                    "mode": "keyword",
                    "detail": "full",
                    "graph": False,
                    "rerank": False,
                    "limit": 1,
                },
                allow_refusal=True,
            )
        proof = _search_proof(payload)
        if result_classification(payload) == "ok" and search_marker_present(payload, WARM_SENTINEL):
            return
        if time.perf_counter() >= deadline:
            raise AdapterFault(
                "initial public search did not converge before timeout; "
                f"final_sanitized_response={json.dumps(proof, sort_keys=True)}"
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _await_exomem_mutation(
    client: PublicClient, *, timeout: float | None = None, deadline: float | None = None
) -> None:
    """Keep semantic corpus startup outside the shared warm-workflow clock."""
    deadline = deadline if deadline is not None else time.perf_counter() + (timeout or 0)
    while True:
        payload = await client.call(
            "remember",
            {
                "title": "Disposable readiness preview",
                "note_type": "insight",
                "content": "Public semantic admission is ready for the disposable workflow.",
                "response_detail": "full",
                "validate_only": True,
            },
            allow_refusal=True,
        )
        if (
            result_classification(payload) == "ok"
            and payload.get("draft_id")
            and payload.get("draft_hash")
        ):
            return
        error = payload.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        if code != "MUTATION_WARMING" or time.perf_counter() >= deadline:
            raise AdapterFault(
                f"initial public mutation admission failed: {code or 'unproved draft'}"
            )
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
    markdown = common_markdown_payload(marker)
    source = dict(fixture_pages(int(fixture["page_count"])))

    def created_path(payload: Mapping[str, Any]) -> str:
        path = payload.get("file_path")
        if not isinstance(path, str) or not path.strip():
            raise AdapterFault("write_note result omitted created file path")
        return path

    # Known fixture paths remain resolvable before the background title index
    # reaches them. Creation paths come from the actual public write result.
    tracker_path = str(fixture["named_pages"]["tracker"])
    stale_path = str(fixture["named_pages"]["stale_link"])
    chapter = await client.call(
        "write_note",
        {
            "title": "Completed chapter",
            "content": markdown["chapter"],
            "directory": "notes",
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    chapter_path = created_path(chapter)
    await client.call(
        "edit_note",
        {
            "identifier": tracker_path,
            "operation": "append",
            "content": markdown["tracker_append"],
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    await client.call(
        "edit_note",
        {
            "identifier": stale_path,
            "operation": "find_replace",
            "find_text": source[str(fixture["named_pages"]["stale_link"])],
            "content": stale_replacement_body(fixture, markdown),
            "expected_replacements": 1,
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    captured = await client.call(
        "write_note",
        {
            "title": "Independent capture",
            "content": markdown["capture"],
            "directory": "notes",
            "project": "main",
            "output_format": "json",
        },
        ack=True,
    )
    changed = [chapter_path, tracker_path, stale_path, created_path(captured)]
    reads = [
        await client.call(
            "read_note", {"identifier": path, "project": "main", "output_format": "json"}
        )
        for path in changed
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
    markdown = common_markdown_payload(marker)
    source = dict(fixture_pages(int(fixture["page_count"])))
    tracker = "Knowledge Base/Reference/" + str(fixture["named_pages"]["tracker"])
    stale = "Knowledge Base/Reference/" + str(fixture["named_pages"]["stale_link"])
    remembered = await client.call(
        "remember",
        {
            "title": "Completed chapter",
            "note_type": "insight",
            "content": markdown["chapter"],
            "response_detail": "full",
            "validate_only": True,
        },
    )
    commit = await client.call(
        "remember",
        _remember_commit_arguments(
            remembered, title="Completed chapter", content=markdown["chapter"]
        ),
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
                "kind": "replace_string",
                "old_string": source[str(fixture["named_pages"]["tracker"])],
                "new_string": source[str(fixture["named_pages"]["tracker"])]
                + markdown["tracker_append"],
                "replace_all": False,
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
                "old_string": source[str(fixture["named_pages"]["stale_link"])],
                "new_string": stale_replacement_body(fixture, markdown),
                "replace_all": False,
            },
        },
        ack=True,
    )
    capture = await client.call(
        "remember",
        {
            "title": "Independent capture",
            "note_type": "insight",
            "content": markdown["capture"],
            "response_detail": "full",
            "validate_only": True,
        },
    )
    captured = await client.call(
        "remember",
        _remember_commit_arguments(
            capture, title="Independent capture", content=markdown["capture"]
        ),
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
    marker: str,
    startup_timeout: float = 1800.0,
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
    public = PublicClient(client)
    setup_public = PublicClient(client)
    indexed_corpus: dict[str, Any] = {"ready": False, "reason": "unobserved"}
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
            package="basic-memory" if product == "basic_memory" else "exomem",
            expected_version=BASIC_MEMORY_VERSION if product == "basic_memory" else None,
            environment=env,
        ),
    }
    try:
        if product == "basic_memory" and not basic_memory_provenance_is_pinned(row["runtime"]):
            raise AdapterFault("Basic Memory runtime provenance does not match the pinned packet")
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
            startup_deadline = time.perf_counter() + startup_timeout
            # Discovery/initial indexing are pre-timing setup, never an inline reindex.
            await _await_initial_index(setup_public, product=product, deadline=startup_deadline)
            await _await_indexed_fixture(
                product=product,
                state=state,
                vault=vault,
                fixture=fixture,
                deadline=startup_deadline,
                evidence=indexed_corpus,
            )
            if product == "exomem":
                await _await_exomem_mutation(setup_public, deadline=startup_deadline)
            clock.start_timing()
            result = await (
                _run_basic_memory(public, marker, fixture, timeout)
                if product == "basic_memory"
                else _run_exomem(public, marker, fixture, timeout)
            )
            markdown = common_markdown_payload(marker)
            chapter_read, tracker_read, stale_read, capture_read = result["reads"]
            expected_bodies = expected_changed_bodies(fixture, marker)
            exact = (
                read_body_has_markers(chapter_read, [f"{marker}-chapter"])
                and read_body_has_markers(tracker_read, [f"{marker}-tracker"])
                and stale_replacement_verified(
                    stale_read,
                    old="[[Archived Runbook]]",
                    replacement=markdown["stale_replacement"],
                )
                and read_body_has_markers(capture_read, [f"{marker}-capture"])
            )
            bodies_match = all(
                read_body_equals(read, expected)
                for read, expected in zip(result["reads"], expected_bodies, strict=True)
            )
            bodies = [
                body_proof(read, expected)
                for read, expected in zip(result["reads"], expected_bodies, strict=True)
            ]
            converged = all(found for found, _, _, _ in result["searches"])
            clock.finish_closure()
            row.update(
                {
                    "status": "pass"
                    if exact and bodies_match and converged
                    else "observed_failure",
                    "public_calls": public.calls,
                    "measurements": call_measurements(public.calls),
                    "verification": {
                        "exact_read_your_write": exact,
                        "expected_bodies_match": bodies_match,
                        "body_proof": bodies,
                        "search_converged": converged,
                        "searches": [
                            {
                                "found": found,
                                "convergence_ms": elapsed,
                                "waits_ms": waits,
                                "final_response": proof,
                            }
                            for found, elapsed, waits, proof in result["searches"]
                        ],
                    },
                    "markers": [
                        f"{marker}-{suffix}" for suffix in ("chapter", "tracker", "capture")
                    ],
                }
            )
    except ProductRefusal as error:
        clock.finish_closure()
        row.update({"status": "observed_failure", "reason": str(error)})
    except AdapterFault as error:
        clock.finish_closure()
        row.update({"status": "invalid", "reason": str(error)})
    except Exception as error:  # noqa: BLE001 - public client startup failures have no shared type.
        clock.finish_closure()
        row.update(
            {"status": "invalid", "reason": f"MCP runtime failure: {type(error).__name__}: {error}"}
        )
    finally:
        clock.finish_teardown()
        row.setdefault("public_calls", public.calls)
        row.setdefault("measurements", call_measurements(public.calls))
        row["initial_indexed_corpus"] = indexed_corpus
        row["pre_timing_public_calls"] = setup_public.calls
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
        "--startup-timeout",
        type=float,
        default=1800.0,
        help="bounded initial public and full-index readiness wait in seconds",
    )
    parser.add_argument(
        "--python", type=Path, default=Path(sys.executable), help="explicit Exomem interpreter"
    )
    parser.add_argument("--basic-memory-executable", type=Path, required=True)
    parser.add_argument("--basic-memory-wheel", type=Path, default=None)
    parser.add_argument("--marker", default=None, help="shared marker for paired product runs")
    args = parser.parse_args(argv)
    selected = ("exomem", "basic_memory") if args.product == "both" else (args.product,)
    if args.pages < MIN_PAGES or args.pages > MAX_PAGES:
        parser.error(f"--pages must be between {MIN_PAGES} and {MAX_PAGES}")
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout must be positive")
    if (
        args.state.exists()
        and any(args.state.iterdir())
        or args.vault.exists()
        and any(args.vault.iterdir())
    ):
        parser.error("--state and --vault must both be empty disposable roots")
    rows = []
    marker = args.marker or f"common-subset-{uuid.uuid4().hex}"
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
                    marker=marker,
                    startup_timeout=args.startup_timeout,
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
