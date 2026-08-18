"""Offline contract tests for the MemoryBench Supermemory traffic recorder."""

from __future__ import annotations

import asyncio
import contextlib
import concurrent.futures
import gzip
import hashlib
import json
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

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


def _absent_body(*, declared_bytes: int | None = None):
    from benchmarks.memorybench.traffic import BodyRecord

    return BodyRecord.model_validate({
        "state": "absent",
        "declared_bytes": declared_bytes,
    })


def _timing(ms: float = 1):
    from benchmarks.memorybench.traffic import Timing

    return Timing(
        ms=ms,
        latency_publishable=False,
        reason="host_unvalidated",
    )


async def _feedback3_real_response_round_trip(
    tmp_path: Path,
    *,
    method: str,
    upstream_response: bytes,
    max_body_bytes: int = 4 * 1024 * 1024,
    request_headers: dict[str, str] | None = None,
    raw_downstream: bool = False,
) -> tuple[httpx.Response | bytes, dict[str, object], Path, bytes]:
    from benchmarks.memorybench.recording_proxy import RecordingProxy
    from benchmarks.memorybench.traffic import RecordingWriter

    observed_head = bytearray()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        observed_head.extend(await reader.readuntil(b"\r\n\r\n"))
        writer.write(upstream_response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    recording = tmp_path / "recording"
    writer = RecordingWriter(recording, pins=PINS, max_body_bytes=max_body_bytes)
    proxy = RecordingProxy(f"http://127.0.0.1:{upstream_port}", writer)
    base_url = await proxy.start("127.0.0.1", 0)
    try:
        if raw_downstream:
            proxy_port = int(base_url.rsplit(":", 1)[1])
            reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            request_head = [
                f"{method} /v3/documents/a HTTP/1.1\r\n".encode(),
                b"Host: 127.0.0.1\r\n",
            ]
            request_head.extend(
                f"{name}: {value}\r\n".encode()
                for name, value in (request_headers or {}).items()
            )
            request_head.append(b"Connection: close\r\n\r\n")
            client_writer.write(b"".join(request_head))
            await client_writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            client_writer.close()
            await client_writer.wait_closed()
        else:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    f"{base_url}/v3/documents/a",
                    headers=request_headers,
                )
    finally:
        await proxy.close()
        upstream_server.close()
        await upstream_server.wait_closed()
    row = json.loads((recording / "http-attempts.jsonl").read_text())
    return response, row, recording, bytes(observed_head)


