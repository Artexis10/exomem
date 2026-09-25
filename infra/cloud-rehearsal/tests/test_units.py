"""Unit tests for the rehearsal's pure parts. The rehearsal itself is the
live test; these pin the pieces a wrong edit would silently weaken."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from cloud_rehearsal import build, platform, report, substrate, tls
from cloud_rehearsal.mcp_client import _HiddenInputs


def test_percentile_is_nearest_rank() -> None:
    assert report.percentile([], 0.95) is None
    assert report.percentile([0.1] * 19 + [5.0], 0.95) == 0.1
    assert report.percentile([0.1] * 18 + [4.0, 5.0], 0.95) == 4.0


def test_targets_match_task_5_3() -> None:
    assert report.TARGETS == {
        "initialize_warm_p95_seconds": 0.5,
        "tools_list_warm_p95_seconds": 0.5,
        "capture_p95_seconds": 1.0,
        "cited_recall_p95_seconds": 1.0,
        "provision_seconds": 180.0,
        "upgrade_seconds_per_cell": 60.0,
    }


def test_a_report_gates_the_node_only_when_everything_passed() -> None:
    run = report.Report(run_id="x")
    run.steps = [report.StepRecord(number=n, name=str(n), status=report.PASSED) for n in range(1, 13)]
    for name in ("initialize_warm", "tools_list_warm", "capture", "cited_recall"):
        run.sample(name, 0.1)
    run.single.update({"provision_seconds": 60.0, "upgrade_seconds_per_cell": 30.0})
    assert run.to_json()["outcome"]["gates_node"] is True
    run.single["provision_seconds"] = 180.0  # the target is strictly under 3 minutes
    assert run.to_json()["outcome"]["gates_node"] is False
    run.single["provision_seconds"] = 60.0
    run.valid = False
    assert run.to_json()["outcome"]["gates_node"] is False


def test_a_cross_lane_failure_is_recorded_with_its_owner() -> None:
    record = report.failure_record(
        report.CrossLaneDefect("boom", component="c", owner="o#1", evidence={"status": 500})
    )
    assert record["cross_lane"] is True and record["owner"] == "o#1" and "traceback" not in record


def test_paddle_signature_is_paddles_scheme() -> None:
    body = b'{"a":1}'
    header = substrate.paddle_signature("secret", body)
    timestamp, digest = (part.split("=", 1)[1] for part in header.split(";"))
    assert digest == hmac.new(b"secret", timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()


def test_emailed_secrets_are_seeded_as_sha256_digests_only() -> None:
    assert substrate.sha256("token") == hashlib.sha256(b"token").digest()
    assert len(substrate.paddle_id("sub")) == len("sub_") + 26


def test_the_consent_form_parser_reads_substrates_hidden_fields() -> None:
    page = (
        '<form action="/api/exomem/oauth/authorize/complete" class="mt-8" method="post">'
        '<input name="nonce" type="hidden" value="n1"/>'
        '<input name="confirmation" type="hidden" value="c1"/></form>'
    )
    parser = _HiddenInputs()
    parser.feed(page)
    assert parser.form_action == "/api/exomem/oauth/authorize/complete"
    assert parser.fields == {"nonce": "n1", "confirmation": "c1"}


def test_the_gateway_overlay_adds_only_what_the_chart_omits() -> None:
    container = {"env": [{"name": "DATABASE_URL", "valueFrom": {}}]}
    added = platform.gateway_env_overlay(container, {"DATABASE_URL": "x", "EXOMEM_CLOUD_MCP_PATH": "/mcp"})
    assert added == ["EXOMEM_CLOUD_MCP_PATH"]
    assert [e["name"] for e in container["env"]] == ["DATABASE_URL", "EXOMEM_CLOUD_MCP_PATH"]


def test_the_broken_canary_still_runs_cell_init() -> None:
    assert 'if [ "$1" = "cell-init" ]; then exec "$0-real" "$@"; fi' in build.BROKEN_SHIM


def test_the_gateway_repository_is_the_one_the_chart_schema_admits() -> None:
    schema = json.loads((platform.PLATFORM_CHART / "values.schema.json").read_text(encoding="utf-8"))
    pattern = schema["properties"]["cloudGateway"]["then"]["properties"]["image"]["pattern"]
    import re

    assert re.fullmatch(pattern, f"{build.GATEWAY_REPOSITORY}@sha256:{'a' * 64}")


def test_pki_signs_both_names(tmp_path: Path) -> None:
    pki = tls.make_pki(tmp_path)
    assert set(pki.leaves) == {tls.SUBSTRATE_HOST, tls.MCP_HOST}
    assert pki.ca_path.read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")


@pytest.mark.parametrize("path", ["Dockerfile", "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"])
def test_repository_inputs_exist(path: str) -> None:
    from cloud_rehearsal import images

    assert (images.REPO_ROOT / path).exists()


def test_pki_verifies_under_strict_x509(tmp_path: Path) -> None:
    import socket
    import ssl
    import threading

    pki = tls.make_pki(tmp_path)
    leaf = pki.leaves[tls.MCP_HOST]
    (tmp_path / "leaf.pem").write_text(leaf.cert_pem + leaf.key_pem, encoding="utf-8")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / "leaf.pem")
    client_context = ssl.create_default_context(cafile=str(pki.ca_path))
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            server_context.wrap_socket(connection, server_side=True).close()
        except ssl.SSLError:
            connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with client_context.wrap_socket(raw, server_hostname=tls.MCP_HOST) as wrapped:
            assert wrapped.version()
    thread.join(timeout=5)
    listener.close()
