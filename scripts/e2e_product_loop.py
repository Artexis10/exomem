#!/usr/bin/env python
"""Installed-wheel black-box E2E for Exomem's governed product lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
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
            "PYTHONUTF8": "1",
        }
    )
    if WINDOWS:
        env["USERPROFILE"] = str(home)
    return env


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
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


async def _call(client, name: str, arguments: dict[str, Any], timeout: float) -> Any:
    result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=timeout)
    return _result_data(result)


async def _assert_relation_contexts(
    client,
    *,
    relation_ref: str,
    timeout: float,
) -> None:
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
        if graph.get("profile", {}).get("name") != profile:
            raise RuntimeError(f"installed context did not resolve {profile!r} profile")
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


async def _stdio_session(
    executable: Path,
    env: dict[str, str],
    work: Path,
    log_file: Path,
    *,
    timeout: float,
    first_run: bool,
    state: dict[str, Any],
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
                registry_result = await _call(
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
                source = await _call(
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
                memory = await _call(
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
                evidence = await _call(
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
                replacement = await _call(
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
                await _call(
                    client,
                    "edit_memory",
                    {
                        "path": new_ref,
                        "why": "attach proof to the active conclusion",
                        "field": "evidence",
                        "value": [f"[[{evidence['sidecar_path']}]]"],
                    },
                    timeout,
                )
                targets: dict[str, dict[str, Any]] = {}
                for key, title in (
                    ("study", "Lantern replication study"),
                    ("record", "Lantern source record"),
                    ("event", "Lantern trigger event"),
                ):
                    targets[key] = await _call(
                        client,
                        "remember",
                        {
                            "content": f"# {title}\n\n## Record\n\nCross-file graph target.\n",
                            "title": title,
                            "note_type": "insight",
                        },
                        timeout,
                    )
                relation_memory = await _call(
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
                reconcile = await _call(
                    client,
                    "maintain_memory",
                    {"mode": "reconcile", "dry_run": False},
                    timeout,
                )
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
                        "references_status": reconcile.get("references_status"),
                        "relation_ref": relation_memory["ref"],
                        "relation_path": relation_memory["path"],
                        "registry_hash": registry_result["saved"]["content_hash"],
                    }
                )
            else:
                reconcile = await _call(
                    client,
                    "maintain_memory",
                    {"mode": "reconcile", "dry_run": False},
                    timeout,
                )
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
                if reconcile.get("graph_status") != "refreshed":
                    raise RuntimeError("deleted graph sidecar was not rebuilt after restart")
    return state


def _installed_stdio(args: argparse.Namespace) -> int:
    executable = Path(args.executable)
    vault = Path(args.vault)
    work = Path(args.work)
    home = Path(args.home)
    env = _clean_env(home, vault)
    state: dict[str, Any] = {}
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
    asyncio.run(
        _stdio_session(
            executable,
            env,
            work,
            work / "stdio-restart.log",
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
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


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
            f"/studio did not redirect to /studio/ ({redirect_status} {redirect_headers.get('location')})"
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
        raise RuntimeError("review_item_context evolution section is not an honest supersession state")
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
        raise RuntimeError(f"stale-fingerprint triage lacked the changed-item contract: {stale_payload}")
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


def _reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    coord_port = _reserve_port()
    port_a = _reserve_port()
    port_b = _reserve_port()
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

        for url, env, log, handle_label in (
            (url_a, env_a, log_a, port_a),
            (url_b, env_b, log_b, port_b),
        ):
            handle = log.open("w", encoding="utf-8")
            handles.append(handle)
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
            f"{url_a}/api/coordination_status", method="POST", body={}, token="e2e-rest-key", timeout=timeout
        )
        _, status_b = _http_json(
            f"{url_b}/api/coordination_status", method="POST", body={}, token="e2e-rest-key", timeout=timeout
        )
        if status_a["data"].get("role") != "writer" or status_a["data"].get("holder") != "replica-a":
            raise RuntimeError(f"writer replica misreported coordination status: {status_a}")
        if status_b["data"].get("role") != "follower" or status_b["data"].get("holder") != "replica-a":
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
    print(json.dumps({"success": True, "transport": "lease", "takeover": True}))
    return 0


def _orchestrate(args: argparse.Namespace) -> int:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="exomem-product-e2e-") as tmp_raw:
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