async def _feedback4_real_request_round_trip(
    tmp_path: Path,
    *,
    body: bytes,
) -> tuple[bytes, list[dict[str, object]], Path, int]:
    """Exercise request parsing through h11 and report upstream admissions."""

    from benchmarks.memorybench.recording_proxy import RecordingProxy
    from benchmarks.memorybench.traffic import RecordingWriter

    upstream_calls = 0

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal upstream_calls
        upstream_calls += 1
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 204 No Content\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    recording = tmp_path / "recording"
    proxy = RecordingProxy(
        f"http://127.0.0.1:{upstream_port}",
        RecordingWriter(recording, pins=PINS),
    )
    base_url = await proxy.start("127.0.0.1", 0)
    proxy_port = int(base_url.rsplit(":", 1)[1])
    try:
        reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(
            b"POST /v3/documents HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await client_writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=2)
        client_writer.close()
        await client_writer.wait_closed()
    finally:
        await proxy.close()
        upstream_server.close()
        await upstream_server.wait_closed()
    rows = [json.loads(line) for line in (recording / "http-attempts.jsonl").read_text().splitlines()]
    return response, rows, recording, upstream_calls


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
    """The committed schemas must be exactly what the models export.

    Compared per file, and reported as a location rather than as two byte
    blobs. `assert fresh == committed` over the whole mapping states the same
    contract, but a mismatch hands pytest a 557 KB value to diff, and
    rendering that diff outran the 60s per-test timeout -- which
    `timeout_method = "thread"` enforces by killing the process, so the shard
    wrote no junit at all and every other test on it went unreported. A stale
    schema should cost one line, not a lane's worth of reporting.
    """
    from benchmarks.memorybench.traffic import export_json_schemas

    fresh = {path.name: path.read_bytes() for path in export_json_schemas(tmp_path)}
    committed = {
        path.name: path.read_bytes()
        for path in Path("benchmarks/memorybench/schema").glob("*.schema.json")
    }

    assert sorted(fresh) == sorted(committed), (
        "exported and committed schema sets differ"
    )

    for name in sorted(fresh):
        exported, stored = fresh[name], committed[name]
        if exported == stored:
            continue
        offset = next(
            (i for i, (a, b) in enumerate(zip(exported, stored)) if a != b),
            min(len(exported), len(stored)),
        )
        pytest.fail(
            f"{name} is stale -- regenerate it from the models.\n"
            f"  exported {len(exported)} bytes, committed {len(stored)} bytes\n"
            f"  first difference at byte {offset}\n"
            f"  exported:  {exported[offset : offset + 60]!r}\n"
            f"  committed: {stored[offset : offset + 60]!r}"
        )


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
        "wire_bytes": len(body),
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
    from benchmarks.memorybench.traffic import BodyRecord, HttpAttempt, HttpRequest

    with pytest.raises(ValidationError):
        BodyRecord.model_validate({"state": "absent", "declared_bytes": 1, "observed_bytes": 1})
    with pytest.raises(ValidationError):
        HttpAttempt(
            schema_version=1,
            attempt_ordinal=1,
            started_at="2026-08-09T00:00:00+00:00",
            request=HttpRequest(method="GET", path="/", query=[], headers=[], body=_absent_body()),
            response=None,
            upstream_status=None,
            client_status=200,
            outcome="forwarded",
            error_code=None,
            timing=_timing(),
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
    from benchmarks.memorybench.traffic import BodyRecord, HttpAttempt, HttpRequest, HttpResponse

    absent = _absent_body()
    request = HttpRequest(method="GET", path="/", query=[], headers=[], body=absent)
    response = HttpResponse(status_code=200, headers=[], body=absent)
    timing = _timing()

    invalid_bodies = [
        dict(state="absent", declared_bytes=None, observed_bytes=1),
        dict(state="json", wire_bytes=2, sanitized_json={}, redaction_count=0),
        dict(state="json", declared_bytes=3, observed_bytes=2, sha256="a" * 64, sanitized_json={}, redaction_count=0),
        dict(state="rejected", declared_bytes=2, observed_bytes=2, sha256="a" * 64),
    ]
    for values in invalid_bodies:
        with pytest.raises(ValidationError):
            BodyRecord.model_validate(values)
    assert _absent_body(declared_bytes=123).declared_bytes == 123

    invalid_attempts = [
        dict(request=request, response=None, upstream_status=None, client_status=200, outcome="forwarded", error_code=None),
        dict(request=request, response=response, upstream_status=200, client_status=200, outcome="forwarded", error_code="unexpected"),
        dict(request=request, response=response, upstream_status=201, client_status=200, outcome="forwarded", error_code=None),
        dict(request=request, response=response, upstream_status=200, client_status=400, outcome="request_rejected", error_code="invalid_json"),
        dict(request=request, response=None, upstream_status=None, client_status=502, outcome="upstream_error", error_code=None),
    ]
    for values in invalid_attempts:
        with pytest.raises(ValidationError):
            HttpAttempt(
                schema_version=1,
                attempt_ordinal=1,
                started_at="2026-08-09T00:00:00+00:00",
                timing=timing,
                **values,
            )


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
    }


def test_correction_writer_serializes_concurrent_rows(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import (
        HttpAttempt,
        HttpRequest,
        HttpResponse,
        RecordingWriter,
        validate_recording,
    )

    writer = RecordingWriter(tmp_path / "recording", pins=PINS)
    empty = _absent_body()

    def write_row(ordinal: int) -> None:
        writer.record(HttpAttempt(
            schema_version=1,
            attempt_ordinal=ordinal,
            started_at=writer.manifest.started_at,
            request=HttpRequest(method="GET", path=f"/{ordinal}", query=[], headers=[], body=empty),
            response=HttpResponse(status_code=204, headers=[], body=empty),
            upstream_status=204,
            client_status=204,
            outcome="forwarded",
            error_code=None,
            timing=_timing(),
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
        HarnessPins,
        HttpAttempt,
        HttpRequest,
        HttpResponse,
        InterceptionDeclaration,
        RecordingLimits,
        RecordingManifest,
        TimingPolicy,
    )

    absent = _absent_body()
    attempt = HttpAttempt(
        schema_version=1,
        attempt_ordinal=1,
        started_at=datetime.fromisoformat("2026-08-09T00:00:00+00:00"),
        request=HttpRequest(method="GET", path="/", query=[], headers=[], body=absent),
        response=HttpResponse(status_code=200, headers=[], body=absent),
        upstream_status=200,
        client_status=200,
        outcome="forwarded",
        error_code=None,
        timing=_timing(),
    ).model_dump(mode="json")
    impossible_body = json.loads(json.dumps(attempt))
    impossible_body["request"]["body"]["observed_bytes"] = 1
    impossible_attempt = json.loads(json.dumps(attempt))
    impossible_attempt["response"] = None
    manifest = RecordingManifest(
        artifact_type="memorybench_http_recording",
        schema_version=1,
        status="recording",
        started_at=datetime.fromisoformat("2026-08-09T00:00:00+00:00"),
        interception=InterceptionDeclaration(
            environment_variable="SUPERMEMORY_BASE_URL",
            provider_modified=False,
            sdk_modified=False,
        ),
        pins=HarnessPins.model_validate(PINS),
        limits=RecordingLimits(max_body_bytes=100, upstream_timeout_seconds=30.0),
        timing_policy=TimingPolicy(
            latency_publishable=False,
            reason="host_unvalidated",
        ),
        attempts_file="http-attempts.jsonl",
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
            "wire_bytes": count,
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
        Timing(ms=value, latency_publishable=False, reason="host_unvalidated")
    with pytest.raises(ValidationError):
        RecordingLimits(max_body_bytes=1, upstream_timeout_seconds=value)
    with pytest.raises(ValidationError):
        BodyRecord.model_validate({
            "state": "json",
            "wire_bytes": 1,
            "sha256": "0" * 64,
            "sanitized_json": {"value": value},
            "redaction_count": 0,
        })
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


def test_feedback3_deeply_encoded_configured_and_environment_secrets_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def encode_layers(value: str, count: int) -> list[str]:
        forms = [value]
        for _ in range(count):
            forms.append(quote(forms[-1], safe=""))
        return forms

    configured_forms = encode_layers("configured+secret", 40)
    environment_forms = encode_layers("environment+secret", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            request=request,
        )

    monkeypatch.setenv("SUPERMEMORY_API_KEY", environment_forms[0])
    proxy = _proxy(tmp_path, handler, secrets=(configured_forms[0],))
    path = f"/v3/{configured_forms[4]}/{environment_forms[4]}/{configured_forms[40]}"
    query = (
        f"{configured_forms[4]}={environment_forms[4]}"
        f"&{environment_forms[4]}={configured_forms[4]}"
    )
    body = json.dumps({
        configured_forms[4]: environment_forms[4],
        environment_forms[4]: configured_forms[4],
    }, separators=(",", ":")).encode()

    result = _run(proxy.handle(
        "POST",
        path,
        raw_query=query,
        headers=[
            ("content-type", "application/json"),
            ("x-configured", configured_forms[4]),
            ("x-environment", environment_forms[4]),
        ],
        body=body,
    ))

    assert result.status_code == 200
    assert seen[0].url.raw_path == f"{path}?{query}".encode()
    assert seen[0].content == body
    artifact = (proxy.writer.output_dir / "http-attempts.jsonl").read_bytes().lower()
    for reversible in (*configured_forms, *environment_forms):
        assert reversible.encode().lower() not in artifact


@pytest.mark.parametrize(
    "field_path",
    [
        ("artifact_type",),
        ("schema_version",),
        ("interception",),
        ("timing_policy",),
        ("attempts_file",),
        ("completed_at",),
        ("attempt_count",),
        ("attempts_sha256",),
        ("interception", "environment_variable"),
        ("interception", "provider_modified"),
        ("interception", "sdk_modified"),
        ("timing_policy", "latency_publishable"),
        ("timing_policy", "reason"),
    ],
)
def test_feedback3_manifest_consumption_never_fabricates_missing_claims(
    tmp_path: Path,
    field_path: tuple[str, ...],
) -> None:
    from jsonschema import Draft202012Validator
    from benchmarks.memorybench.traffic import RecordingError, RecordingManifest, validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    manifest_path = proxy.writer.output_dir / "recording-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    target = manifest
    for component in field_path[:-1]:
        target = target[component]
    del target[field_path[-1]]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RecordingError, match="manifest_invalid"):
        validate_recording(proxy.writer.output_dir, expected_pins=PINS)
    assert list(Draft202012Validator(RecordingManifest.model_json_schema()).iter_errors(manifest))


@pytest.mark.parametrize(
    "case",
    [
        "client_status_mismatch",
        "upstream_status_mismatch",
        "response_status_mismatch",
        "forwarded_rejected_request",
        "request_rejected_absent_body",
        "response_rejected_absent_body",
        "response_rejected_status_mismatch",
        "response_rejected_rejected_request",
        "upstream_error_rejected_request",
        "request_rejected_missing_error",
        "response_rejected_missing_error",
        "upstream_error_missing_error",
    ],
)
def test_feedback3_http_attempt_schema_matches_model_cross_field_rejections(case: str) -> None:
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    from benchmarks.memorybench.traffic import HttpAttempt, HttpRequest, HttpResponse

    absent = _absent_body()
    base = HttpAttempt(
        schema_version=1,
        attempt_ordinal=1,
        started_at=datetime.fromisoformat("2026-08-09T00:00:00+00:00"),
        request=HttpRequest(method="GET", path="/", query=[], headers=[], body=absent),
        response=HttpResponse(status_code=200, headers=[], body=absent),
        upstream_status=200,
        client_status=200,
        outcome="forwarded",
        error_code=None,
        timing=_timing(),
    ).model_dump(mode="json")
    instance = json.loads(json.dumps(base))
    if case == "client_status_mismatch":
        instance["client_status"] = 201
    elif case == "upstream_status_mismatch":
        instance["upstream_status"] = 201
    elif case == "response_status_mismatch":
        instance["response"]["status_code"] = 201
    elif case == "forwarded_rejected_request":
        instance["request"]["body"] = {
            "state": "rejected",
            "declared_bytes": None,
            "observed_bytes": 0,
        }
    elif case == "request_rejected_absent_body":
        instance.update(
            response=None,
            upstream_status=None,
            client_status=400,
            outcome="request_rejected",
            error_code="invalid_json",
        )
    elif case == "response_rejected_absent_body":
        instance.update(
            client_status=502,
            outcome="response_rejected",
            error_code="upstream_response_invalid",
        )
    elif case == "response_rejected_status_mismatch":
        instance["response"]["body"] = {
            "state": "rejected",
            "declared_bytes": None,
            "observed_bytes": 0,
        }
        instance.update(
            upstream_status=201,
            client_status=502,
            outcome="response_rejected",
            error_code="upstream_response_invalid",
        )
    elif case == "response_rejected_rejected_request":
        rejected = {
            "state": "rejected",
            "declared_bytes": None,
            "observed_bytes": 0,
        }
        instance["request"]["body"] = rejected
        instance["response"]["body"] = rejected
        instance.update(
            client_status=502,
            outcome="response_rejected",
            error_code="upstream_response_invalid",
        )
    elif case == "upstream_error_rejected_request":
        instance["request"]["body"] = {
            "state": "rejected",
            "declared_bytes": None,
            "observed_bytes": 0,
        }
        instance.update(
            response=None,
            upstream_status=None,
            client_status=502,
            outcome="upstream_error",
            error_code="upstream_unavailable",
        )
    else:
        outcome = case.removesuffix("_missing_error")
        instance["outcome"] = outcome
        del instance["error_code"]
        if outcome == "request_rejected":
            instance["request"]["body"] = {
                "state": "rejected",
                "declared_bytes": None,
                "observed_bytes": 0,
            }
            instance.update(response=None, upstream_status=None, client_status=400)
        elif outcome == "response_rejected":
            instance["response"]["body"] = {
                "state": "rejected",
                "declared_bytes": None,
                "observed_bytes": 0,
            }
            instance["client_status"] = 502
        else:
            instance.update(response=None, upstream_status=None, client_status=502)

    with pytest.raises(ValidationError):
        HttpAttempt.model_validate(instance)
    errors = list(Draft202012Validator(HttpAttempt.model_json_schema()).iter_errors(instance))
    assert errors, instance


def test_feedback3_complete_forgery_rejects_ordinary_forwarded_absent_body(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    attempts_path = proxy.writer.output_dir / "http-attempts.jsonl"
    manifest_path = proxy.writer.output_dir / "recording-manifest.json"
    row = json.loads(attempts_path.read_text())
    row["response"]["headers"] = [
        header
        for header in row["response"]["headers"]
        if header["name"].lower() not in {"content-type", "content-length"}
    ]
    row["response"]["body"] = {
        "state": "absent",
        "declared_bytes": None,
    }
    attempts_data = json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    attempts_path.write_bytes(attempts_data)
    manifest = json.loads(manifest_path.read_text())
    manifest["attempts_sha256"] = hashlib.sha256(attempts_data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RecordingError, match="attempt_header_body_mismatch"):
        validate_recording(proxy.writer.output_dir, expected_pins=PINS)


def test_feedback3_complete_artifact_accepts_legal_204_absent_body(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    proxy = _proxy(tmp_path, handler)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()

    assert validate_recording(proxy.writer.output_dir, expected_pins=PINS).status == "complete"


@pytest.mark.parametrize(("method", "status"), [("HEAD", 200), ("GET", 304)])
def test_feedback3_real_listener_accepts_oversized_representation_length_without_body(
    tmp_path: Path,
    method: str,
    status: int,
) -> None:
    from benchmarks.memorybench.traffic import DEFAULT_MAX_BODY_BYTES, validate_recording

    declared = DEFAULT_MAX_BODY_BYTES + 123
    upstream_response = (
        f"HTTP/1.1 {status} Empty\r\n".encode()
        + f"Content-Length: {declared}\r\n".encode()
        + b"Connection: close\r\n\r\n"
    )

    response, row, recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method=method,
        upstream_response=upstream_response,
    ))

    assert response.status_code == status
    assert response.content == b""
    assert response.headers["content-length"] == str(declared)
    assert row["response"]["body"]["state"] == "absent"
    assert row["response"]["body"]["declared_bytes"] == declared
    assert validate_recording(recording, expected_pins=PINS).status == "complete"


def test_feedback3_real_listener_accepts_bounded_concatenated_gzip_members(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    decoded = b'{"ok":true}'
    compressed = gzip.compress(decoded[:6], mtime=0) + gzip.compress(decoded[6:], mtime=0)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: gzip\r\n"
        + f"Content-Length: {len(compressed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + compressed
    )

    response, row, recording, observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
        request_headers={"accept-encoding": "gzip"},
        raw_downstream=True,
    ))

    assert b"accept-encoding: gzip" in observed.lower()
    assert isinstance(response, bytes)
    response_head, response_body = response.split(b"\r\n\r\n", 1)
    assert b" 200 " in response_head.split(b"\r\n", 1)[0]
    assert b"content-encoding: gzip" in response_head.lower()
    assert response_body == compressed
    assert gzip.decompress(response_body) == decoded
    assert row["response"]["body"]["sanitized_json"] == {"ok": True}
    assert row["response"]["body"]["wire_bytes"] == len(compressed)
    assert validate_recording(recording, expected_pins=PINS).status == "complete"


def test_feedback3_real_listener_rejects_over_limit_concatenated_gzip(
    tmp_path: Path,
) -> None:
    max_body_bytes = 100
    decoded = b'{"value":"' + (b"x" * 120) + b'"}'
    compressed = gzip.compress(decoded[:60], mtime=0) + gzip.compress(decoded[60:], mtime=0)
    assert len(compressed) < max_body_bytes < len(decoded)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: gzip\r\n"
        + f"Content-Length: {len(compressed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + compressed
    )

    response, row, _recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
        max_body_bytes=max_body_bytes,
        request_headers={"accept-encoding": "gzip"},
    ))

    assert response.status_code == 502
    assert response.content == b'{"error":"upstream_response_invalid"}'
    assert row["outcome"] == "response_rejected"


