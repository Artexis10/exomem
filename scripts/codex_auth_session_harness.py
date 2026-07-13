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


SENTINEL_QUERY = "__exomem_codex_session_smoke_absent__"
_SUCCESS_STATUSES = {"completed", "success", "succeeded"}
_ERROR_STATUSES = {"cancelled", "error", "failed", "failure"}


_FORBIDDEN = (
    re.compile(r"not\s+logged\s+in", re.IGNORECASE),
    re.compile(r"MCP\s+startup\s+incomplete", re.IGNORECASE),
    re.compile(r"codex\s+mcp\s+login", re.IGNORECASE),
    re.compile(r"(?:open(?:ing)?|launch(?:ing)?)\b.{0,40}\bbrowser", re.IGNORECASE),
    re.compile(r"browser\b.{0,40}\b(?:login|log\s*in|authenticat)", re.IGNORECASE),
)


def _has_error_marker(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in {"iserror", "is_error"} and item is True:
                return True
            if (
                normalized_key in {"error", "errors"}
                and item is not None
                and item is not False
                and item != ""
                and item != []
            ):
                return True
            if normalized_key == "status" and str(item).casefold() in _ERROR_STATUSES:
                return True
            if normalized_key == "type" and str(item).casefold() in {
                "error",
                "item.failed",
                "turn.failed",
            }:
                return True
            if _has_error_marker(item):
                return True
    elif isinstance(value, list):
        return any(_has_error_marker(item) for item in value)
    return False


def _arguments(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def classify_exec_output(stdout: str, stderr: str) -> None:
    """Require exactly one explicit successful sentinel ``ask_memory`` call."""
    combined = f"{stdout}\n{stderr}"
    for pattern in _FORBIDDEN:
        if pattern.search(combined):
            raise HarnessFailure("fresh Codex process attempted or requested a new login")

    observed_call_ids: set[str] = set()
    completed_call_ids: list[str] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if _has_error_marker(event):
            raise HarnessFailure("fresh Codex process emitted an error event")
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        server = str(item.get("server") or item.get("server_name") or "")
        tool = str(item.get("tool") or item.get("name") or "")
        if not (
            server.casefold() == "exomem"
            or tool.casefold().startswith("mcp__exomem__")
        ):
            continue
        if tool.casefold() not in {"ask_memory", "mcp__exomem__ask_memory"}:
            raise HarnessFailure("fresh Codex process called an unexpected Exomem tool")
        if _arguments(item.get("arguments")) != {"query": SENTINEL_QUERY}:
            raise HarnessFailure("Exomem ask_memory call used unexpected arguments")
        if _has_error_marker(item):
            raise HarnessFailure("Exomem ask_memory call returned an error")

        raw_id = item.get("id")
        call_id = (
            str(raw_id)
            if raw_id is not None and str(raw_id)
            else f"ambiguous-event-{line_number}"
        )
        observed_call_ids.add(call_id)
        if event.get("type") == "item.completed":
            if str(item.get("status") or "").casefold() not in _SUCCESS_STATUSES:
                raise HarnessFailure(
                    "Exomem ask_memory call lacked explicit success status"
                )
            completed_call_ids.append(call_id)

    if len(observed_call_ids) != 1 or len(completed_call_ids) != 1:
        raise HarnessFailure(
            "fresh Codex process must contain one unambiguous completed Exomem call"
        )


def _run_checked(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: list[str],
    *,
    env: dict[str, str],
    action: str,
    timeout: float,
    interactive: bool = False,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "env": env,
        "check": False,
        "text": True,
        "timeout": timeout,
    }
    if not interactive:
        kwargs["capture_output"] = True
    try:
        result = runner(command, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise HarnessFailure(f"Codex timed out while trying to {action}") from error
    if result.returncode != 0:
        raise HarnessFailure(f"Codex failed to {action} (exit {result.returncode})")
    return result


def run_harness(
    *,
    url: str,
    codex_home: Path,
    runs: int = 3,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    acknowledge_disposable_target: bool = False,
) -> int:
    if not acknowledge_disposable_target:
        raise HarnessFailure(
            "explicit acknowledgement of a disposable staged target is required"
        )
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
    if codex_home.exists() and (
        not codex_home.is_dir() or any(codex_home.iterdir())
    ):
        raise HarnessFailure(
            "CODEX_HOME must be a new or empty isolated directory for the initial login"
        )
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
        timeout=30.0,
    )
    _run_checked(
        runner,
        ["codex", "mcp", "login", "exomem"],
        env=env,
        action="complete the one interactive Exomem login",
        timeout=300.0,
        interactive=True,
    )

    prompt = (
        "Use the exomem MCP server now. Make exactly one harmless read-only "
        f"ask_memory call with query '{SENTINEL_QUERY}'. Do not call any other "
        "Exomem tool. Do not use shell commands."
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
            timeout=300.0,
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
    parser.add_argument(
        "--acknowledge-disposable-target",
        action="store_true",
        required=True,
        help="confirm the URL is a disposable staged KB where this live gate is safe",
    )
    args = parser.parse_args(argv)
    try:
        return run_harness(
            url=args.url,
            codex_home=args.codex_home,
            runs=args.runs,
            acknowledge_disposable_target=args.acknowledge_disposable_target,
        )
    except HarnessFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
