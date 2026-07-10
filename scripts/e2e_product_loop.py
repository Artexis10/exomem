#!/usr/bin/env python
"""Installed-wheel black-box E2E for Exomem's governed product lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
            }
            missing = required - tools
            if missing:
                raise RuntimeError(f"installed stdio server missing tools: {sorted(missing)}")

            if first_run:
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
                state.update(
                    {
                        "source_ref": source["ref"],
                        "old_ref": memory["ref"],
                        "new_ref": new_ref,
                        "evidence_ref": evidence["ref"],
                        "new_path": replacement["new_path"],
                        "references_status": reconcile.get("references_status"),
                    }
                )
            else:
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
                    status, _ = _http_json(
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
        print("product-e2e: HTTP auth + MCP lifecycle + shutdown")
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
    parser.add_argument("--budget-seconds", type=float, default=240.0)
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
    return _orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