@pytest.mark.parametrize("damage", ["truncated", "trailing_junk"])
def test_feedback3_real_listener_rejects_malformed_concatenated_gzip(
    tmp_path: Path,
    damage: str,
) -> None:
    compressed = gzip.compress(b'{"ok":', mtime=0) + gzip.compress(b'true}', mtime=0)
    malformed = compressed[:-4] if damage == "truncated" else compressed + b"junk"
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: gzip\r\n"
        + f"Content-Length: {len(malformed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + malformed
    )

    response, row, _recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
        request_headers={"accept-encoding": "gzip"},
    ))

    assert response.status_code == 502
    assert response.content == b'{"error":"upstream_response_invalid"}'
    assert row["outcome"] == "response_rejected"


def test_feedback3_real_listener_preserves_valid_deflate_behavior(tmp_path: Path) -> None:
    decoded = b'{"deflate":true}'
    compressed = zlib.compress(decoded)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: deflate\r\n"
        + f"Content-Length: {len(compressed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + compressed
    )

    response, row, _recording, observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
        request_headers={"accept-encoding": "deflate"},
    ))

    assert b"accept-encoding: deflate" in observed.lower()
    assert response.status_code == 200
    assert response.content == decoded
    assert row["response"]["body"]["sanitized_json"] == {"deflate": True}


