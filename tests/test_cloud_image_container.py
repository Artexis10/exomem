"""Task 2.8: a committed container test for the Exomem Cloud cell image.

Builds (or reuses, via `CLOUD_IMAGE_TAG`) the real `cloud` Dockerfile target
and drives it under production-shaped constraints -- `--read-only`, a `/tmp`
tmpfs, UID 10001, `--cap-drop ALL`, and the volume root chmod'd `2770` the way
a Kubernetes `fsGroup` volume leaves it -- over real HTTP with the MCP
bearer, the same protocol the gateway uses. No `EXOMEM_LOG_DIR` override is
passed, so the image's own default (design D1.2, `/tmp/exomem-logs`) is what
gets tested.

Gated on Docker and an explicit opt-in, like this repo's other disposable
infrastructure drills (`RUN_K3S_GOVERNANCE_DRILL_TEST`,
`RUN_HOSTED_RELEASE_IMAGE_TEST`): there is no existing plain-`docker run`
container-test convention narrower than that, so this one is
`RUN_CLOUD_IMAGE_TEST`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

RUN_CLOUD_IMAGE_TEST = os.environ.get("RUN_CLOUD_IMAGE_TEST") == "1"
if not RUN_CLOUD_IMAGE_TEST:  # pragma: no cover - the gate is the point
    pytest.skip(
        "set RUN_CLOUD_IMAGE_TEST=1 to run the disposable cloud image container test",
        allow_module_level=True,
    )
if shutil.which("docker") is None:  # pragma: no cover - the gate is the point
    pytest.skip(
        "docker is required for the cloud image container test",
        allow_module_level=True,
    )

pytestmark = pytest.mark.timeout(600)

ROOT = Path(__file__).resolve().parents[1]
IMAGE_TAG = os.environ.get("CLOUD_IMAGE_TAG", "exomem:lanea-r2-cloud-test")
BUILD_IMAGE = "CLOUD_IMAGE_TAG" not in os.environ

RUN_ID = uuid.uuid4().hex[:8]
VOLUME = f"lanea-r2-vol-{RUN_ID}"
CELL_ID = f"lanea-r2-cell-{RUN_ID}"
TOKEN = secrets.token_urlsafe(32)  # noqa: S105 - disposable test bearer, well past the 32-char floor
HOST_PORT = int(os.environ.get("CLOUD_IMAGE_TEST_PORT", "38765"))
LEGACY_PROTOCOL_VERSION = "2025-11-25"

# Distinctive phrases (task 2.8 step 9): one carried by a `remember` title,
# one by an `ask_memory` query. Neither may appear in any log or journal the
# cell writes (see `_assert_no_phrase_leak`).
PHRASE_REMEMBER = f"lanea-r2-distinctive-remember-phrase-{RUN_ID}"
PHRASE_ASK = f"lanea-r2-distinctive-ask-phrase-{RUN_ID}"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = _run(["docker", *args])
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _start_server(name: str, *, port: int) -> None:
    _docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "-u",
            "10001:10001",
            "-v",
            f"{VOLUME}:/data",
            "-p",
            f"{port}:8765",
            "-e",
            "EXOMEM_CLOUD_CELL=1",
            "-e",
            f"EXOMEM_CLOUD_CELL_ID={CELL_ID}",
            "-e",
            f"EXOMEM_CLOUD_CELL_TOKEN={TOKEN}",
            "-e",
            "EXOMEM_VAULT_PATH=/data/vault",
            IMAGE_TAG,
        ]
    )


def _run_cell_init() -> dict:
    result = _docker(
        [
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "-u",
            "10001:10001",
            "-v",
            f"{VOLUME}:/data",
            "-e",
            "EXOMEM_CLOUD_CELL=1",
            IMAGE_TAG,
            "cell-init",
            "--vault",
            "/data/vault",
            "--json",
        ]
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"cell-init printed no output:\n{result.stdout}\n{result.stderr}"
    return json.loads(lines[-1])


def _stat_modes(paths: list[str]) -> dict[str, str]:
    script = " && ".join(f'stat -c "{p} %a" {p}' for p in paths)
    result = _docker(
        [
            "run",
            "--rm",
            "-u",
            "0:0",
            "-v",
            f"{VOLUME}:/data",
            "--entrypoint",
            "sh",
            IMAGE_TAG,
            "-c",
            script,
        ]
    )
    modes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        path, mode = line.rsplit(" ", 1)
        modes[path] = mode
    return modes


def _wait_for_http(url: str, *, timeout: float) -> tuple[bool, list[int]]:
    """Poll `url`; returns whether it ever reached 200, and every status seen."""
    deadline = time.monotonic() + timeout
    statuses: list[int] = []
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                statuses.append(response.status_code)
                if response.status_code == 200:
                    return True, statuses
            except httpx.HTTPError:
                statuses.append(-1)
            time.sleep(0.5)
    return False, statuses


def _headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {TOKEN}",
    }


def _json_body(response: httpx.Response) -> dict:
    """Parse an MCP response body, plain JSON or SSE.

    The real server runs `stateless_http=True` without `json_response=True`
    (`server.py`'s production `mcp.run(...)`, unlike the in-process ASGI app
    the rest of this suite builds with `json_response=True`), so a live
    response is CRLF-terminated SSE framing (an `event: message` line, then
    a `data: {...}` line, then a blank line), not a bare JSON body.
    """
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError(f"no SSE data line in response: {response.text!r}")
    return response.json()


def _mcp_initialize(client: httpx.Client) -> httpx.Response:
    return client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "lanea-r2-container-test", "version": "1"},
            },
        },
    )


def _mcp_call_tool(
    client: httpx.Client, *, name: str, arguments: dict, request_id: int
) -> dict:
    response = client.post(
        "/mcp",
        headers={**_headers(), "mcp-protocol-version": LEGACY_PROTOCOL_VERSION},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    payload = _json_body(response)
    assert "error" not in payload, payload
    result = payload["result"]
    assert result.get("isError") is False, result
    return result


def _governed_write(
    client: httpx.Client, *, arguments: dict, request_id: int, timeout: float = 60.0
) -> dict:
    """A `remember` call, retried through `MUTATION_WARMING`.

    `/health/ready` gates retrieval admission (task 2.8 step 8), not the
    separate graph-handoff warm-up a governed write can still hit just after
    -- measured live: a write immediately after the first `/health/ready`
    200 came back `MUTATION_WARMING`. The refusal already names its own
    `retry_after_ms`; this honors it rather than treating the transient
    warm-up window as a failure.

    A committed write's own structured result (`ok`/`state`/`mutated`, ...)
    carries no `error` key at all -- that, not a `success` field, is the
    general success signal a structured OpError refusal (read-only,
    `MUTATION_WARMING`, ...) and an ordinary committed result share.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        result = _mcp_call_tool(
            client, name="remember", arguments=arguments, request_id=request_id
        )
        structured = result["structuredContent"]
        error = structured.get("error")
        if error is None:
            return result
        last = structured
        if error.get("code") != "MUTATION_WARMING":
            raise AssertionError(f"governed write failed: {structured}")
        wait_ms = error.get("retry_after_ms", 500)
        time.sleep(max(0.1, wait_ms / 1000.0))
    raise AssertionError(f"governed write never left MUTATION_WARMING: {last}")


