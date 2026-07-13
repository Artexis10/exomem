#!/usr/bin/env python3
"""Rollout gate for durable Codex MCP login persistence.

This is intentionally a manual/live harness. Unit tests inject a subprocess
runner; operators run this script once against the staged connector URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit


class HarnessFailure(RuntimeError):
    """The live client gate observed registration, login, or reuse failure."""


_FORBIDDEN = (
    re.compile(r"not\s+logged\s+in", re.IGNORECASE),
    re.compile(r"MCP\s+startup\s+incomplete", re.IGNORECASE),
    re.compile(r"codex\s+mcp\s+login", re.IGNORECASE),
    re.compile(r"(?:open(?:ing)?|launch(?:ing)?)\b.{0,40}\bbrowser", re.IGNORECASE),
    re.compile(r"browser\b.{0,40}\b(?:login|log\s*in|authenticat)", re.IGNORECASE),
)


def classify_exec_output(stdout: str, stderr: str) -> None:
    """Raise unless one fresh process completed an Exomem MCP tool call."""
    combined = f"{stdout}\n{stderr}"
    for pattern in _FORBIDDEN:
        if pattern.search(combined):
            raise HarnessFailure("fresh Codex process attempted or requested a new login")

    completed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        server = str(item.get("server") or item.get("server_name") or "")
        tool = str(item.get("tool") or item.get("name") or "")
        status = str(item.get("status") or "completed").casefold()
        if (
            (server.casefold() == "exomem" or tool.casefold().startswith("mcp__exomem__"))
            and status not in {"failed", "error", "cancelled"}
        ):
            completed = True
            break
    if not completed:
        raise HarnessFailure("fresh Codex process did not complete an Exomem MCP call")


def _run_checked(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: list[str],
    *,
    env: dict[str, str],
    action: str,
    interactive: bool = False,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {"env": env, "check": False, "text": True}
    if not interactive:
        kwargs["capture_output"] = True
    result = runner(command, **kwargs)
    if result.returncode != 0:
        raise HarnessFailure(f"Codex failed to {action} (exit {result.returncode})")
    return result


def run_harness(
    *,
    url: str,
    codex_home: Path,
    runs: int = 3,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HarnessFailure("connector URL must be a credential-free HTTP(S) URL")
    if runs < 2:
        raise HarnessFailure("at least two fresh Codex processes are required")

    codex_home = codex_home.expanduser().resolve()
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        codex_home.chmod(0o700)
    except OSError:
        pass
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)

    _run_checked(
        runner,
        ["codex", "mcp", "add", "exomem", "--url", url.strip()],
        env=env,
        action="register the Exomem MCP server",
    )
    _run_checked(
        runner,
        ["codex", "mcp", "login", "exomem"],
        env=env,
        action="complete the one interactive Exomem login",
        interactive=True,
    )

    prompt = (
        "Use the exomem MCP server now. Make exactly one harmless read-only "
        "ask_memory call with query '__exomem_codex_session_smoke_absent__'. "
        "Do not use shell commands."
    )
    for index in range(1, runs + 1):
        result = _run_checked(
            runner,
            [
                "codex",
                "exec",
                "--ephemeral",
                "--json",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                prompt,
            ],
            env=env,
            action=f"run fresh compatibility process {index}",
        )
        classify_exec_output(result.stdout or "", result.stderr or "")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Exomem MCP connector URL")
    parser.add_argument(
        "--codex-home",
        type=Path,
        required=True,
        help="isolated persistent CODEX_HOME used only by this rollout gate",
    )
    parser.add_argument("--runs", type=int, default=3, help="fresh process count (minimum 2)")
    args = parser.parse_args(argv)
    try:
        return run_harness(url=args.url, codex_home=args.codex_home, runs=args.runs)
    except HarnessFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