@pytest.mark.parametrize(
    "missing_path",
    [
        ("schema_version",),
        ("attempt_ordinal",),
        ("started_at",),
        ("request",),
        ("response",),
        ("upstream_status",),
        ("client_status",),
        ("outcome",),
        ("error_code",),
        ("timing",),
        ("request", "method"),
        ("request", "path"),
        ("request", "body"),
        ("response", "body"),
        ("request", "headers"),
        ("request", "query"),
        ("request", "body", "state"),
        ("request", "body", "declared_bytes"),
        ("response", "status_code"),
        ("response", "headers"),
        ("response", "body", "state"),
        ("response", "body", "declared_bytes"),
        ("timing", "ms"),
        ("timing", "latency_publishable"),
        ("timing", "reason"),
    ],
)
def test_feedback4_complete_artifact_never_synthesizes_missing_row_evidence(
    tmp_path: Path,
    missing_path: tuple[str, ...],
) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    proxy = _proxy(tmp_path, handler)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    attempts_path = proxy.writer.output_dir / "http-attempts.jsonl"
    manifest_path = proxy.writer.output_dir / "recording-manifest.json"
    row = json.loads(attempts_path.read_text())
    target = row
    for component in missing_path[:-1]:
        target = target[component]
    if missing_path == ("response", "body"):
        target[missing_path[-1]] = {}
    else:
        del target[missing_path[-1]]
    attempts_data = json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    attempts_path.write_bytes(attempts_data)
    manifest = json.loads(manifest_path.read_text())
    manifest["attempts_sha256"] = hashlib.sha256(attempts_data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RecordingError, match="invalid_attempt_jsonl"):
        validate_recording(proxy.writer.output_dir, expected_pins=PINS)