def _assert_no_phrase_leak(server_container: str, phrases: list[str]) -> None:
    """Task 2.8 step 9: zero hits in every log and journal the cell writes.

    Content privacy covers observability output: container stdout and
    stderr, the log directory (the image default `/tmp/exomem-logs`), and
    any `*.log` or `*.jsonl` file anywhere on the volume outside the vault.
    The last part catches a log directory that ends up on the tenant volume
    that backups copy. Tenant *state* under `/data/host` (retrieval indexes,
    graph-sync checkpoints, custody, writer leases) is not a log. It sits on
    the tenant's own encrypted volume and names only the tenant's own notes,
    so the vault already holds the same information and it is not scanned.
    """
    # `/tmp` is the running container's own tmpfs -- read it via `exec`
    # rather than a second container, which would see an empty tmpfs of its
    # own.
    tmp_scan = _docker(
        ["exec", server_container, "sh", "-c", "grep -rI . /tmp 2>/dev/null || true"],
        check=False,
    )
    # `/data` minus `/data/vault` is on the shared volume -- a throwaway
    # helper container mounting the same volume reads it directly.
    data_scan = _docker(
        [
            "run",
            "--rm",
            "-u",
            "0:0",
            "-v",
            f"{VOLUME}:/data",
            "--entrypoint",
            "sh",
            IMAGE_TAG,
            "-c",
            "find /data -path /data/vault -prune -o -type f "
            "\\( -name '*.log' -o -name '*.log.*' -o -name '*.jsonl' -o -name '*.jsonl.*' \\) "
            "-print0 | xargs -0 -r grep -I . 2>/dev/null || true",
        ],
        check=False,
    )
    logs = _docker(["logs", server_container], check=False)
    haystack = "\n".join([tmp_scan.stdout, data_scan.stdout, logs.stdout, logs.stderr])
    for phrase in phrases:
        assert phrase not in haystack, (
            f"{phrase!r} leaked into a log, a journal or a container log line"
        )


