"""Offline contract tests for the MemoryBench Supermemory traffic recorder."""

from __future__ import annotations

import asyncio
import contextlib
import concurrent.futures
import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest


PINS = {
    "memorybench_commit_sha": "1" * 40,
    "memorybench_tree_sha": "2" * 40,
    "provider_sha256": "3" * 64,
    "sdk_version": "4.0.0",
    "sdk_integrity": "sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ==",
}


def _writer(tmp_path: Path):
    from benchmarks.memorybench.traffic import RecordingWriter

    return RecordingWriter(tmp_path / "recording", pins=PINS)


def _response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "authorization": "Bearer upstream-secret"},
        content=b'{"ok":true,"token":"nested-secret"}',
        request=request,
    )


def _proxy(tmp_path: Path, handler=_response, *, secrets: tuple[str, ...] = ("nested-secret",)):
    from benchmarks.memorybench.recording_proxy import RecordingProxy

    return RecordingProxy(
        "http://127.0.0.1:8765",
        _writer(tmp_path),
        transport=httpx.MockTransport(handler),
        configured_secrets=secrets,
    )


def _run(coro):
    return asyncio.run(coro)


def test_manifest_pins_verified_harness_provider_and_sdk(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    manifest = json.loads((writer.output_dir / "recording-manifest.json").read_text())
    assert manifest["pins"] == PINS
    assert manifest["status"] == "recording"


def test_manifest_marks_every_timing_non_publishable(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/doc-1"))
    proxy.writer.finalize()
    row = json.loads((proxy.writer.output_dir / "http-attempts.jsonl").read_text())
    assert row["timing"] == {"latency_publishable": False, "reason": "host_unvalidated", "ms": pytest.approx(row["timing"]["ms"])}


def test_schemas_match_exported_models(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import export_json_schemas

    fresh = {path.name: path.read_bytes() for path in export_json_schemas(tmp_path)}
    committed = {
        path.name: path.read_bytes()
        for path in Path("benchmarks/memorybench/schema").glob("*.schema.json")
    }
    assert fresh == committed


def test_writer_refuses_existing_or_incomplete_output(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import RecordingError, RecordingWriter

    output = tmp_path / "recording"
    RecordingWriter(output, pins=PINS)
    with pytest.raises(RecordingError, match="existing_output"):
        RecordingWriter(output, pins=PINS)
    with pytest.raises(RecordingError, match="incomplete_recording"):
        RecordingWriter(tmp_path / "other", pins=PINS).finalize()


def test_validator_recomputes_count_digest_and_ordinals(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    _run(proxy.handle("GET", "/v3/documents/b"))
    proxy.writer.finalize()
    manifest = validate_recording(proxy.writer.output_dir, expected_pins=PINS)
    assert manifest.attempt_count == 2
    assert manifest.attempts_sha256 == hashlib.sha256(
        (proxy.writer.output_dir / "http-attempts.jsonl").read_bytes()
    ).hexdigest()


def test_complete_status_cannot_mask_tampered_attempt_bytes(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    attempts = proxy.writer.output_dir / "http-attempts.jsonl"
    attempts.write_bytes(attempts.read_bytes().replace(b'"attempt_ordinal":1', b'"attempt_ordinal":2'))
    with pytest.raises(RecordingError, match="attempts_digest_mismatch"):
        validate_recording(proxy.writer.output_dir, expected_pins=PINS)


def test_forged_complete_manifest_cannot_claim_an_unfinalized_recording(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    writer = _writer(tmp_path)
    manifest_path = writer.output_dir / "recording-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        status="complete",
        completed_at=manifest["started_at"],
        attempt_count=1,
        attempts_sha256=hashlib.sha256(b"").hexdigest(),
    )
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RecordingError, match="incomplete_recording"):
        validate_recording(writer.output_dir, expected_pins=PINS)


def test_forwards_original_json_once_and_records_sanitized_copy(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(request)

    proxy = _proxy(tmp_path, handler, secrets=("top-secret", "nested-secret"))
    body = b'{"token":"top-secret","ordinary":"value"}'
    result = _run(
        proxy.handle(
            "POST",
            "/v3/documents",
            raw_query="tag=one&tag=two&api_key=top-secret",
            headers=[("content-type", "application/json"), ("authorization", "Bearer top-secret")],
            body=body,
        )
    )
    assert result.status_code == 200
    assert len(seen) == 1
    assert seen[0].content == body
    row = json.loads((proxy.writer.output_dir / "http-attempts.jsonl").read_text())
    recorded = json.dumps(row, sort_keys=True)
    assert "top-secret" not in recorded and "nested-secret" not in recorded
    assert row["request"]["path"] == "/v3/documents"
    assert row["request"]["query"] == [["tag", "one"], ["tag", "two"], ["api_key", "[redacted]"]]
    assert datetime.fromisoformat(row["started_at"]).tzinfo is not None
    assert row["request"]["body"] == {
        "state": "json",
        "declared_bytes": None,
        "observed_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "sanitized_json": {"token": "[redacted]", "ordinary": "value"},
        "redaction_count": 1,
    }


def test_proxy_never_retries_or_follows_redirects(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "/elsewhere"}, request=request)

    result = _run(_proxy(tmp_path, handler).handle("GET", "/v3/documents/a"))
    assert calls == 1
    assert result.status_code == 307


def test_preserves_repeated_identical_get_attempts(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a", raw_query="x=1&x=1"))
    _run(proxy.handle("GET", "/v3/documents/a", raw_query="x=1&x=1"))
    rows = [json.loads(line) for line in (proxy.writer.output_dir / "http-attempts.jsonl").read_text().splitlines()]
    assert [row["attempt_ordinal"] for row in rows] == [1, 2]
    assert rows[0]["request"] == rows[1]["request"]


def test_concurrent_attempt_ordinals_are_unique(tmp_path: Path) -> None:
    async def exercise() -> list[int]:
        proxy = _proxy(tmp_path)
        await asyncio.gather(*(proxy.handle("GET", f"/v3/documents/{number}") for number in range(12)))
        return [
            json.loads(line)["attempt_ordinal"]
            for line in (proxy.writer.output_dir / "http-attempts.jsonl").read_text().splitlines()
        ]

    ordinals = _run(exercise())
    assert sorted(ordinals) == list(range(1, 13))


def test_loopback_server_forwards_percent_encoded_path_and_ordered_query(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _response(request)

    async def exercise() -> None:
        proxy = _proxy(tmp_path, handler)
        base_url = await proxy.start("127.0.0.1", 0)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/v3%2Fdocuments?tag=one&tag=two",
                headers={"content-type": "application/json"},
                content=b'{"document":"fixture"}',
            )
        assert response.status_code == 200
        await proxy.close()

    _run(exercise())
    assert len(calls) == 1
    row = json.loads((tmp_path / "recording" / "http-attempts.jsonl").read_text())
    assert row["request"]["path"] == "/v3%2Fdocuments"
    assert row["request"]["query"] == [["tag", "one"], ["tag", "two"]]


def test_authorization_nested_secrets_and_error_text_never_reach_artifacts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("provider-failure secret-value")

    proxy = _proxy(tmp_path, broken, secrets=("secret-value",))
    result = _run(
        proxy.handle(
            "POST",
            "/v3/documents",
            headers=[("content-type", "application/json"), ("authorization", "Bearer secret-value")],
            body=b'{"outer":{"password":"secret-value"}}',
        )
    )
    assert result.status_code == 502
    assert result.body == b'{"error":"upstream_unavailable"}'
    captured = capsys.readouterr()
    leaked = "\n".join(path.read_text() for path in proxy.writer.output_dir.iterdir() if path.is_file()) + captured.out + captured.err
    assert "secret-value" not in leaked
    assert "provider-failure" not in leaked


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ([("content-type", "application/json"), ("content-length", "5"), ("content-length", "5")], b"{}", "ambiguous_content_length"),
        ([("transfer-encoding", "chunked")], b"", "unsupported_transfer_encoding"),
        ([("content-encoding", "gzip")], b"", "unsupported_content_encoding"),
        ([("content-type", "text/plain")], b"{}", "unsupported_media_type"),
        ([("content-type", "application/json")], b"\xff", "invalid_json"),
        ([("content-type", "application/json")], b'{"x":1e999}', "invalid_json"),
        ([("content-type", "application/json")], b'{"x":1,"x":2}', "duplicate_json_key"),
    ],
)
def test_request_refusals_make_zero_upstream_contacts(tmp_path: Path, headers, body: bytes, code: str) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request)

    result = _run(_proxy(tmp_path, handler).handle("POST", "/v3/documents", headers=headers, body=body))
    assert calls == 0
    assert result.status_code == 400
    assert result.body == json.dumps({"error": code}, separators=(",", ":")).encode()


def test_oversized_request_makes_zero_upstream_contacts(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request)

    proxy = _proxy(tmp_path, handler)
    proxy.writer.max_body_bytes = 2
    result = _run(proxy.handle("POST", "/v3/documents", headers=[("content-type", "application/json")], body=b"{}\n"))
    assert calls == 0
    assert result.body == b'{"error":"body_too_large"}'


@pytest.mark.parametrize("content", [b"\xff", b'{"x":1,"x":2}'])
def test_invalid_upstream_json_returns_generic_502_without_partial_body(tmp_path: Path, content: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=content, request=request)

    result = _run(_proxy(tmp_path, handler).handle("GET", "/v3/documents/a"))
    assert result.status_code == 502
    assert result.body == b'{"error":"upstream_response_invalid"}'
    assert content not in result.body


def test_oversized_upstream_response_returns_generic_502_without_partial_body(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"ok":"long"}', request=request)

    proxy = _proxy(tmp_path, handler)
    proxy.writer.max_body_bytes = 4
    result = _run(proxy.handle("GET", "/v3/documents/a"))
    assert result.status_code == 502
    assert result.body == b'{"error":"upstream_response_invalid"}'


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/plain"},
        {"content-type": "application/json", "content-encoding": "br"},
        {"content-type": "application/json", "transfer-encoding": "gzip"},
    ],
)
def test_unsupported_upstream_response_framing_returns_generic_502(tmp_path: Path, headers: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=b'{"secret":"never-forwarded"}', request=request)

    result = _run(_proxy(tmp_path, handler).handle("GET", "/v3/documents/a"))
    assert result.status_code == 502
    assert result.body == b'{"error":"upstream_response_invalid"}'


def test_cli_refuses_unverified_pin_before_listening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from benchmarks.memorybench import recording_proxy
    from benchmarks.memorybench.setup import SetupVerificationError

    monkeypatch.setattr(recording_proxy.setup, "verify_from_environment", lambda: (_ for _ in ()).throw(SetupVerificationError("no")))
    assert recording_proxy.main([
        "--upstream-base-url", "http://127.0.0.1:8765",
        "--output-dir", str(tmp_path / "recording"),
    ]) == 2


def test_cli_refuses_non_loopback_or_unsafe_upstream(tmp_path: Path) -> None:
    from benchmarks.memorybench.recording_proxy import ConfigurationError, validate_proxy_configuration

    with pytest.raises(ConfigurationError, match="loopback"):
        validate_proxy_configuration("0.0.0.0", "https://api.example.invalid")
    with pytest.raises(ConfigurationError, match="unsafe_upstream"):
        validate_proxy_configuration("127.0.0.1", "http://api.example.invalid")
    with pytest.raises(ConfigurationError, match="unsafe_upstream"):
        validate_proxy_configuration("127.0.0.1", "https://user:pass@api.example.invalid")


def test_cli_emits_one_machine_readable_ready_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from benchmarks.memorybench import recording_proxy

    class FakeProxy:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.writer = type("Writer", (), {"finalize": lambda self: None})()

        async def start(self, host: str, port: int) -> str:
            return "http://127.0.0.1:4567"

        async def close(self) -> None:
            return None

    monkeypatch.setattr(recording_proxy.setup, "verify_from_environment", lambda: None)
    monkeypatch.setattr(recording_proxy, "verified_pins_from_environment", lambda: PINS)
    monkeypatch.setattr(recording_proxy, "RecordingProxy", FakeProxy)
    monkeypatch.setattr(recording_proxy, "_wait_for_shutdown", lambda: None)
    assert recording_proxy.main([
        "--upstream-base-url", "http://127.0.0.1:8765",
        "--output-dir", str(tmp_path / "recording"),
    ]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [json.dumps({"event": "ready", "base_url": "http://127.0.0.1:4567", "environment": "SUPERMEMORY_BASE_URL", "latency_publishable": False}, separators=(",", ":"))]


def test_clean_shutdown_finalizes_recomputed_manifest(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    async def exercise() -> None:
        proxy = _proxy(tmp_path)
        await proxy.start("127.0.0.1", 0)
        await proxy.handle("GET", "/v3/documents/a")
        await proxy.close()

    _run(exercise())
    assert validate_recording(tmp_path / "recording", expected_pins=PINS).status == "complete"


def test_audit_records_active_environment_seam_and_shared_get_resource() -> None:
    audit = Path("benchmarks/memorybench/audit/supermemory-provider-audit.md").read_text(encoding="utf-8")
    assert "SUPERMEMORY_BASE_URL is the no-provider-patch interception seam" in audit
    assert "same `/v3/documents/:id` resource" in audit
    assert "cannot infer whether byte-identical GET attempts" in audit


def test_hop_by_hop_headers_and_loopback_host_are_not_forwarded(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return _response(request)

    result = _run(_proxy(tmp_path, handler).handle(
        "POST", "/v3/documents", headers=[
            ("content-type", "application/json"), ("host", "127.0.0.1:9999"),
            ("connection", "x-remove"), ("x-remove", "value"), ("keep-alive", "timeout=5"),
            ("authorization", "Bearer retained"),
        ], body=b"{}",
    ))
    assert result.status_code == 200
    assert "x-remove" not in seen and "keep-alive" not in seen
    assert seen["host"] == "127.0.0.1:8765"
    assert seen["authorization"] == "Bearer retained"


def test_validator_requires_independently_expected_pins(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    forged = dict(PINS, sdk_integrity="sha512-forged")
    with pytest.raises(RecordingError, match="provenance_mismatch"):
        validate_recording(proxy.writer.output_dir, expected_pins=forged)


def test_body_record_and_attempt_reject_impossible_cross_fields() -> None:
    from pydantic import ValidationError
    from benchmarks.memorybench.traffic import BodyRecord, HttpAttempt, HttpRequest, Timing

    with pytest.raises(ValidationError):
        BodyRecord(state="absent", declared_bytes=1, observed_bytes=1)
    with pytest.raises(ValidationError):
        HttpAttempt(
            attempt_ordinal=1, request=HttpRequest(method="GET", path="/", body=BodyRecord()),
            client_status=200, outcome="forwarded", timing=Timing(ms=1),
        )


def test_correction_real_bun_tuple_is_parsed_exactly() -> None:
    from benchmarks.memorybench.recording_proxy import extract_supermemory_sdk_pin

    bun_lock = b'''{
      "lockfileVersion": 1,
      "workspaces": {"": {"dependencies": {"supermemory": "^4.0.0"}}},
      "packages": {
        "supermemory": ["supermemory@4.0.0", "", {}, "sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ=="]
      }
    }'''

    assert extract_supermemory_sdk_pin(bun_lock) == (
        "4.0.0",
        PINS["sdk_integrity"],
    )


@pytest.mark.parametrize(
    "bun_lock",
    [
        b'{"workspaces":{"":{"dependencies":{"supermemory":"^4.0.0"}}},"packages":{}}',
        b'{"packages":{"supermemory":["supermemory@4.0.0","",{},"sha512-forged"]}}',
        b'{"packages":{"supermemory":["supermemory@4.0.1","",{},"sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ=="]}}',
    ],
)
def test_correction_bun_range_forged_integrity_or_wrong_version_is_refused(bun_lock: bytes) -> None:
    from benchmarks.memorybench.recording_proxy import ConfigurationError, extract_supermemory_sdk_pin

    with pytest.raises(ConfigurationError, match="verified_pins_unavailable"):
        extract_supermemory_sdk_pin(bun_lock)


def test_correction_manifest_contains_full_plan_and_thirty_second_timeout(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import RecordingWriter

    writer = RecordingWriter(
        tmp_path / "recording",
        pins=PINS,
        max_body_bytes=1234,
        upstream_timeout_seconds=30.0,
    )
    manifest = json.loads((writer.output_dir / "recording-manifest.json").read_text())

    assert manifest["artifact_type"] == "memorybench_http_recording"
    assert manifest["interception"] == {
        "environment_variable": "SUPERMEMORY_BASE_URL",
        "provider_modified": False,
        "sdk_modified": False,
    }
    assert manifest["limits"] == {
        "max_body_bytes": 1234,
        "upstream_timeout_seconds": 30.0,
    }
    assert manifest["timing_policy"] == {
        "latency_publishable": False,
        "reason": "host_unvalidated",
    }
    assert datetime.fromisoformat(manifest["started_at"]).tzinfo is not None
    assert manifest["completed_at"] is None


def test_correction_proxy_config_api_uses_planned_fields_and_defaults(tmp_path: Path) -> None:
    from benchmarks.memorybench.recording_proxy import ProxyConfig

    config = ProxyConfig(
        upstream_base_url="http://127.0.0.1:8765",
        output_dir=tmp_path / "recording",
    )

    assert config.listen_host == "127.0.0.1"
    assert config.listen_port == 0
    assert config.max_body_bytes == 4 * 1024 * 1024
    assert config.upstream_timeout_seconds == 30.0


def test_correction_httpx_timeout_is_manifested_and_applied(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.extensions["timeout"])
        return _response(request)

    from benchmarks.memorybench.recording_proxy import RecordingProxy
    from benchmarks.memorybench.traffic import RecordingWriter

    writer = RecordingWriter(
        tmp_path / "recording",
        pins=PINS,
        upstream_timeout_seconds=30.0,
    )
    proxy = RecordingProxy(
        "http://127.0.0.1:8765",
        writer,
        transport=httpx.MockTransport(handler),
        timeout_seconds=30.0,
    )
    _run(proxy.handle("GET", "/v3/documents/a"))

    assert observed == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


def test_correction_recorded_url_and_json_keys_hide_encoded_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(request)

    monkeypatch.setenv("SUPERMEMORY_API_KEY", "env-secret")
    proxy = _proxy(tmp_path, handler, secrets=("configured secret", "nested-secret"))
    body = b'{"env%2Dsecret":"value","configured%20secret":"value","safe":"env\\u002dsecret"}'
    path = "/v3/%65nv%2Dsecret/configured%20secret"
    query = "a%70i_key=env%2Dsecret&ordinary=configured%20secret"

    result = _run(proxy.handle(
        "POST",
        path,
        raw_query=query,
        headers=[("content-type", "application/json")],
        body=body,
    ))

    assert result.status_code == 200
    assert seen[0].url.raw_path == f"{path}?{query}".encode()
    assert seen[0].content == body
    artifact = (proxy.writer.output_dir / "http-attempts.jsonl").read_text()
    lowered = artifact.lower()
    for leaked in (
        "env-secret",
        "env%2dsecret",
        "configured secret",
        "configured%20secret",
    ):
        assert leaked not in lowered


def test_correction_validate_recording_requires_independent_provenance(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()

    with pytest.raises(TypeError):
        validate_recording(proxy.writer.output_dir)


def test_correction_response_hop_headers_and_content_length_are_normalized(tmp_path: Path) -> None:
    async def exercise() -> tuple[bytes, httpx.Response, bytes, int]:
        observed_request = bytearray()

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            observed_request.extend(head)
            content_length = 0
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1])
            if content_length:
                observed_request.extend(await reader.readexactly(content_length))
            response_body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 11\r\n"
                b"Connection: x-response-hop\r\n"
                b"X-Response-Hop: remove-me\r\n"
                b"Keep-Alive: timeout=5\r\n"
                b"\r\n" + response_body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        proxy = RecordingProxy(
            f"http://127.0.0.1:{upstream_port}",
            _writer(tmp_path),
            configured_secrets=("nested-secret",),
        )
        base_url = await proxy.start("127.0.0.1", 0)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{base_url}/v3%2Fdocuments?tag=one&tag=two",
                    headers=[
                        ("content-type", "application/json"),
                        ("connection", "x-request-hop"),
                        ("x-request-hop", "remove-me"),
                        ("keep-alive", "timeout=5"),
                        ("authorization", "Bearer retained"),
                    ],
                    content=b"{}",
                )
            attempts = (proxy.writer.output_dir / "http-attempts.jsonl").read_bytes()
        finally:
            await proxy.close()
            upstream_server.close()
            await upstream_server.wait_closed()
        return bytes(observed_request), response, attempts, upstream_port

    from benchmarks.memorybench.recording_proxy import RecordingProxy

    observed, response, attempts, upstream_port = _run(exercise())
    head, body = observed.split(b"\r\n\r\n", 1)
    lowered_head = head.lower()
    assert head.startswith(b"POST /v3%2Fdocuments?tag=one&tag=two HTTP/1.1")
    assert f"host: 127.0.0.1:{upstream_port}".encode() in lowered_head
    assert b"x-request-hop" not in lowered_head
    assert b"keep-alive" not in lowered_head
    assert b"authorization: bearer retained" in lowered_head
    assert body == b"{}"
    assert response.content == b'{"ok":true}'
    assert response.headers["content-length"] == "11"
    assert "x-response-hop" not in response.headers
    assert "keep-alive" not in response.headers
    assert attempts.count(b"\n") == 1


def test_correction_shutdown_drains_an_admitted_blocked_request(tmp_path: Path) -> None:
    async def exercise() -> tuple[bool, int]:
        admitted = asyncio.Event()
        release = asyncio.Event()

        from benchmarks.memorybench.recording_proxy import RecordingProxy

        proxy = RecordingProxy(
            "http://127.0.0.1:8765",
            _writer(tmp_path),
            transport=httpx.MockTransport(_response),
            configured_secrets=("nested-secret",),
        )
        original_handle = proxy.handle

        async def blocked_handle(*args, **kwargs):  # type: ignore[no-untyped-def]
            admitted.set()
            await release.wait()
            return await original_handle(*args, **kwargs)

        proxy.handle = blocked_handle  # type: ignore[method-assign]
        base_url = await proxy.start("127.0.0.1", 0)
        async with httpx.AsyncClient() as client:
            request_task = asyncio.create_task(client.get(f"{base_url}/v3/documents/a"))
            await admitted.wait()
            close_task = asyncio.create_task(proxy.close())
            await asyncio.sleep(0.05)
            closed_before_release = close_task.done()
            manifest_before_release = json.loads(
                (tmp_path / "recording" / "recording-manifest.json").read_text()
            )["status"]
            release.set()
            response = await request_task
            await close_task
        manifest = json.loads((tmp_path / "recording" / "recording-manifest.json").read_text())
        assert manifest_before_release == "recording"
        return closed_before_release, response.status_code if manifest["status"] == "complete" else 0

    closed_early, status = _run(exercise())
    assert closed_early is False
    assert status == 200


def test_correction_declared_oversize_is_rejected_before_h11_body_read(tmp_path: Path) -> None:
    async def exercise() -> tuple[bytes, dict[str, object]]:
        from benchmarks.memorybench.recording_proxy import RecordingProxy
        from benchmarks.memorybench.traffic import RecordingWriter

        writer = RecordingWriter(tmp_path / "recording", pins=PINS, max_body_bytes=4)
        proxy = RecordingProxy(
            "http://127.0.0.1:8765",
            writer,
            transport=httpx.MockTransport(_response),
            configured_secrets=("nested-secret",),
        )
        base_url = await proxy.start("127.0.0.1", 0)
        port = int(base_url.rsplit(":", 1)[1])
        reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        client_writer.write(
            b"POST /v3/documents HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n"
        )
        await client_writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=1)
        client_writer.close()
        await client_writer.wait_closed()
        await proxy.close()
        row = json.loads((writer.output_dir / "http-attempts.jsonl").read_text())
        return response, row

    response, row = _run(exercise())
    assert b"400" in response.split(b"\r\n", 1)[0]
    assert row["attempt_ordinal"] == 1
    assert row["outcome"] == "request_rejected"
    assert row["error_code"] == "body_too_large"
    assert row["request"]["body"] == {
        "state": "rejected",
        "declared_bytes": 100,
        "observed_bytes": 0,
        "sha256": None,
        "sanitized_json": None,
        "redaction_count": 0,
    }


def test_correction_malformed_framing_emits_one_stable_terminal_row(tmp_path: Path) -> None:
    async def exercise() -> tuple[bytes, list[dict[str, object]]]:
        from benchmarks.memorybench.recording_proxy import RecordingProxy

        proxy = RecordingProxy(
            "http://127.0.0.1:8765",
            _writer(tmp_path),
            transport=httpx.MockTransport(_response),
            configured_secrets=("nested-secret",),
        )
        base_url = await proxy.start("127.0.0.1", 0)
        port = int(base_url.rsplit(":", 1)[1])
        reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        client_writer.write(
            b"POST /v3/documents HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Length: 3\r\n\r\n{}"
        )
        await client_writer.drain()
        with contextlib.suppress(OSError):
            client_writer.write_eof()
        response = await asyncio.wait_for(reader.read(), timeout=1)
        client_writer.close()
        await client_writer.wait_closed()
        await proxy.close()
        rows = [json.loads(line) for line in (proxy.writer.output_dir / "http-attempts.jsonl").read_text().splitlines()]
        return response, rows

    response, rows = _run(exercise())
    assert b"400" in response.split(b"\r\n", 1)[0]
    assert len(rows) == 1
    assert rows[0]["attempt_ordinal"] == 1
    assert rows[0]["outcome"] == "request_rejected"
    assert rows[0]["error_code"] == "ambiguous_content_length"


@pytest.mark.parametrize(
    ("method", "status"),
    [("HEAD", 200), ("GET", 204), ("GET", 304)],
)
def test_correction_method_and_status_aware_empty_responses_are_legal(
    tmp_path: Path,
    method: str,
    status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-length": "123"} if method == "HEAD" or status == 304 else {}
        return httpx.Response(status, headers=headers, content=b"", request=request)

    result = _run(_proxy(tmp_path, handler).handle(method, "/v3/documents/a"))

    assert result.status_code == status
    row = json.loads((tmp_path / "recording" / "http-attempts.jsonl").read_text())
    assert row["outcome"] == "forwarded"
    assert row["response"]["body"]["state"] == "absent"


def test_correction_unexpected_empty_json_response_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"", request=request)

    result = _run(_proxy(tmp_path, handler).handle("GET", "/v3/documents/a"))

    assert result.status_code == 502
    assert result.body == b'{"error":"upstream_response_invalid"}'


def test_correction_body_and_attempt_cross_field_invariants_are_strict() -> None:
    from pydantic import ValidationError
    from benchmarks.memorybench.traffic import BodyRecord, HttpAttempt, HttpRequest, HttpResponse, Timing

    absent = BodyRecord(
        state="absent",
        declared_bytes=None,
        observed_bytes=0,
        sha256=None,
        sanitized_json=None,
        redaction_count=0,
    )
    request = HttpRequest(method="GET", path="/", body=absent)
    response = HttpResponse(status_code=200, body=absent)
    timing = Timing(ms=1)

    invalid_bodies = [
        dict(state="absent", declared_bytes=None, observed_bytes=1, sha256=None, sanitized_json=None, redaction_count=0),
        dict(state="json", declared_bytes=2, observed_bytes=2, sha256=None, sanitized_json={}, redaction_count=0),
        dict(state="json", declared_bytes=3, observed_bytes=2, sha256="a" * 64, sanitized_json={}, redaction_count=0),
        dict(state="rejected", declared_bytes=2, observed_bytes=2, sha256="a" * 64, sanitized_json=None, redaction_count=0),
    ]
    for values in invalid_bodies:
        with pytest.raises(ValidationError):
            BodyRecord(**values)
    assert BodyRecord(state="absent", declared_bytes=123).declared_bytes == 123

    invalid_attempts = [
        dict(request=request, response=None, upstream_status=None, client_status=200, outcome="forwarded", error_code=None),
        dict(request=request, response=response, upstream_status=200, client_status=200, outcome="forwarded", error_code="unexpected"),
        dict(request=request, response=response, upstream_status=201, client_status=200, outcome="forwarded", error_code=None),
        dict(request=request, response=response, upstream_status=200, client_status=400, outcome="request_rejected", error_code="invalid_json"),
        dict(request=request, response=None, upstream_status=None, client_status=502, outcome="upstream_error", error_code=None),
    ]
    for values in invalid_attempts:
        with pytest.raises(ValidationError):
            HttpAttempt(attempt_ordinal=1, started_at="2026-08-09T00:00:00+00:00", timing=timing, **values)


def test_correction_expected_cli_failures_are_generic_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from benchmarks.memorybench import recording_proxy

    monkeypatch.setattr(recording_proxy.setup, "verify_from_environment", lambda: None)
    secret_url = "https://user:secret@example.invalid/private"
    code = recording_proxy.main([
        "--upstream-base-url", secret_url,
        "--output-dir", str(tmp_path / "private-output"),
    ])

    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert captured.err == "recording_proxy_error: invalid_configuration\n"
    assert "secret" not in captured.err
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err


def test_correction_audit_pins_official_sdk_evidence_without_contradictory_todo() -> None:
    audit = Path("benchmarks/memorybench/audit/supermemory-provider-audit.md").read_text()

    assert '"supermemory@4.0.0", "", {}, "sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ=="' in audit
    assert "https://app.unpkg.com/supermemory@4.0.0/files/client.mjs" in audit
    assert "https://app.unpkg.com/supermemory@4.0.0/files/resources/documents.mjs" in audit
    assert "https://app.unpkg.com/supermemory@4.0.0/files/resources/memories.mjs" in audit
    assert "static source verification" in audit
    assert "no live SDK invocation" in audit
    assert "SDK parameter surface was not installed/verified" not in audit


def test_correction_admission_ordinal_precedes_body_completion(tmp_path: Path) -> None:
    async def exercise() -> list[dict[str, object]]:
        proxy = _proxy(tmp_path)
        base_url = await proxy.start("127.0.0.1", 0)
        port = int(base_url.rsplit(":", 1)[1])
        first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
        first_writer.write(
            b"POST /v3/documents/first HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n"
        )
        await first_writer.drain()
        await asyncio.sleep(0.05)

        async with httpx.AsyncClient() as client:
            second_response = await client.get(f"{base_url}/v3/documents/second")
        assert second_response.status_code == 200

        first_writer.write(b"{}")
        await first_writer.drain()
        first_response = await asyncio.wait_for(first_reader.read(), timeout=1)
        assert b"200" in first_response.split(b"\r\n", 1)[0]
        first_writer.close()
        await first_writer.wait_closed()
        await proxy.close()
        return [
            json.loads(line)
            for line in (proxy.writer.output_dir / "http-attempts.jsonl").read_text().splitlines()
        ]

    rows = _run(exercise())
    assert [row["attempt_ordinal"] for row in rows] == [2, 1]
    by_ordinal = {row["attempt_ordinal"]: row for row in rows}
    assert by_ordinal[1]["request"]["path"] == "/v3/documents/first"
    assert by_ordinal[2]["request"]["path"] == "/v3/documents/second"


def test_correction_rejected_raw_body_is_never_stored(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path, secrets=("raw-secret", "nested-secret"))
    malformed = b'{"password":"raw-secret"'

    result = _run(proxy.handle(
        "POST",
        "/v3/documents",
        headers=[("content-type", "application/json")],
        body=malformed,
    ))

    assert result.status_code == 400
    artifact = (proxy.writer.output_dir / "http-attempts.jsonl").read_text()
    assert "raw-secret" not in artifact
    row = json.loads(artifact)
    assert row["request"]["body"] == {
        "state": "rejected",
        "declared_bytes": None,
        "observed_bytes": len(malformed),
        "sha256": None,
        "sanitized_json": None,
        "redaction_count": 0,
    }


def test_correction_writer_serializes_concurrent_rows(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import (
        BodyRecord,
        HttpAttempt,
        HttpRequest,
        HttpResponse,
        RecordingWriter,
        Timing,
        validate_recording,
    )

    writer = RecordingWriter(tmp_path / "recording", pins=PINS)
    empty = BodyRecord()

    def write_row(ordinal: int) -> None:
        writer.record(HttpAttempt(
            attempt_ordinal=ordinal,
            started_at=writer.manifest.started_at,
            request=HttpRequest(method="GET", path=f"/{ordinal}", body=empty),
            response=HttpResponse(status_code=200, body=empty),
            upstream_status=200,
            client_status=200,
            outcome="forwarded",
            timing=Timing(ms=1),
        ))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write_row, range(1, 65)))
    writer.finalize()

    assert validate_recording(writer.output_dir, expected_pins=PINS).attempt_count == 64


def test_correction_writer_startup_and_shutdown_cli_errors_are_generic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from benchmarks.memorybench import recording_proxy

    monkeypatch.setattr(recording_proxy.setup, "verify_from_environment", lambda: None)
    monkeypatch.setattr(recording_proxy, "verified_pins_from_environment", lambda: PINS)
    arguments = lambda output: [
        "--upstream-base-url", "http://127.0.0.1:8765",
        "--output-dir", str(output),
    ]

    existing = tmp_path / "private-existing"
    existing.mkdir()
    assert recording_proxy.main(arguments(existing)) == 2
    writer_error = capsys.readouterr()
    assert writer_error.out == ""
    assert writer_error.err == "recording_proxy_error: existing_output\n"

    async def failed_start_server(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("startup leaked-secret")

    monkeypatch.setattr(recording_proxy.asyncio, "start_server", failed_start_server)
    assert recording_proxy.main(arguments(tmp_path / "private-startup")) == 2
    startup_error = capsys.readouterr()
    assert startup_error.out == ""
    assert startup_error.err == "recording_proxy_error: startup_failed\n"

    class FailingCloseProxy:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def start(self, host: str, port: int) -> str:
            return "http://127.0.0.1:4567"

        async def close(self) -> None:
            raise recording_proxy.ShutdownError("shutdown leaked-secret")

        async def abort(self) -> None:
            return None

    monkeypatch.setattr(recording_proxy, "RecordingProxy", FailingCloseProxy)
    monkeypatch.setattr(recording_proxy, "_wait_for_shutdown", lambda: None)
    assert recording_proxy.main(arguments(tmp_path / "private-shutdown")) == 2
    shutdown_error = capsys.readouterr()
    assert shutdown_error.out.splitlines() == [
        '{"event":"ready","base_url":"http://127.0.0.1:4567","environment":"SUPERMEMORY_BASE_URL","latency_publishable":false}'
    ]
    assert shutdown_error.err == "recording_proxy_error: shutdown_failed\n"

    combined = writer_error.err + startup_error.err + shutdown_error.err
    assert "leaked-secret" not in combined
    assert str(tmp_path) not in combined
    assert "Traceback" not in combined


def test_feedback2_encoded_plus_secrets_are_redacted_on_every_recorded_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            request=request,
        )

    monkeypatch.setenv("SUPERMEMORY_API_KEY", "env+secret")
    proxy = _proxy(tmp_path, handler, secrets=("abc+def",))
    path = "/v3/abc%2Bdef/env%252Bsecret"
    query = "abc%2Bdef=env%2Bsecret&safe=abc%252Bdef&env%252Bsecret=value"
    body = b'{"abc%2Bdef":"env%2Bsecret","nested":{"safe":"abc%252Bdef"},"env%252Bsecret":"ok"}'

    result = _run(proxy.handle(
        "POST",
        path,
        raw_query=query,
        headers=[
            ("content-type", "application/json"),
            ("x-marker", "abc%2Bdef"),
            ("x-environment", "env%252Bsecret"),
        ],
        body=body,
    ))

    assert result.status_code == 200
    assert seen[0].url.raw_path == f"{path}?{query}".encode()
    assert seen[0].content == body
    assert seen[0].headers["x-marker"] == "abc%2Bdef"
    artifact = (proxy.writer.output_dir / "http-attempts.jsonl").read_text().lower()
    for leaked in (
        "abc+def",
        "abc%2bdef",
        "abc%252bdef",
        "env+secret",
        "env%2bsecret",
        "env%252bsecret",
    ):
        assert leaked not in artifact


def test_feedback2_real_listener_forwards_chunked_json_without_invented_negotiation(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[bytes, bytes]:
        from benchmarks.memorybench.recording_proxy import RecordingProxy

        observed_head = bytearray()

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            observed_head.extend(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
                b"b\r\n{\"ok\":true}\r\n0\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        proxy = RecordingProxy(f"http://127.0.0.1:{upstream_port}", _writer(tmp_path))
        base_url = await proxy.start("127.0.0.1", 0)
        proxy_port = int(base_url.rsplit(":", 1)[1])
        try:
            reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            client_writer.write(
                b"GET /v3/documents/a HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"
            )
            await client_writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            client_writer.close()
            await client_writer.wait_closed()
        finally:
            await proxy.close()
            upstream_server.close()
            await upstream_server.wait_closed()
        return bytes(observed_head), response

    observed, response = _run(exercise())
    lowered = observed.lower()
    assert b"accept:" not in lowered
    assert b"accept-encoding:" not in lowered
    response_head, response_body = response.split(b"\r\n\r\n", 1)
    assert b" 200 " in response_head.split(b"\r\n", 1)[0]
    assert b"transfer-encoding" not in response_head.lower()
    assert b"content-length: 11" in response_head.lower()
    assert response_body == b'{"ok":true}'


@pytest.mark.parametrize("max_body_bytes", [4 * 1024 * 1024, 64])
def test_feedback2_real_listener_handles_negotiated_gzip_with_decoded_bound(
    tmp_path: Path,
    max_body_bytes: int,
) -> None:
    decoded = b'{"ok":true}' if max_body_bytes > 64 else json.dumps({"value": "x" * 256}).encode()
    compressed = gzip.compress(decoded, mtime=0)
    assert len(compressed) < max_body_bytes

    async def exercise() -> tuple[bytes, httpx.Response]:
        from benchmarks.memorybench.recording_proxy import RecordingProxy
        from benchmarks.memorybench.traffic import RecordingWriter

        observed_head = bytearray()

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            observed_head.extend(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Encoding: gzip\r\n"
                + f"Content-Length: {len(compressed)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + compressed
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        writer = RecordingWriter(
            tmp_path / "recording",
            pins=PINS,
            max_body_bytes=max_body_bytes,
        )
        proxy = RecordingProxy(f"http://127.0.0.1:{upstream_port}", writer)
        base_url = await proxy.start("127.0.0.1", 0)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{base_url}/v3/documents/a",
                    headers={"accept-encoding": "gzip"},
                )
        finally:
            await proxy.close()
            upstream_server.close()
            await upstream_server.wait_closed()
        return bytes(observed_head), response

    observed, response = _run(exercise())
    assert b"accept-encoding: gzip" in observed.lower()
    if len(decoded) <= max_body_bytes:
        assert response.status_code == 200
        assert response.content == decoded
        assert response.headers["content-encoding"] == "gzip"
        assert response.headers["content-length"] == str(len(compressed))
    else:
        assert response.status_code == 502
        assert response.content == b'{"error":"upstream_response_invalid"}'


def test_feedback2_real_listener_rejects_equal_duplicate_content_length_once(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request)

    async def exercise() -> tuple[bytes, list[dict[str, object]]]:
        from benchmarks.memorybench.recording_proxy import RecordingProxy

        proxy = RecordingProxy(
            "http://127.0.0.1:8765",
            _writer(tmp_path),
            transport=httpx.MockTransport(handler),
        )
        base_url = await proxy.start("127.0.0.1", 0)
        port = int(base_url.rsplit(":", 1)[1])
        reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        client_writer.write(
            b"POST /v3/documents HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        await client_writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=1)
        client_writer.close()
        await client_writer.wait_closed()
        await proxy.close()
        rows = [
            json.loads(line)
            for line in (proxy.writer.output_dir / "http-attempts.jsonl").read_text().splitlines()
        ]
        return response, rows

    response, rows = _run(exercise())
    assert calls == 0
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert len(rows) == 1
    assert rows[0]["error_code"] == "ambiguous_content_length"


def test_feedback2_exported_schemas_reject_impossible_cross_fields() -> None:
    from jsonschema import Draft202012Validator
    from benchmarks.memorybench.traffic import (
        BodyRecord,
        HarnessPins,
        HttpAttempt,
        HttpRequest,
        HttpResponse,
        RecordingLimits,
        RecordingManifest,
        Timing,
    )

    absent = BodyRecord()
    attempt = HttpAttempt(
        attempt_ordinal=1,
        started_at=datetime.fromisoformat("2026-08-09T00:00:00+00:00"),
        request=HttpRequest(method="GET", path="/", body=absent),
        response=HttpResponse(status_code=200, body=absent),
        upstream_status=200,
        client_status=200,
        outcome="forwarded",
        timing=Timing(ms=1),
    ).model_dump(mode="json")
    impossible_body = json.loads(json.dumps(attempt))
    impossible_body["request"]["body"]["observed_bytes"] = 1
    impossible_attempt = json.loads(json.dumps(attempt))
    impossible_attempt["response"] = None
    manifest = RecordingManifest(
        status="recording",
        started_at=datetime.fromisoformat("2026-08-09T00:00:00+00:00"),
        pins=HarnessPins.model_validate(PINS),
        limits=RecordingLimits(max_body_bytes=100, upstream_timeout_seconds=30.0),
    ).model_dump(mode="json")
    impossible_manifest = dict(manifest, status="complete")

    cases = [
        (HttpAttempt.model_json_schema(), impossible_body),
        (HttpAttempt.model_json_schema(), impossible_attempt),
        (RecordingManifest.model_json_schema(), impossible_manifest),
    ]
    for schema, instance in cases:
        errors = list(Draft202012Validator(schema).iter_errors(instance))
        assert errors, instance


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("body_limit", "attempt_body_exceeds_limit"),
        ("timestamp", "attempt_timestamp_out_of_bounds"),
        ("header_body", "attempt_header_body_mismatch"),
    ],
)
def test_feedback2_validator_correlates_rows_with_manifest(
    tmp_path: Path,
    case: str,
    expected_error: str,
) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    attempts_path = proxy.writer.output_dir / "http-attempts.jsonl"
    manifest_path = proxy.writer.output_dir / "recording-manifest.json"
    row = json.loads(attempts_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    if case == "body_limit":
        count = manifest["limits"]["max_body_bytes"] + 1
        row["request"]["headers"] = [
            {"name": "content-type", "value": "application/json"},
            {"name": "content-length", "value": str(count)},
        ]
        row["request"]["body"] = {
            "state": "json",
            "declared_bytes": count,
            "observed_bytes": count,
            "sha256": "0" * 64,
            "sanitized_json": {},
            "redaction_count": 0,
        }
    elif case == "timestamp":
        row["started_at"] = (
            datetime.fromisoformat(manifest["started_at"]) - timedelta(seconds=1)
        ).isoformat()
    else:
        row["request"]["headers"] = [{"name": "content-length", "value": "1"}]
    attempts_data = json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    attempts_path.write_bytes(attempts_data)
    manifest["attempts_sha256"] = hashlib.sha256(attempts_data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RecordingError, match=expected_error):
        validate_recording(proxy.writer.output_dir, expected_pins=PINS)


@pytest.mark.parametrize(("method", "status"), [("HEAD", 200), ("GET", 304)])
def test_feedback2_real_listener_preserves_empty_response_representation_length(
    tmp_path: Path,
    method: str,
    status: int,
) -> None:
    async def exercise() -> tuple[httpx.Response, dict[str, object]]:
        from benchmarks.memorybench.recording_proxy import RecordingProxy

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                f"HTTP/1.1 {status} Empty\r\n".encode()
                + b"Content-Length: 123\r\n"
                + b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        proxy = RecordingProxy(f"http://127.0.0.1:{upstream_port}", _writer(tmp_path))
        base_url = await proxy.start("127.0.0.1", 0)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(method, f"{base_url}/v3/documents/a")
        finally:
            await proxy.close()
            upstream_server.close()
            await upstream_server.wait_closed()
        row = json.loads((proxy.writer.output_dir / "http-attempts.jsonl").read_text())
        return response, row

    response, row = _run(exercise())
    assert response.status_code == status
    assert response.content == b""
    assert response.headers["content-length"] == "123"
    assert row["response"]["body"] == {
        "state": "absent",
        "declared_bytes": 123,
        "observed_bytes": 0,
        "sha256": None,
        "sanitized_json": None,
        "redaction_count": 0,
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_feedback2_non_finite_artifact_and_timeout_values_are_rejected(
    tmp_path: Path,
    value: float,
) -> None:
    from pydantic import ValidationError
    from benchmarks.memorybench.recording_proxy import ProxyConfig
    from benchmarks.memorybench.traffic import BodyRecord, RecordingLimits, Timing, canonical_bytes

    with pytest.raises(ValidationError):
        Timing(ms=value)
    with pytest.raises(ValidationError):
        RecordingLimits(max_body_bytes=1, upstream_timeout_seconds=value)
    with pytest.raises(ValidationError):
        BodyRecord(
            state="json",
            observed_bytes=1,
            sha256="0" * 64,
            sanitized_json={"value": value},
        )
    with pytest.raises(ValidationError):
        ProxyConfig(
            upstream_base_url="http://127.0.0.1:8765",
            output_dir=tmp_path / "recording",
            upstream_timeout_seconds=value,
        )
    with pytest.raises(ValueError):
        canonical_bytes(Timing.model_construct(ms=value))


@pytest.mark.parametrize(
    "bun_lock",
    [
        b'{"unrelated":{"packages":{"supermemory":["supermemory@4.0.0","",{},"sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ=="]}},"packages":{}}',
        b'{"packages":{"nested":{"supermemory":["supermemory@4.0.0","",{},"sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ=="]}}}',
        b'{"packages":{},"packages":{"supermemory":["supermemory@4.0.0","",{},"sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ=="]}}',
    ],
)
def test_feedback2_bun_parser_rejects_unrelated_nested_or_duplicate_packages(
    bun_lock: bytes,
) -> None:
    from benchmarks.memorybench.recording_proxy import ConfigurationError, extract_supermemory_sdk_pin

    with pytest.raises(ConfigurationError, match="verified_pins_unavailable"):
        extract_supermemory_sdk_pin(bun_lock)


def test_feedback2_bun_parser_accepts_jsonc_and_trailing_commas() -> None:
    from benchmarks.memorybench.recording_proxy import extract_supermemory_sdk_pin

    bun_lock = b'''{
      // Bun's text lockfile is JSONC.
      "packages": {
        "supermemory": [
          "supermemory@4.0.0",
          "",
          {},
          "sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXcSUBVBNYPPbhAQMF6GYQ==",
        ],
      },
    }'''

    assert extract_supermemory_sdk_pin(bun_lock) == ("4.0.0", PINS["sdk_integrity"])
