"""Track C two-witness activation join + natural-prompt driver contracts.

Synthetic fixtures only: a canned ``claude -p --output-format stream-json``
transcript and a minimal server witness trace. A client-claims/server-quiet
disagreement is a WITNESS_MISMATCH — a harness fault, never a product score.
Live ``claude -p`` execution is user-run (network + credentials); tests drive
the injectable runner seam.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from membench.trackc.natural_prompt_driver import (
    EXPLICIT_PREFIX,
    build_argv,
    run_case,
    write_mcp_config,
)
from membench.trackc.witness_join import (
    join_case,
    parse_server_trace,
    parse_stream_json_transcript,
)

# ------------------------------------------------------------- fixtures


def _transcript_lines(*, with_tool_use: bool) -> list[str]:
    lines: list[dict] = [
        {"type": "system", "subtype": "init", "session_id": "sess-1"},
    ]
    if with_tool_use:
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check stored memory."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "mcp__exomem__ask_memory",
                            "input": {"query": "delivery deadline", "limit": 5},
                        },
                    ]
                },
            }
        )
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "..."}
                    ]
                },
            }
        )
    lines.append(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Answering now."}]},
        }
    )
    lines.append(
        {
            "type": "result",
            "subtype": "success",
            "result": "The deadline is 2026-03-04. [ref:SRC-AAAA1111]",
            "duration_ms": 4321,
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 11, "output_tokens": 22},
            "num_turns": 2,
            "session_id": "sess-1",
        }
    )
    return [json.dumps(line) for line in lines]


_WITNESS_LINES = [
    json.dumps({"ts": "2026-08-01T10:00:00Z", "tool": "ask_memory", "args_digest": "ab12"}),
]


# ------------------------------------------------------------ witness join


def test_matching_witnesses_join_to_activated(tmp_path: Path) -> None:
    transcript = parse_stream_json_transcript(_transcript_lines(with_tool_use=True))
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("\n".join(_WITNESS_LINES) + "\n", encoding="utf-8")
    trace = parse_server_trace(trace_path)
    assert trace.events and trace.events[0].tool == "ask_memory"
    assert trace.malformed_lines == 0

    verdict = join_case("case-1", transcript=transcript, server_events=trace)
    assert verdict.client_claims is True
    assert verdict.server_observed is True
    assert verdict.verdict == "activated"
    assert verdict.harness_fault is False


def test_both_quiet_join_to_not_activated() -> None:
    transcript = parse_stream_json_transcript(_transcript_lines(with_tool_use=False))
    verdict = join_case("case-2", transcript=transcript, server_events=[])
    assert verdict.client_claims is False
    assert verdict.server_observed is False
    assert verdict.verdict == "not_activated"
    assert verdict.harness_fault is False


def test_transcript_claims_without_server_trace_is_witness_mismatch() -> None:
    transcript = parse_stream_json_transcript(_transcript_lines(with_tool_use=True))
    verdict = join_case("case-3", transcript=transcript, server_events=[])
    assert verdict.verdict == "WITNESS_MISMATCH"
    assert verdict.harness_fault is True  # never a product score


def test_server_trace_without_transcript_claim_is_witness_mismatch(tmp_path: Path) -> None:
    transcript = parse_stream_json_transcript(_transcript_lines(with_tool_use=False))
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("\n".join(_WITNESS_LINES) + "\n", encoding="utf-8")
    verdict = join_case(
        "case-4", transcript=transcript, server_events=parse_server_trace(trace_path)
    )
    assert verdict.verdict == "WITNESS_MISMATCH"
    assert verdict.harness_fault is True


# ------------------------------------------------- damaged-witness integrity


def test_broken_server_trace_never_scores_not_activated() -> None:
    """The reviewer-executed scenario: a schema-drifted / crash-truncated
    server trace parses to zero events but MUST be counted as malformed, and
    the join must report a harness fault instead of taking the both-quiet
    branch to a product score."""

    transcript = parse_stream_json_transcript(_transcript_lines(with_tool_use=False))
    trace = parse_server_trace(
        [
            '{"ts": 1, "too',  # crash-truncated JSON
            '{"no_tool": true}',  # schema drift: no tool field
            "[1, 2]",  # non-dict payload
        ]
    )
    assert trace.events == ()
    assert trace.malformed_lines == 3  # counted, never silently dropped

    verdict = join_case("case-6", transcript=transcript, server_events=trace)
    assert verdict.verdict == "WITNESS_MISMATCH"
    assert verdict.harness_fault is True
    assert verdict.verdict != "not_activated"


def test_damaged_transcript_never_scores_not_activated() -> None:
    lines = _transcript_lines(with_tool_use=False)
    lines.insert(1, '{"type": "assistant", "message"')  # crash-truncated line
    transcript = parse_stream_json_transcript(lines)
    assert transcript.malformed_lines == 1

    verdict = join_case("case-7", transcript=transcript, server_events=[])
    assert verdict.verdict == "WITNESS_MISMATCH"
    assert verdict.harness_fault is True


def test_error_result_transcript_is_a_harness_fault_in_the_join() -> None:
    lines = _transcript_lines(with_tool_use=False)
    result = json.loads(lines[-1])
    result["subtype"] = "error_during_execution"
    lines[-1] = json.dumps(result)
    transcript = parse_stream_json_transcript(lines)
    assert transcript.is_error is True

    verdict = join_case("case-8", transcript=transcript, server_events=[])
    assert verdict.verdict == "WITNESS_MISMATCH"
    assert verdict.harness_fault is True


def test_non_memory_tool_use_does_not_claim_activation() -> None:
    lines = _transcript_lines(with_tool_use=False)
    lines.insert(
        1,
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_09",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ]
                },
            }
        ),
    )
    transcript = parse_stream_json_transcript(lines)
    verdict = join_case("case-5", transcript=transcript, server_events=[])
    assert verdict.client_claims is False
    assert verdict.verdict == "not_activated"


# ------------------------------------------------------------------ driver


def test_driver_writes_isolated_mcp_config(tmp_path: Path) -> None:
    config_path = write_mcp_config(
        tmp_path / "mcp.json", vault=tmp_path / "vault", workdir=tmp_path / "wd"
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["exomem"]
    assert server["args"][-3:] == ["exomem", "--transport", "stdio"]
    assert server["env"]["EXOMEM_VAULT_PATH"] == str(tmp_path / "vault")
    assert server["env"]["EXOMEM_DISABLE_EMBEDDINGS"] == "1"
    # Isolation: config/lease/log state under the driver workdir.
    assert server["env"]["EXOMEM_CONFIG_PATH"].startswith(str(tmp_path / "wd"))


def test_driver_builds_fresh_session_argv() -> None:
    cfg = Path("/tmp/mcp.json")
    argv = build_argv("What is the delivery deadline?", mcp_config=cfg, mode="natural")
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    assert "What is the delivery deadline?" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(cfg)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # Fresh session: never resume/continue.
    assert "--continue" not in argv and "--resume" not in argv

    explicit = build_argv("What is the delivery deadline?", mcp_config=cfg, mode="explicit")
    prompt = explicit[2]
    assert prompt.startswith(EXPLICIT_PREFIX)
    assert prompt.endswith("What is the delivery deadline?")


def test_driver_parses_canned_transcript_into_answer_record(tmp_path: Path) -> None:
    invocations: list[list[str]] = []

    def runner(argv: list[str]) -> SimpleNamespace:
        invocations.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(_transcript_lines(with_tool_use=True)),
            stderr="",
        )

    config_path = write_mcp_config(
        tmp_path / "mcp.json", vault=tmp_path / "vault", workdir=tmp_path / "wd"
    )
    execution = run_case(
        "What is the delivery deadline?",
        query_id="QRY-NPD1",
        mcp_config=config_path,
        mode="natural",
        runner=runner,
    )
    assert invocations and invocations[0][0] == "claude"
    assert execution.harness_fault is False
    assert execution.harness_fault_reason is None

    answer = execution.answer
    assert answer is not None
    assert answer.query_id == "QRY-NPD1"
    assert "2026-03-04" in answer.answer_text
    assert "SRC-AAAA1111" in answer.citations
    assert answer.latency_ms == 4321.0
    assert answer.raw is not None
    assert answer.raw["usage"]["output_tokens"] == 22
    assert answer.raw["total_cost_usd"] == 0.0123

    # Witness inputs for the two-witness join.
    assert execution.tool_uses == [
        ("mcp__exomem__ask_memory", {"query": "delivery deadline", "limit": 5})
    ]
    verdict = join_case("QRY-NPD1", transcript=execution.transcript, server_events=[])
    assert verdict.verdict == "WITNESS_MISMATCH"


def test_driver_nonzero_exit_is_a_harness_fault_never_an_answer(tmp_path: Path) -> None:
    """Auth failure / rate limit / rejected mcp-config: claude exits non-zero
    with an empty transcript. That is a HARNESS fault — no scoreable
    AnswerRecord may exist for the gates to fail."""

    def runner(argv: list[str]) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="Invalid API key")

    config_path = write_mcp_config(
        tmp_path / "mcp.json", vault=tmp_path / "vault", workdir=tmp_path / "wd"
    )
    execution = run_case(
        "What is the delivery deadline?",
        query_id="QRY-F1",
        mcp_config=config_path,
        runner=runner,
    )
    assert execution.harness_fault is True
    assert execution.answer is None  # structurally unscorable
    reason = execution.harness_fault_reason or ""
    assert "exit" in reason
    assert "Invalid API key" in reason


def test_driver_error_transcript_is_a_harness_fault(tmp_path: Path) -> None:
    lines = _transcript_lines(with_tool_use=False)
    result = json.loads(lines[-1])
    result["subtype"] = "error_during_execution"
    lines[-1] = json.dumps(result)

    def runner(argv: list[str]) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="\n".join(lines), stderr="")

    config_path = write_mcp_config(
        tmp_path / "mcp.json", vault=tmp_path / "vault", workdir=tmp_path / "wd"
    )
    execution = run_case(
        "What is the delivery deadline?",
        query_id="QRY-F2",
        mcp_config=config_path,
        runner=runner,
    )
    assert execution.harness_fault is True
    assert execution.answer is None
    assert "error" in (execution.harness_fault_reason or "")