def test_cloud_image_end_to_end_under_production_shaped_constraints() -> None:
    containers: list[str] = []
    try:
        # ---- image: build (default) or reuse (CLOUD_IMAGE_TAG) ----
        if BUILD_IMAGE:
            build = _run(
                [
                    "docker",
                    "build",
                    "--target",
                    "cloud",
                    "-t",
                    IMAGE_TAG,
                    "--build-arg",
                    f"EXOMEM_RELEASE_BUILD_TIME={_now_iso()}",
                    str(ROOT),
                ]
            )
            assert build.returncode == 0, build.stdout + build.stderr

        # ---- volume, chmod'd 2770 the way a Kubernetes fsGroup leaves it ----
        _docker(["volume", "create", VOLUME])
        _docker(
            [
                "run",
                "--rm",
                "-u",
                "0:0",
                "-v",
                f"{VOLUME}:/data",
                "--entrypoint",
                "sh",
                IMAGE_TAG,
                "-c",
                "chown 10001:10001 /data && chmod 2770 /data",
            ]
        )

        # ---- step 1: init ----
        init_report = _run_cell_init()
        assert init_report["vault_created"] is True, init_report
        modes = _stat_modes(["/data/vault", "/data/host"])
        assert modes["/data/vault"] == "700", modes
        assert modes["/data/host"] == "700", modes

        # ---- step 2: first start, only the D1 environment ----
        server_1 = f"lanea-r2-server1-{RUN_ID}"
        containers.append(server_1)
        _start_server(server_1, port=HOST_PORT)

        # `docker run -d` returns once the container is created, not once
        # uvicorn is actually accepting connections -- poll rather than
        # assume the first request lands after the port is bound.
        health_ok, health_statuses = _wait_for_http(
            f"http://127.0.0.1:{HOST_PORT}/health", timeout=30.0
        )
        assert health_ok, f"liveness never came up: {health_statuses}"

        # step 8: /health/ready is not ready until retrieval is admitted.
        ready_ok, ready_statuses = _wait_for_http(
            f"http://127.0.0.1:{HOST_PORT}/health/ready", timeout=90.0
        )
        assert ready_ok, f"never became ready: {ready_statuses}"
        assert any(code != 200 for code in ready_statuses), (
            f"/health/ready was 200 on the very first poll, so this run gives "
            f"no evidence it ever gated on retrieval admission: {ready_statuses}"
        )

        with httpx.Client(base_url=f"http://127.0.0.1:{HOST_PORT}", timeout=30.0) as client:
            assert _mcp_initialize(client).status_code == 200

            # ---- step 3: a governed write ----
            _governed_write(
                client,
                arguments={
                    "content": f"container test note body naming {PHRASE_REMEMBER}",
                    "title": PHRASE_REMEMBER,
                    "status": "draft",
                },
                request_id=2,
            )

        # ---- step 4: container replacement on the same volume ----
        # A real pod restart always re-runs the init container; re-running it
        # here is the faithful replacement and re-proves idempotency (task
        # 2.7) against the live image in the same pass.
        _docker(["rm", "-f", server_1], check=False)
        containers.remove(server_1)
        second_init = _run_cell_init()
        assert second_init["vault_created"] is False, second_init

        server_2 = f"lanea-r2-server2-{RUN_ID}"
        containers.append(server_2)
        _start_server(server_2, port=HOST_PORT)
        ready_ok, _ = _wait_for_http(
            f"http://127.0.0.1:{HOST_PORT}/health/ready", timeout=90.0
        )
        assert ready_ok

        # ---- step 5: owner-only modes still intact after replacement ----
        modes = _stat_modes(["/data/vault", "/data/host"])
        assert modes["/data/vault"] == "700", modes
        assert modes["/data/host"] == "700", modes

        with httpx.Client(base_url=f"http://127.0.0.1:{HOST_PORT}", timeout=30.0) as client:
            assert _mcp_initialize(client).status_code == 200

            # ---- step 6: exact and paraphrased recall of the write ----
            exact = _mcp_call_tool(
                client, name="ask_memory", arguments={"query": PHRASE_REMEMBER}, request_id=2
            )
            assert PHRASE_REMEMBER in json.dumps(exact["structuredContent"]), exact

            paraphrase = _mcp_call_tool(
                client,
                name="ask_memory",
                arguments={
                    "query": "what does the container test note say happened here"
                },
                request_id=3,
            )
            assert PHRASE_REMEMBER in json.dumps(paraphrase["structuredContent"]), paraphrase

            # ---- step 7: a further governed write ----
            _governed_write(
                client,
                arguments={
                    "content": f"second container test note naming {PHRASE_ASK}",
                    "title": f"second-write-{PHRASE_ASK}",
                    "status": "draft",
                },
                request_id=4,
            )

            # step 9 setup: an ask_memory call that carries the second phrase.
            _mcp_call_tool(
                client, name="ask_memory", arguments={"query": PHRASE_ASK}, request_id=5
            )

        # ---- step 9: zero phrase hits outside /data/vault, none in logs ----
        _assert_no_phrase_leak(server_2, [PHRASE_REMEMBER, PHRASE_ASK])

    finally:
        for name in containers:
            _docker(["rm", "-f", name], check=False)
        _docker(["volume", "rm", "-f", VOLUME], check=False)
        if BUILD_IMAGE:
            _docker(["image", "rm", "-f", IMAGE_TAG], check=False)
