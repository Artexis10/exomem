from __future__ import annotations

import asyncio
import importlib
import json
import shutil

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

pytest.importorskip("exomem_provisioner")
bridge = importlib.import_module("hosted_cluster_bridge")


async def _node_fetch(origin: str, *, method: str, path: str, body: str = "") -> dict:
    script = """
const [origin, method, path, body] = process.argv.slice(1);
const response = await fetch(`${origin}${path}`, {
  method,
  headers: {
    host: "control.drill.invalid",
    authorization: "Bearer rehearsal-test",
    "content-type": "application/json",
  },
  ...(body ? { body } : {}),
});
console.log(JSON.stringify({ status: response.status, body: await response.text() }));
"""
    process = await asyncio.create_subprocess_exec(
        "node",
        "-e",
        script,
        origin,
        method,
        path,
        body,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


def test_control_ingress_proxy_restores_control_host_for_real_node_fetch():
    if shutil.which("node") is None:
        pytest.skip("Node is required for the optional transport regression probe")

    async def upstream(request: Request):
        if request.headers.get("host") != "control.drill.invalid":
            return PlainTextResponse("host not routed", status_code=404)
        return JSONResponse(
            {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "authorization": request.headers.get("authorization"),
                "forwarded_proto": request.headers.get("x-forwarded-proto"),
                "body": (await request.body()).decode(),
            }
        )

    app = Starlette(routes=[Route("/{path:path}", upstream, methods=["GET", "POST"])])

    async def scenario():
        async with bridge._serve_asgi(app) as upstream_origin:
            direct = await _node_fetch(
                upstream_origin,
                method="POST",
                path="/private/exomem/v2/agent/hosted-alpha-agent-v4/command/remember",
                body='{"probe":"direct"}',
            )
            assert direct["status"] == 404

            async with bridge.control_ingress_proxy(upstream_origin) as proxy_origin:
                get_result = await _node_fetch(
                    proxy_origin,
                    method="GET",
                    path="/health?probe=one",
                )
                post_result = await _node_fetch(
                    proxy_origin,
                    method="POST",
                    path="/private/exomem/v2/agent/hosted-alpha-agent-v4/command/remember?probe=two",
                    body='{"probe":"proxy"}',
                )

        assert get_result["status"] == 200
        assert json.loads(get_result["body"]) == {
            "method": "GET",
            "path": "/health",
            "query": "probe=one",
            "authorization": "Bearer rehearsal-test",
            "forwarded_proto": "https",
            "body": "",
        }
        assert post_result["status"] == 200
        assert json.loads(post_result["body"]) == {
            "method": "POST",
            "path": "/private/exomem/v2/agent/hosted-alpha-agent-v4/command/remember",
            "query": "probe=two",
            "authorization": "Bearer rehearsal-test",
            "forwarded_proto": "https",
            "body": '{"probe":"proxy"}',
        }

    asyncio.run(scenario())


def test_control_ingress_proxy_rejects_nonliteral_loopback_before_startup():
    async def scenario():
        with pytest.raises(ValueError, match="exact 127.0.0.1 HTTP origin"):
            async with bridge.control_ingress_proxy("http://localhost:9876"):
                raise AssertionError("invalid target started a proxy")

    asyncio.run(scenario())
