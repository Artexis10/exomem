"""Capture two arms' ranked results for the same queries, over MCP on HTTP.

An arm is an endpoint: a URL plus the NAME of an environment variable that
holds its bearer token. The token is read once, handed to the MCP client, and
scrubbed from any error text before it is kept; it is never printed or written.

Both arms get the same tool and the same fixed arguments, called the way an
assistant calls it through the MCP client. Each query goes to both arms, one
after the other, with the leading arm alternating by case index so neither
arm always runs first. Latency is wall-clock at the client around the tool
call only. A failed call is a recorded outcome with its error kind and stays in
every denominator; nothing is retried.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from exomem.recall_canary import validate_target

from . import private

RUN_SCHEMA = "recall-parity/run/1"
DEFAULT_TOOL = "search_memory"
#: ``detail=full`` is what adds the excerpt a judge needs; ``compact`` returns
#: only path and title. ``limit`` is the pool depth the packet and score use.
DEFAULT_ARGUMENTS: dict[str, Any] = {"limit": 10, "detail": "full"}
ERROR_DETAIL_CHARS = 300


class CaptureError(RuntimeError):
    """A configuration or pre-flight fault. Its text never carries a token."""


@dataclass(frozen=True)
class Arm:
    name: str
    url: str
    token_env: str


def parse_arm(name: str, url: str, token_env: str) -> Arm:
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise CaptureError("an arm name may hold only letters, digits, '-' and '_'")
    try:
        validate_target(url)
    except ValueError:
        raise CaptureError(
            f"arm {name}: the URL must be http(s) with a host and no credentials"
        ) from None
    if urlsplit(url).query:
        # A secret in a query string would be copied into the run manifest.
        raise CaptureError(f"arm {name}: the URL must not carry a query string")
    return Arm(name=name, url=url, token_env=token_env)


def read_token(arm: Arm) -> str:
    token = os.environ.get(arm.token_env, "").strip()
    if not token:
        # The variable name is deliberately not echoed: a token passed by mistake
        # in its place must not reach the terminal.
        raise CaptureError(f"arm {arm.name}: its token environment variable is unset or empty")
    return token


def scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[token]")
    return text


def decode_hits(result: Any) -> tuple[list[Any] | None, str | None, str | None]:
    """The hit list of one search result, else the error kind and any detail.

    ``search_memory`` answers a list of hits, or an envelope ``{"hits": [...]}``
    that also carries advisory blocks; the MCP layer may wrap either in
    ``{"result": ...}``. ``warming`` and ``degraded`` are the product's own
    envelope markers for a response that is not a full answer.
    """

    if result.is_error:
        texts = [getattr(item, "text", None) for item in result.content or ()]
        return None, "tool_error", " ".join(text for text in texts if text)
    data = result.structured_content
    if data is None:
        for item in result.content or ():
            try:
                data = json.loads(getattr(item, "text", None) or "")
            except ValueError:
                continue
            break
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, list):
        return data, None, None
    if isinstance(data, dict):
        error = data.get("error")
        if data.get("success") is False or isinstance(error, dict):
            code = error.get("code") if isinstance(error, dict) else None
            return None, "refused", str(code) if code else None
        for marker in ("warming", "degraded"):
            if data.get(marker):
                return None, marker, None
        if isinstance(data.get("hits"), list):
            return data["hits"], None, None
    return None, "malformed", None


def ranked_results(hits: Sequence[Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Hits as ranked result rows, with how many were dropped and repeated.

    A document is identified by its vault-relative ``path``. A hit with no
    string path cannot be identified and is counted, not guessed. A path seen
    earlier is a repeat of the same document and is counted, not re-ranked.
    """

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = repeated = 0
    for hit in hits:
        path = hit.get("path") if isinstance(hit, dict) else None
        if not isinstance(path, str) or not path.strip():
            dropped += 1
            continue
        identifier = path.strip()
        if identifier in seen:
            repeated += 1
            continue
        seen.add(identifier)
        title, excerpt = hit.get("title"), hit.get("excerpt")
        results.append(
            {
                "rank": len(results) + 1,
                "id": identifier,
                "title": title if isinstance(title, str) else None,
                "snippet": excerpt if isinstance(excerpt, str) else None,
            }
        )
    return results, dropped, repeated