def test_feedback4_message_attempt_and_timing_fields_are_required() -> None:
    from benchmarks.memorybench.traffic import HttpAttempt, HttpMessage, HttpRequest, Timing

    requirements = {
        HttpMessage: {"headers", "body"},
        HttpRequest: {"headers", "body", "method", "path", "query"},
        Timing: {"ms", "latency_publishable", "reason"},
        HttpAttempt: {
            "schema_version",
            "attempt_ordinal",
            "started_at",
            "request",
            "response",
            "upstream_status",
            "client_status",
            "outcome",
            "error_code",
            "timing",
        },
    }
    for model, names in requirements.items():
        assert names == set(model.model_fields)
        assert all(model.model_fields[name].is_required() for name in names)
        assert set(model.model_json_schema()["required"]) == names

    from benchmarks.memorybench.traffic import BodyRecord

    body_schema = BodyRecord.model_json_schema()
    body_requirements = {
        "AbsentBodyRecord": {"state", "declared_bytes"},
        "JsonBodyRecord": {"state", "wire_bytes", "sha256", "sanitized_json", "redaction_count"},
        "RejectedBodyRecord": {"state", "declared_bytes", "observed_bytes"},
    }
    for definition, names in body_requirements.items():
        variant = body_schema["$defs"][definition]
        assert set(variant["required"]) == names
        assert all("default" not in variant["properties"][name] for name in names)


