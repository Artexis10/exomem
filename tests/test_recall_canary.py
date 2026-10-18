"""Read-only public MCP case proof and content-free availability reports."""

from __future__ import annotations

import asyncio
import json

import pytest

from exomem import recall_canary as gate
from exomem.request_latency_contract import load_contract


def _canary_case(**expected):
    return {
        "id": "case-01",
        "tool": "ask_memory",
        "arguments": {"query": "private-question"},
        "expected": {
            "state_path": ["ok"],
            "state": True,
            "outcome": "ok",
            "references": [{"path": ["hits", 0, "ref"], "value": "private-reference"}],
            "material_paths": [["hits", 0, "text"]],
            **expected,
        },
    }


def _run_canary(tmp_path, server, cases, **kwargs):
    from fastmcp import Client

    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    options = {
        "samples": 30,
        "warm_up": True,
        "contention": "uncontended",
        "deployment_id": "verified-deployment",
        "deployment_verified": True,
        "package_mode": "source",
        **kwargs,
    }
    return asyncio.run(
        gate.run_mcp_canary(
            client=Client(server),
            cases_file=path,
            **options,
        )
    )


def test_mcp_canary_proves_material_over_parsed_public_calls(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["verdict"] == "passed"
    assert report["availability"] == "passed"
    case = report["cases"][0]
    assert case["measured_attempts"] == 30
    assert case["warmth"] == "successful-case-proof"
    assert case["latency"][0]["boundary"] == "mcp-roundtrip-v1"
    assert case["latency"][0]["samples"] == 30
    assert case["latency"][0]["outcome_inclusion"] == ["ok", "error", "refused", "abstained"]
    assert report["initialization_ms"] >= 0
    assert all(item["elapsed_ms"] >= 0 for item in case["attempts"])
    assert report["observer"]["role"] == "observer-only"
    rendered = json.dumps(report)
    for secret in ("private-question", "private-reference", "verified-deployment"):
        assert secret not in rendered


def test_mcp_deep_recall_cannot_pass_the_ordinary_population(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str, deep: bool) -> dict:
        assert deep
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    case = _canary_case()
    case["arguments"]["deep"] = True
    report = _run_canary(tmp_path, server, [case])
    assert report["availability"] == "passed"
    assert report["verdict"] == "incomplete"
    [row] = report["cases"][0]["latency"]
    assert row["deep"] is True
    assert row["verdict"] == "unavailable"
    assert row["breach"] is False


def test_mcp_fast_typed_refusal_fails_and_stops_the_series(tmp_path, caplog):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": False, "error": {"code": "RETRIEVAL_INDEX_WARMING"}}

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["verdict"] == "failed"
    assert report["availability"] == "failed"
    assert report["coverage"] == "incomplete"
    attempts = report["cases"][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "refused"
    assert attempts[0]["elapsed_ms"] >= 0
    assert any(
        record.event == "recall_canary_failed"
        for record in caplog.records
        if hasattr(record, "event")
    )


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"ok": True, "hits": [{"ref": "wrong", "text": "material"}]}, "missing-reference"),
        ({"ok": True, "hits": [{"ref": "private-reference", "text": "  "}]}, "missing-material"),
        ({"ok": True}, "missing-reference"),
        ({"ok": False}, "unexpected-outcome"),
    ],
)
def test_mcp_positive_cases_require_source_truth_and_material(tmp_path, response, reason):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return response

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["availability"] == "failed"
    assert report["cases"][0]["attempts"][0]["failure"] == reason


