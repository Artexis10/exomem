"""Judge-layer contract: default OFF, blind by construction, gates FINAL.

Lean and offline — no model, no network, no corpus generation. Run dirs are
built by hand mimicking runner.py's exact artifact layout.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from membench.judge import (
    ClaudeCliBackend,
    LeakageError,
    NoneBackend,
    OpenAICompatBackend,
    RequestItem,
    collect_responses,
    default_backend,
    deterministic_permutation,
    leakage_scan,
    load_requests,
    make_judge_item,
    write_requests,
)
from membench.judge.blinding import BlindingMap
from membench.reporting import (
    GATE_CONFLICT_NOTE,
    build_comparison_report,
    merge_judge_scores,
)

_DIMENSIONS_OK = {
    "factual_qa": {"pass": 1, "fail": 0, "not_applicable": 0, "unsupported": 0},
    "governance": {"pass": 0, "fail": 1, "not_applicable": 0, "unsupported": 0},
    "_run": {"failures": 0, "queries_scored": 1},
}

_PER_QUERY_OK = [
    {
        "query_id": "Q-0001",
        "status": "ok",
        "gates": [
            {"gate": "value", "dimension": "factual_qa", "status": "pass", "evidence": None},
            {
                "gate": "no_leak",
                "dimension": "governance",
                "status": "fail",
                "evidence": "leaked: ['w']",
            },
        ],
        "retrieval": {
            "relevant": ["S-1"],
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "mrr": 1.0,
            "first_relevant_rank": 1,
            "hit_count": 3,
        },
    }
]


def _make_run_dir(
    tmp_path: Path,
    run_id: str,
    *,
    provider: str = "provider-one",
    profile: str = "lexical",
    invalid: bool = False,
    invalid_reason: str | None = None,
    dimensions: dict | None = None,
    per_query: list | None = None,
    latencies: tuple[float, ...] = (10.0, 20.0, 30.0),
) -> Path:
    """Fake run dir with runner.py's exact filenames and shapes."""

    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "provider": provider,
        "profile": {"name": profile, "settings": {}},
        "top_k": 10,
        "corpus_dir": "corpus/s1",
        "started_utc": "20260101T000000Z",
        "ended_utc": "20260101T000001Z",
        "invalid": invalid,
        "invalid_reason": invalid_reason,
        "run_failures": 0,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "failures.jsonl").write_text("", encoding="utf-8")
    if not invalid:
        (run_dir / "deterministic-scores.json").write_text(
            json.dumps(
                {
                    "dimensions": dimensions if dimensions is not None else _DIMENSIONS_OK,
                    "per_query": per_query if per_query is not None else _PER_QUERY_OK,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with (run_dir / "retrieval.jsonl").open("w", encoding="utf-8") as handle:
            for index, latency in enumerate(latencies):
                handle.write(
                    json.dumps(
                        {"query_id": f"Q-{index:04d}", "latency_ms": latency, "hits": []},
                        sort_keys=True,
                    )
                    + "\n"
                )
        (run_dir / "answers.jsonl").write_text(
            json.dumps({"query_id": "Q-0001", "answer_text": "w", "citations": []}) + "\n",
            encoding="utf-8",
        )
    return run_dir


def _clean_judge_items(count: int = 1) -> list[RequestItem]:
    blinding = BlindingMap.mint(["provider-one", "provider-two"], seed="run-42")
    token = blinding.token_for("provider-one")
    return [
        make_judge_item(
            f"Q-{index:04d}",
            question="When did the deadline move?",
            expected_summary="It moved to week nine.",
            candidate_answer="The deadline moved to week nine.",
            provider_token=token,
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------- default off


def test_default_backend_is_none_and_flow_needs_no_judge_artifacts(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "run-plain")
    backend = default_backend()
    assert isinstance(backend, NoneBackend)
    outcome = backend.run_phase(run_dir, "judge", _clean_judge_items(), samples=3)
    assert outcome.status == "not_run"
    assert "judge: not run" in outcome.note
    assert not (run_dir / "judge-requests").exists()
    assert not (run_dir / "judge-responses").exists()
    assert not (run_dir / "judge-scores.json").exists()

    out = build_comparison_report([run_dir], tmp_path / "compare.md")
    report = out.read_text(encoding="utf-8")
    assert "factual_qa" in report
    assert "Judge: not run" in report
    assert "gate_conflict" not in report


# ------------------------------------------------------------------- blinding


def test_serialized_requests_are_blind_grep_the_bytes(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "run-blind")
    blinding = BlindingMap.mint(["provider-one", "provider-two"], seed="run-42")
    item = make_judge_item(
        "Q-0001",
        question=(
            "What does [ref:SRC-AAAA1111] say about the deadline in "
            "Knowledge Base/Notes/plan-notes.md?"
        ),
        expected_summary=(
            "Per SRC-AAAA1111 the deadline moved; see exomem://notes/plan "
            "and roadmap.md for details."
        ),
        candidate_answer=(
            "Exomem and basic-memory (also mem0, GrayBox) report the deadline "
            "moved, per [ref:SRC-AAAA1111]."
        ),
        provider_token=blinding.token_for("provider-one"),
    )
    batch = write_requests(run_dir, "judge", [item], samples=2, seed="run-42")

    raw = batch.read_bytes().decode("utf-8")
    lowered = raw.lower()
    for forbidden in (
        "exomem",  # covers exomem:// refs too
        "mem0",
        "graybox",
        "basic-memory",
        "basic memory",
        "basic_memory",
        "[ref:",
        "src-",
        "knowledge base",
        "knowledge_base",
        ".md",
    ):
        assert forbidden not in lowered, f"provider-identifying bytes leaked: {forbidden!r}"

    # Blinded shape is present: neutral tokens + a system token, nothing raw.
    assert "[ctx:1]" in raw
    assert '"blinded_provider_token": "system-' in raw

    # Stable per-source numbering within one request: the same source id maps
    # to the same [ctx:N] in question, expected summary, and candidate answer.
    payload = json.loads(raw.splitlines()[0])["payload"]
    tokens_q = set(re.findall(r"\[ctx:\d+\]", payload["question"]))
    tokens_e = set(re.findall(r"\[ctx:\d+\]", payload["expected_summary"]))
    tokens_c = set(re.findall(r"\[ctx:\d+\]", payload["candidate_answer"]))
    assert "[ctx:1]" in tokens_q and "[ctx:1]" in tokens_e and "[ctx:1]" in tokens_c


def test_leakage_scan_catches_leaks_and_writer_refuses(tmp_path: Path) -> None:
    leaks = leakage_scan(
        "Answer per [ref:SRC-BBBB2222] in Knowledge Base/x and exomem://n/p via mem0."
    )
    assert leaks, "planted leaks must be detected"
    joined = " ".join(leaks).lower()
    for expected in ("[ref:", "src-bbbb2222", "knowledge base/", "exomem", "mem0"):
        assert expected in joined

    assert leakage_scan("The deadline moved to week nine per [ctx:1].") == []

    run_dir = _make_run_dir(tmp_path, "run-leaky")
    leaky = RequestItem(
        item_id="Q-0001",
        blinded_provider_token="system-A",
        payload={"prompt": "grade this: exomem said so, see [ref:SRC-BBBB2222]"},
    )
    with pytest.raises(LeakageError) as excinfo:
        write_requests(run_dir, "judge", [leaky], samples=2, seed="s")
    assert "Q-0001" in str(excinfo.value)
    assert not (run_dir / "judge-requests").exists(), "refusal must write nothing"


# -------------------------------------------------------------- order shuffle


def test_order_shuffle_deterministic_per_seed(tmp_path: Path) -> None:
    perm = deterministic_permutation(16, "alpha")
    assert perm == deterministic_permutation(16, "alpha")
    assert sorted(perm) == list(range(16))
    assert perm != deterministic_permutation(16, "beta")

    items = _clean_judge_items(count=4)
    run_a = _make_run_dir(tmp_path, "run-ord-a")
    run_b = _make_run_dir(tmp_path, "run-ord-b")
    run_c = _make_run_dir(tmp_path, "run-ord-c")
    bytes_a = write_requests(run_a, "judge", items, samples=2, seed="alpha").read_bytes()
    bytes_b = write_requests(run_b, "judge", items, samples=2, seed="alpha").read_bytes()
    bytes_c = write_requests(run_c, "judge", items, samples=2, seed="beta").read_bytes()
    assert bytes_a == bytes_b, "same seed must serialize identically"

    def order(raw: bytes) -> list[tuple[str, int]]:
        return [
            (row["request_id"], row["sample_index"])
            for row in map(json.loads, raw.decode("utf-8").splitlines())
        ]

    assert order(bytes_a) != order(bytes_c), "different seed must reorder"
    assert sorted(order(bytes_a)) == sorted(order(bytes_c)), "same requests either way"


# ------------------------------------------- N samples, collect, denominators


def _verdict(match: bool, quality: int, reason: str = "because") -> str:
    return json.dumps(
        {"semantic_match": match, "explanation_quality": quality, "reason": reason}
    )


def test_sample_expansion_collect_and_merge_keep_denominators(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "run-samples")
    items = _clean_judge_items(count=2)  # Q-0000, Q-0001
    write_requests(run_dir, "judge", items, samples=3, seed="s5")

    requests = load_requests(run_dir, "judge")
    assert len(requests) == 6
    by_id: dict[str, set[int]] = {}
    for request in requests:
        by_id.setdefault(request.request_id, set()).add(request.sample_index)
    assert by_id == {"Q-0000": {0, 1, 2}, "Q-0001": {0, 1, 2}}

    responses_dir = run_dir / "judge-responses"
    responses_dir.mkdir()
    lines = [
        json.dumps(
            {
                "request_id": "Q-0000",
                "sample_index": 0,
                "model_id": "judge-model-x",
                "response": _verdict(True, 4),
            }
        ),
        json.dumps(
            {
                "request_id": "Q-0000",
                "sample_index": 1,
                "model_id": "judge-model-x",
                "response": _verdict(True, 5),
            }
        ),
        json.dumps(
            {
                "request_id": "Q-0000",
                "sample_index": 2,
                "model_id": "judge-model-x",
                "response": _verdict(False, 3),
            }
        ),
        json.dumps(
            {
                "request_id": "Q-0001",
                "sample_index": 0,
                "model_id": "judge-model-x",
                "response": _verdict(True, 2),
            }
        ),
        "{this is not json",  # malformed response LINE
        json.dumps(
            {
                "request_id": "Q-0001",
                "sample_index": 2,
                "model_id": "judge-model-x",
                "response": "I think it matches, quality four.",  # invalid verdict
            }
        ),
        # Q-0001 sample 1 never gets a valid response -> missing
    ]
    (responses_dir / "batch-001.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )

    paired, stats = collect_responses(run_dir, "judge")
    assert stats["requests"] == 6
    assert stats["paired"] == 5
    assert stats["malformed"] == 1
    assert stats["missing_response"] == 1

    failures = [
        json.loads(line)
        for line in (run_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(1 for f in failures if "malformed" in f["detail"]) == 1
    assert sum(1 for f in failures if "missing response" in f["detail"]) == 1

    out = merge_judge_scores(run_dir, paired)
    scores = json.loads(out.read_text(encoding="utf-8"))
    rows = {row["query_id"]: row for row in scores["per_query"]}

    q0 = rows["Q-0000"]
    assert q0["samples_total"] == 3 and q0["samples_valid"] == 3
    assert q0["mean"] == pytest.approx(4.0)
    assert q0["stdev"] == pytest.approx(1.0)
    assert q0["majority"] is True  # 2 of 3 matched
    assert [s["sample_index"] for s in q0["samples"]] == [0, 1, 2]

    q1 = rows["Q-0001"]
    assert q1["samples_total"] == 3, "malformed/missing samples stay in the denominator"
    assert q1["samples_valid"] == 1
    assert q1["semantic_matches"] == 1
    assert q1["majority"] is False, "1 match of 3 total (errors never count as matches)"
    assert q1["mean"] == pytest.approx(2.0)
    errors = [s for s in q1["samples"] if "error" in s]
    assert len(errors) == 2  # one missing/malformed, one invalid verdict

    # invalid verdict was recorded as a failure too
    failures = (run_dir / "failures.jsonl").read_text(encoding="utf-8")
    assert "judge-verdict" in failures


# ----------------------------------------------- gate conflict + invalid runs


def test_gate_conflict_annotation_and_invalid_run_rendering(tmp_path: Path) -> None:
    run_ok = _make_run_dir(tmp_path, "run-conflict", provider="provider-one")
    (run_ok / "judge-scores.json").write_text(
        json.dumps(
            {
                "per_query": [
                    {
                        "query_id": "Q-0001",
                        "samples": [
                            {
                                "sample_index": 0,
                                "model_id": "judge-model-x",
                                "semantic_match": True,
                                "explanation_quality": 4,
                                "reason": "matches",
                            }
                        ],
                        "samples_total": 1,
                        "samples_valid": 1,
                        "semantic_matches": 1,
                        "majority": True,
                        "mean": 4.0,
                        "stdev": None,
                    }
                ],
                "meta": {"kind": "judge", "queries": 1},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    run_bad = _make_run_dir(
        tmp_path,
        "run-broken",
        provider="provider-two",
        invalid=True,
        invalid_reason="environment: simulated fault",
    )

    out = build_comparison_report([run_ok, run_bad], tmp_path / "compare.md")
    report = out.read_text(encoding="utf-8")

    # Judge said match, deterministic no_leak gate failed -> annotated conflict.
    assert GATE_CONFLICT_NOTE in report
    assert "no_leak" in report

    # The deterministic table still shows the fail and never judge values.
    dims_section = report.split("## Dimensions")[1].split("## Retrieval")[0]
    assert "fail=1" in dims_section
    assert "match" not in dims_section and "4.0" not in dims_section

    # Invalid run renders INVALID, never numbers.
    assert "INVALID: environment: simulated fault" in report
    for row in dims_section.splitlines():
        if row.startswith("| factual_qa") or row.startswith("| governance"):
            assert row.rstrip().endswith("INVALID |")

    # Latency stays in its own section with real numbers for the valid run.
    latency_section = report.split("## Latency")[1].split("## Failures")[0]
    assert "20.000" in latency_section  # median of 10/20/30
    assert "INVALID" in latency_section


# ------------------------------------------------------- skip, never fabricate


def test_claude_cli_backend_missing_binary_returns_skip_with_command(
    tmp_path: Path,
) -> None:
    run_dir = _make_run_dir(tmp_path, "run-cli")
    backend = ClaudeCliBackend(binary="claude-membench-not-a-real-binary")
    outcome = backend.run_phase(run_dir, "judge", _clean_judge_items(), samples=1, seed="s8")

    assert outcome.status == "skipped"
    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert result.status == "skip"
    assert result.response is None, "a missing binary must never fabricate a response"

    prompt = load_requests(run_dir, "judge")[0].payload["prompt"]
    expected = shlex.join(
        ["claude-membench-not-a-real-binary", "-p", prompt, "--output-format", "json"]
    )
    assert result.command == expected
    assert not (run_dir / "judge-responses").exists()


def test_openai_backend_unset_env_returns_skip_with_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEMBENCH_TEST_API_KEY", raising=False)
    run_dir = _make_run_dir(tmp_path, "run-http")
    backend = OpenAICompatBackend(
        base_url="https://models.invalid/v1",
        model="neutral-model",
        api_key_env="MEMBENCH_TEST_API_KEY",
    )
    outcome = backend.run_phase(run_dir, "judge", _clean_judge_items(), samples=1, seed="s9")

    assert outcome.status == "skipped"
    result = outcome.results[0]
    assert result.status == "skip" and result.response is None
    assert result.command is not None
    assert "https://models.invalid/v1/chat/completions" in result.command
    assert "$MEMBENCH_TEST_API_KEY" in result.command
    assert '\\"temperature\\": 0' in result.command or '"temperature": 0' in result.command
    assert not (run_dir / "judge-responses").exists()