def test_feedback4_body_model_and_schema_use_one_canonical_wire_length() -> None:
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    from benchmarks.memorybench.traffic import BodyRecord

    canonical = {
        "state": "json",
        "wire_bytes": 10,
        "sha256": "a" * 64,
        "sanitized_json": {"ok": True},
        "redaction_count": 0,
    }
    body = BodyRecord.model_validate(canonical)
    assert body.model_dump(mode="json") == canonical
    assert not list(Draft202012Validator(BodyRecord.model_json_schema()).iter_errors(canonical))

    forged = {
        "state": "json",
        "declared_bytes": 10,
        "observed_bytes": 11,
        "sha256": "a" * 64,
        "sanitized_json": {"ok": True},
        "redaction_count": 0,
    }
    with pytest.raises(ValidationError):
        BodyRecord.model_validate(forged)
    assert list(Draft202012Validator(BodyRecord.model_json_schema()).iter_errors(forged))


def test_feedback4_final_artifact_recomputes_content_length_against_wire_length(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.traffic import RecordingError, validate_recording

    proxy = _proxy(tmp_path)
    _run(proxy.handle("GET", "/v3/documents/a"))
    proxy.writer.finalize()
    attempts_path = proxy.writer.output_dir / "http-attempts.jsonl"
    manifest_path = proxy.writer.output_dir / "recording-manifest.json"
    row = json.loads(attempts_path.read_text())
    body = row["response"]["body"]
    assert set(body) == {"state", "wire_bytes", "sha256", "sanitized_json", "redaction_count"}
    content_length = next(
        header for header in row["response"]["headers"]
        if header["name"].lower() == "content-length"
    )
    content_length["value"] = str(body["wire_bytes"] + 1)
    attempts_data = json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    attempts_path.write_bytes(attempts_data)
    manifest = json.loads(manifest_path.read_text())
    manifest["attempts_sha256"] = hashlib.sha256(attempts_data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RecordingError, match="attempt_header_body_mismatch"):
        validate_recording(proxy.writer.output_dir, expected_pins=PINS)


def test_feedback4_real_listener_rejects_compressed_empty_json(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    compressed = gzip.compress(b"", mtime=0)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: gzip\r\n"
        + f"Content-Length: {len(compressed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + compressed
    )
    response, row, recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
        request_headers={"accept-encoding": "gzip"},
        raw_downstream=True,
    ))

    assert isinstance(response, bytes)
    assert b" 502 " in response.split(b"\r\n", 1)[0]
    assert response.endswith(b'{"error":"upstream_response_invalid"}')
    assert row["outcome"] == "response_rejected"
    assert row["error_code"] == "upstream_response_invalid"
    assert row["response"]["body"]["state"] == "rejected"
    assert "sanitized_json" not in row["response"]["body"]
    assert validate_recording(recording, expected_pins=PINS).attempt_count == 1


def test_feedback4_gzip_member_limit_rejects_many_empty_members_without_hanging(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.recording_proxy import MAX_GZIP_MEMBERS
    from benchmarks.memorybench.traffic import validate_recording

    assert 2 <= MAX_GZIP_MEMBERS <= 32
    compressed = gzip.compress(b"", mtime=0) * (MAX_GZIP_MEMBERS + 1000)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: gzip\r\n"
        + f"Content-Length: {len(compressed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + compressed
    )

    async def exercise():
        return await asyncio.wait_for(
            _feedback3_real_response_round_trip(
                tmp_path,
                method="GET",
                upstream_response=upstream_response,
                request_headers={"accept-encoding": "gzip"},
            ),
            timeout=5,
        )

    response, row, recording, _observed = _run(exercise())
    assert response.status_code == 502
    assert response.content == b'{"error":"upstream_response_invalid"}'
    assert row["outcome"] == "response_rejected"
    assert validate_recording(recording, expected_pins=PINS).attempt_count == 1