async def ask(
    client: Any, tool: str, arguments: dict[str, Any], *, timeout_s: float, secrets: Sequence[str]
) -> dict[str, Any]:
    """One timed call. Always returns an outcome record and never raises."""

    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            result = await client.call_tool(
                tool, arguments, raise_on_error=False, timeout=timeout_s
            )
    except TimeoutError:
        error: dict[str, Any] | None = {"kind": "timeout", "detail": None}
        hits = None
    except Exception as exc:  # noqa: BLE001 - one failed call is data, never a crash
        text = scrub(f"{type(exc).__name__}: {exc}", secrets)[:ERROR_DETAIL_CHARS]
        error = {"kind": "transport", "detail": text}
        hits = None
    else:
        hits, kind, detail = decode_hits(result)
        error = None
        if kind is not None:
            text = scrub(detail, secrets)[:ERROR_DETAIL_CHARS] if detail else None
            error = {"kind": kind, "detail": text}
    elapsed_ms = (time.perf_counter() - started) * 1000
    results, dropped, repeated = ranked_results(hits or [])
    return {
        "ok": error is None,
        "latency_ms": round(elapsed_ms, 3),
        "error": error,
        "results": results,
        "dropped_results": dropped,
        "repeated_results": repeated,
    }


def _schema_digest(schema: Any) -> str:
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _open_session(
    stack: AsyncExitStack,
    arm: Arm,
    token: str,
    *,
    tool: str,
    arguments: dict[str, Any],
    timeout_s: float,
) -> tuple[Any, str]:
    """Open the arm's session and check it offers the tool with these arguments."""

    from fastmcp import Client

    # cache=False: a client-side cache would turn a repeat call into a local read.
    client = Client(arm.url, auth=token, timeout=timeout_s, init_timeout=timeout_s, cache=False)
    try:
        await stack.enter_async_context(client)
        tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 - the exception text can carry the endpoint
        raise CaptureError(
            f"arm {arm.name}: cannot open a session ({type(exc).__name__})"
        ) from None
    offered = {item.name: item for item in tools}
    if tool not in offered:
        raise CaptureError(
            f"arm {arm.name}: tool {tool!r} is not offered; offered: {sorted(offered)}"
        )
    schema = offered[tool].input_schema or {}
    properties = schema.get("properties")
    if isinstance(properties, dict):
        unknown = sorted((set(arguments) | {"query"}) - set(properties))
        if unknown:
            raise CaptureError(f"arm {arm.name}: tool {tool!r} does not accept arguments {unknown}")
    return client, _schema_digest(schema)


async def capture_arms(
    arms: Sequence[Arm],
    tokens: dict[str, str],
    cases: Sequence[dict[str, str]],
    *,
    tool: str,
    arguments: dict[str, Any],
    timeout_s: float,
    warmup: int,
    record: Callable[[str, dict[str, Any]], None],
) -> dict[str, str]:
    """Call every case on both arms, passing each outcome row to ``record``.

    Returns each arm's tool-input-schema digest. ``warmup`` extra calls per arm
    use the first case's query and are discarded, so a cold cell does not
    inflate the first measured latency.
    """

    secrets = list(tokens.values())
    async with AsyncExitStack() as stack:
        sessions: list[tuple[Arm, Any]] = []
        digests: dict[str, str] = {}
        for arm in arms:
            client, digests[arm.name] = await _open_session(
                stack, arm, tokens[arm.name], tool=tool, arguments=arguments, timeout_s=timeout_s
            )
            sessions.append((arm, client))
        for _ in range(warmup):
            for _arm, client in sessions:
                first = {**arguments, "query": cases[0]["query"]}
                await ask(client, tool, first, timeout_s=timeout_s, secrets=secrets)
        for index, case in enumerate(cases):
            order = sessions if index % 2 == 0 else sessions[::-1]
            for position, (arm, client) in enumerate(order):
                call = {**arguments, "query": case["query"]}
                outcome = await ask(client, tool, call, timeout_s=timeout_s, secrets=secrets)
                record(
                    arm.name,
                    {"case_id": case["case_id"], "arm": arm.name, "position": position, **outcome},
                )
    return digests


