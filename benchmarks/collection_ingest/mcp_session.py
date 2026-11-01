"""One long-lived stdio MCP session against the real server, with a call ledger.

Shared by ``drive.py`` and ``score.py`` so both observe the product the same way:
only public tool results, every call timed, every refusal kept verbatim. All
server state (vault, state root, logs, bytecode cache) points at the caller's
scratch directory, never at the repository or a real vault.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

# A result that names `retry_after_ms` asks the caller to repeat the identical call.
RETRY_FIELD = "retry_after_ms"
DEFAULT_RETRY_DEADLINE_S = 180.0
ARG_LIST_CAP = 12


def isolated_env(
    *, state: Path, vault: Path, server_root: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Environment for a hermetic server: no EXOMEM_* inherited, every path in scratch."""
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
            # No model weights or network in this instrument: lexical retrieval only.
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_EAGER_BOOT": "1",
            "HF_HUB_OFFLINE": "1",
            "XDG_STATE_HOME": str(state / "xdg"),
            "PYTHONPATH": str(server_root / "src"),
            "PYTHONPYCACHEPREFIX": str(state / "pycache"),
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        }
    )
    env.update(extra or {})
    return env


def summarise_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, list) and len(value) > ARG_LIST_CAP:
            out[key] = {"len": len(value), "head": value[:5]}
        else:
            out[key] = value
    return out


def split_error_text(text: str) -> tuple[str | None, str]:
    """``CODE: reason`` is the product's error text format; anything else has no code."""
    head, sep, tail = text.partition(": ")
    if sep and head.replace("_", "").isalnum() and head == head.upper():
        return head, tail
    return None, text


def _texts(result: Any) -> list[str]:
    return [t for t in (getattr(item, "text", None) for item in result.content or ()) if t]


def decode(result: Any) -> tuple[str, Any, str | None, str | None]:
    """Return ``(outcome, data, error_code, error_message)`` for a raw tool result."""
    if result.is_error:
        text = "\n".join(_texts(result))
        code, _ = split_error_text(text.removeprefix("Error calling tool").split("': ", 1)[-1])
        return "tool_error", None, code, text[:800]
    data = result.structured_content
    if data is None:
        for text in _texts(result):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            break
    return classify(data)


def classify(data: Any) -> tuple[str, Any, str | None, str | None]:
    """Return ``(outcome, data, error_code, error_message)`` for a tool's decoded reply object."""
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, dict):
        error = data.get("error")
        if data.get("success") is False or isinstance(error, dict):
            err = error if isinstance(error, dict) else {}
            return "refused", data, str(err.get("code") or "UNKNOWN"), json.dumps(err)[:800]
    if data is None:
        return "transport_error", None, None, "no structured or JSON content"
    return "ok", data, None, None


def retry_hint_ms(data: Any) -> int | None:
    """The wait a refusal asks for, read from its ``error`` object; None if it asks nothing."""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        value = data["error"].get(RETRY_FIELD)
        if isinstance(value, int | float):
            return int(value)
    return None


class LedgerSession:
    """The call ledger over any transport: a subclass supplies ``invoke``, one raw tool call.

    ``call`` repeats a call while the product asks for a retry and records every logical call
    in ``self.calls``, so a drive log reads the same whichever transport carried it.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tools: list[str] = []
        self.tool_schemas: dict[str, Any] = {}

    async def invoke(self, tool: str, args: dict[str, Any]) -> tuple[str, Any, str | None, str | None]:
        raise NotImplementedError

    async def call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        journey: str,
        step: str,
        retry_deadline_s: float = DEFAULT_RETRY_DEADLINE_S,
    ) -> tuple[str, Any, dict[str, Any]]:
        """Run one logical call, repeating it while the product asks for a retry.

        Returns ``(outcome, data, record)``; the record is also appended to ``self.calls``.
        """
        started = time.perf_counter()
        attempts = 0
        waited_ms = 0
        warming_codes: list[str] = []
        outcome, data, code, message = "transport_error", None, None, None
        while True:
            attempts += 1
            try:
                outcome, data, code, message = await self.invoke(tool, args)
            except Exception as exc:  # noqa: BLE001 - a measurement harness keeps every failure verbatim
                outcome, data, code, message = "transport_error", None, type(exc).__name__, str(exc)[:800]
                break
            hint = retry_hint_ms(data) if outcome == "refused" else None
            if hint is None or (time.perf_counter() - started) > retry_deadline_s:
                break
            warming_codes.append(code or "")
            waited_ms += hint
            await asyncio.sleep(hint / 1000.0)
        record = {
            "n": len(self.calls) + 1,
            "journey": journey,
            "step": step,
            "tool": tool,
            "args": summarise_args(args),
            "outcome": outcome,
            "error_code": code,
            "error_message": message,
            "attempts": attempts,
            "retried_codes": sorted(set(warming_codes)),
            "retry_wait_ms": waited_ms,
            "wall_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
        self.calls.append(record)
        return outcome, data, record


class McpSession(LedgerSession):
    """Async context manager over one server process; ``call`` records into ``self.calls``."""

    def __init__(self, *, python: Path, state: Path, env: dict[str, str], timeout: float = 600.0):
        super().__init__()
        self.python = python
        self.state = state
        self.env = env
        self.timeout = timeout
        self._client: Any = None

    async def __aenter__(self) -> McpSession:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        self.state.mkdir(parents=True, exist_ok=True)
        transport = StdioTransport(
            command=str(self.python),
            args=["-m", "exomem", "--transport", "stdio"],
            env=self.env,
            cwd=str(self.state),
            keep_alive=False,
            log_file=self.state / "stdio.log",
        )
        self._client = Client(transport, timeout=self.timeout, init_timeout=self.timeout)
        await self._client.__aenter__()
        listed = await self._client.list_tools()
        self.tools = sorted(tool.name for tool in listed)
        self.tool_schemas = {tool.name: tool.input_schema for tool in listed}
        # Deliver a public refusal envelope to the harness instead of a client-side schema error.
        for owner in (self._client, getattr(self._client, "session", None)):
            schemas = getattr(owner, "_tool_output_schemas", None)
            if isinstance(schemas, dict):
                for name in tuple(schemas):
                    schemas[name] = None
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.__aexit__(*exc_info)

    async def invoke(self, tool: str, args: dict[str, Any]) -> tuple[str, Any, str | None, str | None]:
        return decode(await self._client.call_tool_mcp(tool, args))