def test_mcp_declared_abstention_must_match_its_state(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        return {"abstained": True, "abstention": {"reason": "unresolved"}}

    case = _canary_case(
        state_path=["abstention", "reason"],
        state="unresolved",
        outcome="abstained",
        references=[],
        material_paths=[],
    )
    case["tool"] = "activate_context"
    report = _run_canary(tmp_path, server, [case])
    assert report["availability"] == "incomplete"
    assert report["cases"][0]["attempts"][0]["outcome"] == "abstained"
    assert report["verdict"] == "incomplete"
    assert report["positive_coverage"]["activate_context"]["successful_cases"] == 0
    case["expected"]["state"] = "ambiguous"
    assert _run_canary(tmp_path, server, [case])["availability"] == "failed"


def test_mcp_expected_abstention_needs_positive_coverage_for_its_tool(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        if query == "negative":
            return {"abstained": True, "abstention": {"reason": "unresolved"}}
        return {"abstained": False, "hits": [{"ref": "private-reference", "text": query}]}

    positive = _canary_case(state_path=["abstained"], state=False)
    positive["tool"] = "activate_context"
    negative = _canary_case(
        state_path=["abstention", "reason"],
        state="unresolved",
        outcome="abstained",
        references=[],
        material_paths=[],
    )
    negative.update(id="case-02", tool="activate_context", arguments={"query": "negative"})
    report = _run_canary(tmp_path, server, [positive, negative])
    assert report["verdict"] == "passed"
    assert report["positive_coverage"]["activate_context"]["successful_cases"] == 1
    assert report["cases"][1]["availability"] == "passed"
    assert report["cases"][1]["latency"][0]["abstentions"] == 30


@pytest.mark.parametrize("damage", ["write-tool", "empty-material", "bad-path", "duplicate"])
def test_mcp_invalid_cases_do_not_call_tools(tmp_path, damage):
    from fastmcp import FastMCP

    server = FastMCP("test")
    calls = []

    @server.tool
    def ask_memory(query: str) -> dict:
        calls.append(query)
        return {"ok": True}

    case = _canary_case()
    if damage == "write-tool":
        case["tool"] = "remember"
    elif damage == "empty-material":
        case["expected"]["material_paths"] = []
    elif damage == "bad-path":
        case["expected"]["state_path"] = [True]
    cases = [case, case] if damage == "duplicate" else [case]
    report = _run_canary(tmp_path, server, cases)
    assert report["verdict"] == "failed"
    assert report["failure"] == "invalid-cases"
    assert calls == []


@pytest.mark.parametrize(
    "missing",
    [
        {"contention": "unknown"},
        {"warm_up": False},
        {"deployment_verified": False},
        {"samples": 1},
    ],
)
def test_mcp_missing_reference_conditions_prevent_acceptance(tmp_path, missing):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(tmp_path, server, [_canary_case()], **missing)
    assert report["availability"] == "passed"
    assert report["verdict"] == "incomplete"


def test_mcp_source_observer_cannot_claim_an_installed_package(tmp_path):
    from fastmcp import FastMCP

    report = _run_canary(tmp_path, FastMCP("test"), [_canary_case()], package_mode="installed")
    assert report["failure"] == "invalid-observer-or-contract"
    assert report["initialization_ms"] is None


def test_mcp_cli_rejects_invalid_target_without_leaking_inputs(tmp_path, capsys):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        ["--mcp-url", "file:///private-target", "--cases", str(path), "--package-mode", "source"]
    )
    output = capsys.readouterr()
    assert status != 0
    assert json.loads(output.out)["failure"] == "invalid-target"
    assert "private-target" not in output.out + output.err


def test_mcp_measured_failure_retains_all_outcomes_and_unattempted_cases(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")
    completed = 0

    @server.tool
    def ask_memory(query: str) -> dict:
        nonlocal completed
        completed += 1
        if completed == 3:
            return {"ok": False, "error": {"code": "RETRIEVAL_INDEX_WARMING"}}
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    later = _canary_case()
    later["id"] = "case-02"
    report = _run_canary(tmp_path, server, [_canary_case(), later])
    first, second = report["cases"]
    assert report["verdict"] == "failed"
    assert first["measured_attempts"] == 2
    assert first["latency"][0]["observed"] == 2
    assert first["latency"][0]["samples"] == 2
    assert first["latency"][0]["refusals"] == 1
    assert second["availability"] == "unattempted"
    assert second["attempts"] == []


@pytest.mark.parametrize("terminal", ["malformed", "tool-error", "timeout"])
def test_mcp_terminal_failures_have_elapsed_time_without_response_content(tmp_path, terminal):
    from fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent

    server = FastMCP("test")

    @server.tool
    async def ask_memory(query: str):
        if terminal == "timeout":
            await asyncio.sleep(1)
        return CallToolResult(
            isError=terminal == "tool-error",
            content=[TextContent(type="text", text="private-response")],
        )

    report = _run_canary(tmp_path, server, [_canary_case()], timeout_seconds=0.01)
    assert report["verdict"] == "failed"
    attempt = report["cases"][0]["attempts"][0]
    assert attempt["outcome"] == "error"
    assert attempt["elapsed_ms"] >= 0
    assert len(report["cases"][0]["attempts"]) == 1
    assert "private-response" not in json.dumps(report)


def test_mcp_bad_json_and_missing_case_files_fail_before_connection(tmp_path):
    from fastmcp import Client, FastMCP

    path = tmp_path / "cases.json"
    for content in (None, "{not json}"):
        if content:
            path.write_text(content)
        report = asyncio.run(
            gate.run_mcp_canary(
                client=Client(FastMCP("test")),
                cases_file=path,
                package_mode="source",
            )
        )
        assert report["failure"] == "invalid-cases"
        assert report["initialization_ms"] is None


def test_mcp_controller_contract_uses_its_own_revision_and_sample_count(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    contract = load_contract().model_dump(mode="json")
    contract["revision"] = "controller-test"
    bar = next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")
    bar["min_samples"] = 31
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    report = _run_canary(tmp_path, server, [_canary_case()], contract_file=path)
    assert report["verdict"] == "incomplete"
    assert report["contract"]["revision"] == "controller-test"
    assert report["contract"]["sha256"] == gate._digest(path.read_bytes())
    latency = report["cases"][0]["latency"][0]
    assert latency["contract_revision"] == "controller-test"
    assert latency["min_samples"] == 31


def test_mcp_cli_emits_only_content_free_json_and_structured_warning(tmp_path, monkeypatch, capsys):
    import fastmcp

    server = fastmcp.FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": ""}]}

    real_client = fastmcp.Client
    monkeypatch.setattr(fastmcp, "Client", lambda transport, **kwargs: real_client(server))
    monkeypatch.setenv("TEST_CANARY_TOKEN", "private-token")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        [
            "--mcp-url",
            "https://private-target.invalid/mcp",
            "--cases",
            str(path),
            "--token-env",
            "TEST_CANARY_TOKEN",
            "--package-mode",
            "source",
            "--warm-up",
        ]
    )
    output = capsys.readouterr()
    assert status == 1
    assert json.loads(output.out)["availability"] == "failed"
    assert json.loads(output.err)["event"] == "recall_canary_failed"
    for secret in ("private-token", "private-target", "private-reference", "private-question"):
        assert secret not in output.out + output.err


def test_one_successful_sample_proves_availability_without_accepting_latency(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(
        tmp_path,
        server,
        [_canary_case()],
        samples=1,
        warm_up=False,
        contention="unknown",
        deployment_verified=False,
        check="availability",
    )
    assert report["verdict"] == "incomplete"
    assert report["check"] == "availability"
    assert report["check_verdict"] == "passed"
    assert report["cases"][0]["latency"][0]["samples"] == 1


def test_availability_keeps_a_latency_breach_visible_without_failing_the_check(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    contract = load_contract().model_dump(mode="json")
    next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")[
        "threshold_ms"
    ] = 0
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    report = _run_canary(
        tmp_path, server, [_canary_case()], contract_file=path, check="availability"
    )
    assert report["verdict"] == "failed"
    assert report["check_verdict"] == "passed"
    assert report["cases"][0]["latency"][0]["breach"] is True


def test_cli_atomically_keeps_an_owner_only_latest_report(tmp_path, monkeypatch, capsys):
    import fastmcp

    server = fastmcp.FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    real_client = fastmcp.Client
    monkeypatch.setattr(fastmcp, "Client", lambda transport, **kwargs: real_client(server))
    monkeypatch.setenv("TEST_CANARY_TOKEN", "private-token")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([_canary_case()]))
    path = tmp_path / "latest.json"
    status = gate.main(
        [
            "--mcp-url",
            "https://private-target.invalid/mcp",
            "--cases",
            str(cases),
            "--token-env",
            "TEST_CANARY_TOKEN",
            "--package-mode",
            "source",
            "--check",
            "availability",
            "--report-file",
            str(path),
        ]
    )
    output = capsys.readouterr()
    assert status == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == json.loads(output.out)
    assert not list(tmp_path.glob(".canary-*"))
    assert "private-token" not in path.read_text() + output.out + output.err
