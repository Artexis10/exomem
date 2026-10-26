"""The published tools carry one original from a client file into recoverable custody."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace

from test_agent_upload_handoff import _ASGISyncTransport
from test_agent_upload_handoff import upload_env as upload_env

from exomem import artifact_recovery, server, upload_handoff
from exomem.__main__ import _attach_main
from exomem.governance import principal


def test_mcp_handoff_cli_capture_and_original_recovery(upload_env, tmp_path, monkeypatch, capsys):
    vault, who, *_ = upload_env
    original = b"\xef\xbb\xbf00:18:23 Speaker A: A precise original.\r\n\x00\xff"
    digest = hashlib.sha256(original).hexdigest()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    # This synthetic entropy produces one uninterrupted mixed-case run in the former encoding.
    monkeypatch.setattr(upload_handoff, "secrets", SimpleNamespace(
        token_hex=lambda count: bytes(range(count)).hex(),
        token_urlsafe=lambda count: base64.urlsafe_b64encode(bytes(range(count))).decode().rstrip("="),
    ))
    mcp = server.build_server(require_auth=False)

    def call(name, arguments):
        with principal.request_scope(who):
            result = asyncio.run(mcp.call_tool(name, arguments, run_middleware=False))
        payload = result.structured_content or json.loads(result.content[0].text)
        assert payload.get("success", True), payload
        return payload.get("result", payload)

    descriptor = call("transfer_artifact", {
        "operation": "upload", "lane": "source", "hold": True, "expected_sha256": digest,
    })
    assert descriptor["token"].startswith(upload_handoff.TOKEN_PREFIX), "the published tool blocked its own handoff"
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps(descriptor))
    path = tmp_path / "original.bin"
    path.write_bytes(original)
    assert _attach_main(
        [str(path), "--handoff-file", str(handoff), "--root", str(tmp_path)],
        transport=_ASGISyncTransport(upload_env[-1]),
    ) == 0
    upload = json.loads(capsys.readouterr().out)
    captured = call("capture_source", {
        "title": "Exact meeting original", "source_kind": "session",
        "files": [upload["file"]], "expected_sha256": digest,
    })
    [receipt] = captured["files"]
    assert receipt["outcome"] == "stored"
    assert receipt["hash"] == digest
    assert (vault / receipt["stored_path"]).read_bytes() == original
    with principal.request_scope(who):
        with artifact_recovery.stage(vault, receipt["stored_path"] + ".md", who) as (copied, descriptor):
            assert copied.private_path.read_bytes() == original
            assert descriptor["sha256"] == digest


def test_published_read_follows_correction_and_can_read_history(vault, source_schema):
    from exomem import commands
    from exomem import vault as vault_module

    first = commands.op_capture_source(vault, source_schema, title="Earlier original", source_kind="session", content="09:00 Original copy.")["source"]
    historical = (vault / first["path"]).read_bytes()
    current = commands.op_capture_source(
        vault, source_schema, title="Correct original", source_kind="session", content="09:01 Exact original.",
        correction={"supersedes": first["ref"], "expected_hash": vault_module.content_hash(historical.decode()), "reason": "Restore timestamp"},
    )["source"]
    read = commands.op_read_memory(vault, first["ref"])
    assert read["ref"] == current["ref"]
    assert read["source_resolution"]["requested_ref"] == first["ref"]
    assert "09:01" in read["body"]
    previous = commands.op_read_memory(vault, first["ref"], follow_corrections=False)
    assert previous["ref"] == first["ref"]
    assert "09:00" in previous["body"]
    assert (vault / first["path"]).read_bytes() == historical