@pytest.mark.parametrize("case", ["two_members", "over_limit", "truncated", "trailing_junk", "deflate"])
def test_feedback4_real_listener_preserves_strict_bounded_content_decoding(
    tmp_path: Path,
    case: str,
) -> None:
    max_body_bytes = 100
    encoding = "gzip"
    decoded = b'{"ok":true}'
    compressed = gzip.compress(decoded[:6], mtime=0) + gzip.compress(decoded[6:], mtime=0)
    expected_status = 200
    if case == "over_limit":
        decoded = b'{"value":"' + (b"x" * 120) + b'"}'
        compressed = gzip.compress(decoded[:60], mtime=0) + gzip.compress(decoded[60:], mtime=0)
        expected_status = 502
    elif case == "truncated":
        compressed = compressed[:-4]
        expected_status = 502
    elif case == "trailing_junk":
        compressed += b"junk"
        expected_status = 502
    elif case == "deflate":
        encoding = "deflate"
        decoded = b'{"deflate":true}'
        compressed = zlib.compress(decoded)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Encoding: {encoding}\r\n".encode()
        + f"Content-Length: {len(compressed)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + compressed
    )
    response, row, _recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
        max_body_bytes=max_body_bytes,
        request_headers={"accept-encoding": encoding},
        raw_downstream=case == "two_members",
    ))

    actual_status = (
        int(response.split(b" ", 2)[1]) if isinstance(response, bytes) else response.status_code
    )
    assert actual_status == expected_status
    assert row["outcome"] == ("forwarded" if expected_status == 200 else "response_rejected")


def test_feedback4_json_nesting_exhaustion_has_stable_error_code() -> None:
    from benchmarks.memorybench.recording_proxy import RequestRefusal, _parse_json

    deeply_nested = (b"[" * 1100) + b"0" + (b"]" * 1100)
    with pytest.raises(RequestRefusal, match="invalid_json") as exc_info:
        _parse_json(deeply_nested)
    assert exc_info.value.code == "invalid_json"


def test_feedback4_real_listener_records_deeply_nested_request_rejection(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    deeply_nested = (b"[" * 1100) + b'"request-secret"' + (b"]" * 1100)
    response, rows, recording, upstream_calls = _run(_feedback4_real_request_round_trip(
        tmp_path,
        body=deeply_nested,
    ))

    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert response.endswith(b'{"error":"invalid_json"}')
    assert upstream_calls == 0
    assert len(rows) == 1
    assert rows[0]["outcome"] == "request_rejected"
    assert rows[0]["error_code"] == "invalid_json"
    assert "request-secret" not in json.dumps(rows[0])
    assert validate_recording(recording, expected_pins=PINS).attempt_count == 1


def test_feedback4_real_listener_records_deeply_nested_upstream_rejection(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    deeply_nested = (b"[" * 1100) + b'"response-secret"' + (b"]" * 1100)
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(deeply_nested)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + deeply_nested
    )
    response, row, recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
    ))

    assert response.status_code == 502
    assert response.content == b'{"error":"upstream_response_invalid"}'
    assert row["outcome"] == "response_rejected"
    assert "response-secret" not in json.dumps(row)
    assert validate_recording(recording, expected_pins=PINS).attempt_count == 1


def test_feedback4_decoded_stages_is_bounded_iterator() -> None:
    from collections.abc import Iterator
    from benchmarks.memorybench.traffic import _decoded_stages

    stages = _decoded_stages("api%255Fkey", plus_as_space=False)
    assert isinstance(stages, Iterator)
    assert iter(stages) is stages
    assert not isinstance(stages, (tuple, list))
    assert list(stages) == ["api%255Fkey", "api%5Fkey", "api_key"]


def test_feedback4_idle_client_close_is_bounded_and_leaves_recording_incomplete(
    tmp_path: Path,
) -> None:
    from benchmarks.memorybench.recording_proxy import RecordingProxy
    from benchmarks.memorybench.traffic import RecordingError, RecordingWriter, validate_recording

    async def exercise() -> tuple[dict[str, object], bytes, bytes]:
        writer = RecordingWriter(
            tmp_path / "recording",
            pins=PINS,
            upstream_timeout_seconds=0.05,
        )
        proxy = RecordingProxy(
            "http://127.0.0.1:8765",
            writer,
            transport=httpx.MockTransport(_response),
            timeout_seconds=0.05,
        )
        base_url = await proxy.start("127.0.0.1", 0)
        port = int(base_url.rsplit(":", 1)[1])
        reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            for _ in range(100):
                if proxy._active_count == 1:
                    break
                await asyncio.sleep(0)
            assert proxy._active_count == 1
            await asyncio.wait_for(proxy.close(), timeout=1)
            eof = await asyncio.wait_for(reader.read(), timeout=1)
        finally:
            client_writer.close()
            await client_writer.wait_closed()
            if not proxy._closed:
                await proxy.abort()
        manifest = json.loads((writer.output_dir / "recording-manifest.json").read_text())
        attempts = (writer.output_dir / "http-attempts.jsonl").read_bytes()
        return manifest, attempts, eof

    manifest, attempts, eof = _run(exercise())
    assert eof == b""
    assert manifest["status"] == "recording"
    assert manifest["completed_at"] is None
    assert attempts == b""
    with pytest.raises(RecordingError, match="incomplete_recording"):
        validate_recording(tmp_path / "recording", expected_pins=PINS)