def capture_path(run_dir: Path, arm: str) -> Path:
    return run_dir / "capture" / f"{arm}.jsonl"


def run_capture(
    root: Path,
    run_name: str,
    *,
    split: str,
    arms: Sequence[Arm],
    tool: str,
    arguments: dict[str, Any],
    timeout_s: float,
    warmup: int,
) -> dict[str, Any]:
    """Run one capture under ``root/runs/<run_name>``; the manifest is written last."""

    if len(arms) != 2 or arms[0].name == arms[1].name:
        raise CaptureError("a paired run needs exactly two arms with different names")
    if "query" in arguments:
        raise CaptureError("--arguments must not set 'query'; each case supplies it")
    tokens = {arm.name: read_token(arm) for arm in arms}
    cases_path = root / "sample" / "cases.jsonl"
    sample_path = root / "sample" / "sample-manifest.json"
    cases_bytes = cases_path.read_bytes()
    cases = [json.loads(line) for line in cases_bytes.decode("utf-8").splitlines() if line.strip()]
    if split != "all":
        cases = [case for case in cases if case["split"] == split]
    if not cases:
        raise CaptureError(f"the sample has no cases in split {split!r}")
    sample = private.read_json(sample_path)
    run_dir = root / "runs" / run_name
    if run_dir.exists():
        raise CaptureError("this run name already exists; artifacts are immutable")

    created = dt.datetime.now(dt.UTC)
    handles = {arm.name: private.open_new(capture_path(run_dir, arm.name)) for arm in arms}
    outcomes: dict[str, Counter[str]] = {arm.name: Counter() for arm in arms}

    def record(arm_name: str, row: dict[str, Any]) -> None:
        handles[arm_name].write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handles[arm_name].flush()
        outcomes[arm_name]["ok" if row["ok"] else f"error:{row['error']['kind']}"] += 1

    try:
        digests = asyncio.run(
            capture_arms(
                arms,
                tokens,
                cases,
                tool=tool,
                arguments=arguments,
                timeout_s=timeout_s,
                warmup=warmup,
                record=record,
            )
        )
    finally:
        for handle in handles.values():
            handle.close()
    manifest = {
        "schema": RUN_SCHEMA,
        "run": run_name,
        "created_utc": created.isoformat(timespec="seconds"),
        "finished_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "split": split,
        "case_ids": [case["case_id"] for case in cases],
        "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "sample": {
            "seed": sample["seed"],
            "split_sizes": sample["split_sizes"],
            "manifest_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
        },
        "tool": tool,
        "arguments": arguments,
        "query_argument": "query",
        "arms": [
            {
                "name": arm.name,
                "url": arm.url,
                "token_env": arm.token_env,
                "tool_input_schema_sha256": digests[arm.name],
            }
            for arm in arms
        ],
        "arm_order": "alternating by case index; the first arm leads on even indexes",
        "warmup_calls_per_arm": warmup,
        "timeout_s": timeout_s,
        "client": {"fastmcp": metadata.version("fastmcp"), "mcp": metadata.version("mcp")},
        "outcomes": {name: dict(sorted(counter.items())) for name, counter in outcomes.items()},
    }
    private.write_json(run_dir / "manifest.json", manifest)
    return manifest