def test_feedback4_close_finalizes_after_normal_clean_drain(tmp_path: Path) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    proxy = _proxy(tmp_path)

    async def exercise() -> bytes:
        base_url = await proxy.start("127.0.0.1", 0)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/v3/documents/a")
        await proxy.close()
        return response.content

    assert _run(exercise()) == b'{"ok":true,"token":"nested-secret"}'
    assert validate_recording(proxy.writer.output_dir, expected_pins=PINS).status == "complete"


def _feedback5_nested_json(depth: int, *, mixed: bool, leaf: str) -> bytes:
    payload = json.dumps(leaf, separators=(",", ":")).encode()
    for level in range(depth):
        if mixed and level % 2:
            payload = b'{"item":' + payload + b"}"
        else:
            payload = b"[" + payload + b"]"
    return payload


def test_feedback5_uses_conservative_json_nesting_bound() -> None:
    from benchmarks.memorybench.recording_proxy import MAX_JSON_NESTING_DEPTH

    assert MAX_JSON_NESTING_DEPTH == 64


@pytest.mark.parametrize(
    ("case", "depth", "mixed", "expected_status"),
    [
        ("array_at_limit", 64, False, 204),
        ("array_over_limit", 65, False, 400),
        ("array_far_over_limit", 1000, False, 400),
        ("mixed_at_limit", 64, True, 204),
        ("brackets_in_string", 1, False, 204),
    ],
)
def test_feedback5_real_listener_request_depth_boundary(
    tmp_path: Path,
    case: str,
    depth: int,
    mixed: bool,
    expected_status: int,
) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    rejected = expected_status == 400
    secret = f"{case}-request-payload-secret"
    if case == "brackets_in_string":
        payload = json.dumps({"value": ("[" * 1000) + ("]" * 1000)}, separators=(",", ":")).encode()
    else:
        payload = _feedback5_nested_json(
            depth,
            mixed=mixed,
            leaf=secret if rejected else "safe-leaf",
        )
    response, rows, recording, upstream_calls = _run(_feedback4_real_request_round_trip(
        tmp_path,
        body=payload,
    ))

    assert int(response.split(b" ", 2)[1]) == expected_status
    assert len(rows) == 1
    assert rows[0]["outcome"] == ("request_rejected" if rejected else "forwarded")
    assert rows[0]["error_code"] == ("invalid_json" if rejected else None)
    assert upstream_calls == (0 if rejected else 1)
    if rejected:
        assert response.endswith(b'{"error":"invalid_json"}')
        assert secret.encode() not in (recording / "http-attempts.jsonl").read_bytes()
    assert validate_recording(recording, expected_pins=PINS).attempt_count == 1


@pytest.mark.parametrize(
    ("case", "depth", "mixed", "expected_status"),
    [
        ("array_at_limit", 64, False, 200),
        ("array_over_limit", 65, False, 502),
        ("array_far_over_limit", 1000, False, 502),
        ("mixed_at_limit", 64, True, 200),
        ("brackets_in_string", 1, False, 200),
    ],
)
def test_feedback5_real_listener_response_depth_boundary(
    tmp_path: Path,
    case: str,
    depth: int,
    mixed: bool,
    expected_status: int,
) -> None:
    from benchmarks.memorybench.traffic import validate_recording

    rejected = expected_status == 502
    secret = f"{case}-response-payload-secret"
    if case == "brackets_in_string":
        payload = json.dumps({"value": ("[" * 1000) + ("]" * 1000)}, separators=(",", ":")).encode()
    else:
        payload = _feedback5_nested_json(
            depth,
            mixed=mixed,
            leaf=secret if rejected else "safe-leaf",
        )
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + payload
    )
    response, row, recording, _observed = _run(_feedback3_real_response_round_trip(
        tmp_path,
        method="GET",
        upstream_response=upstream_response,
    ))

    assert response.status_code == expected_status
    assert row["outcome"] == ("response_rejected" if rejected else "forwarded")
    assert row["error_code"] == ("upstream_response_invalid" if rejected else None)
    if rejected:
        assert response.content == b'{"error":"upstream_response_invalid"}'
        assert secret.encode() not in (recording / "http-attempts.jsonl").read_bytes()
    assert validate_recording(recording, expected_pins=PINS).attempt_count == 1
