"""RB2-RB4, RM1, RM4, RM6, RM8: the runner's protocol contract, executed.

Every test here drives the real ``execute_run`` pipeline against the committed
mini fixture with the deterministic fixture embedder, so a claim about manifest
status, canary semantics, readiness, traces, or the budget ledger is a claim
about what the runner actually wrote to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import pytest

FIXTURE = Path("benchmarks/lme/fixtures/mini.json")
CASE_COUNT = 6


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fixture_env() -> dict[str, str]:
    return {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}


def _execute(out: Path, run_id: str, *, provider: str = "hybrid-rag-control", **kwargs):
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    with mock.patch.dict(os.environ, _fixture_env()):
        return execute_run(
            RunConfig(dataset=FIXTURE, out=out, reader_name="stub", run_id=run_id, provider=provider, **kwargs),
            reader=StubReader(),
        )


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def _install(monkeypatch: pytest.MonkeyPatch, factory) -> None:
    """Replace the closed registry lookup for the duration of one test."""

    from lme.providers import registry

    monkeypatch.setattr(registry, "provider_factory", lambda name: factory)


def _strict_validate(run_dir: Path) -> tuple[int, str]:
    from protocol import cli as protocol_cli
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = protocol_cli.main(["validate", "--run-dir", str(run_dir), "--strict"])
    return code, buffer.getvalue().strip()


# --------------------------------------------------------------------------
# Clean end-to-end demo (acceptance demo (a))
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory: pytest.TempPathFactory):
    return _execute(tmp_path_factory.mktemp("clean"), "clean-demo")


def test_clean_fixture_run_finalises_valid_with_measured_summaries(clean_run) -> None:
    manifest = _manifest(clean_run.run_dir)
    assert manifest["status"] == "VALID"
    assert manifest["contamination"] == "isolated"
    assert manifest["provider_variant"] == "hybrid-rag-fixture"
    assert manifest["control_config_sha256"]
    assert manifest["leakage"]["scanned_cases"] == CASE_COUNT
    assert manifest["leakage"]["invalidated_cases"] == 0
    assert sorted(manifest["namespaces"]) == sorted(
        row["question_id"] for row in json.loads((clean_run.run_dir / "dataset.json").read_text(encoding="utf-8"))
    )
    assert all(name.startswith("hybrid-") for name in manifest["namespaces"].values())
    code, output = _strict_validate(clean_run.run_dir)
    assert (code, output) == (0, "VALID")


def test_clean_run_records_every_known_answer_probe(clean_run) -> None:
    probes = _rows(clean_run.run_dir / "probes.jsonl")
    assert {row["probe_kind"] for row in probes} == {
        "lexical-rare-token", "semantic-zero-overlap", "update-current-state",
    }
    assert {row["outcome"] for row in probes} == {"pass"}
    assert all(row["detail"] for row in probes)


def test_budget_summary_is_read_back_from_the_on_disk_ledger(clean_run) -> None:
    """RB4: the summary is a ledger reading, never a placeholder triple of zeros."""

    from protocol.budget import BudgetLedger

    entries = _rows(clean_run.run_dir / "ledger.jsonl")
    assert entries, "no ledger was written"
    assert {entry["kind"] for entry in entries} == {"reserve", "commit"}
    ledger = BudgetLedger(clean_run.run_dir)
    manifest = _manifest(clean_run.run_dir)
    assert manifest["budget"]["cap_usd"] == ledger.caps["usd"]
    assert manifest["budget"]["committed_usd"] == round(
        sum(entry["units"] for entry in entries if entry["kind"] == "commit"), 8
    )
    assert manifest["budget"]["refusals"] == sum(entry["decision"] == "refused-cap" for entry in entries)


def test_the_budget_cap_reaches_the_ledger_from_the_cli_and_the_environment(tmp_path: Path) -> None:
    """RB4: the cap is an operator input, not a constant baked into the runner."""

    from lme import cli as cli_module

    with mock.patch.dict(os.environ, {**_fixture_env(), "PROTOCOL_BUDGET_CAP_USD": "2.5"}):
        assert cli_module.main([
            "run", "--dataset", str(FIXTURE), "--reader", "stub", "--out", str(tmp_path),
            "--run-id", "cap-from-env", "--provider", "hybrid-rag-control",
        ]) == 0
        assert cli_module.main([
            "run", "--dataset", str(FIXTURE), "--reader", "stub", "--out", str(tmp_path),
            "--run-id", "cap-from-flag", "--provider", "hybrid-rag-control", "--budget-cap-usd", "7.25",
        ]) == 0
    assert json.loads((tmp_path / "cap-from-env" / "budget.json").read_text(encoding="utf-8"))["caps"]["usd"] == 2.5
    assert _manifest(tmp_path / "cap-from-env")["budget"]["cap_usd"] == 2.5
    assert _manifest(tmp_path / "cap-from-flag")["budget"]["cap_usd"] == 7.25


def test_traces_carry_per_session_ingest_search_answer_and_cleanup_records(clean_run) -> None:
    """RM6: the ingest sha is the sha of the rendered neutral session actually sent."""

    from lme.dataset import load_dataset
    from lme.normalize import render_neutral_session, neutralize
    from protocol.models import DatasetIdentity
    from protocol.trace import CaseTraceReader

    dataset = load_dataset(FIXTURE)
    identity = DatasetIdentity(
        id="longmemeval", variant="LongMemEval-S cleaned September 2025",
        source="xiaowu0162/longmemeval-cleaned", revision="fixture-local",
        sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), case_count=CASE_COUNT,
    )
    question = dataset.questions[0]
    records = list(CaseTraceReader(clean_run.run_dir, question.question_id))
    kinds = [record.record for record in records]
    assert kinds.count("ingest") == len(question.sessions) + 1, "one record per session plus the canary session"
    assert {"search", "timing", "answer", "cleanup"} <= set(kinds)
    assert [record for record in records if record.record == "cleanup"][0].verified is True
    ingests = [record for record in records if record.record == "ingest"]
    assert [record.session_ordinal for record in ingests] == sorted(record.session_ordinal for record in ingests)
    events = neutralize(question, identity)
    expected = hashlib.sha256(
        render_neutral_session([event for event in events if event.session_ordinal == 1]).encode()
    ).hexdigest()
    assert ingests[0].payload_sha256 == expected
    answer = [record for record in records if record.record == "answer"][0]
    assert answer.model_id == "offline deterministic stub"


# --------------------------------------------------------------------------
# RM8: ordering, canary authorship, readiness, budget, null floor
# --------------------------------------------------------------------------


def test_manifest_exists_before_any_provider_is_constructed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RM8: constructor side-effect guard — a provider must never precede the manifest."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider

    observed: list[bool] = []

    class _Observing(HybridRagDirectProvider):
        def __init__(self) -> None:
            observed.append((tmp_path / "ordering" / "manifest.json").is_file())
            super().__init__()

    _install(monkeypatch, _Observing)
    _execute(tmp_path, "ordering")
    assert observed, "provider was never constructed"
    assert observed[0] is False, "the first construction happens while choosing the variant"
    assert all(observed[1:]), "every per-case provider was constructed after the manifest existed"
    assert _manifest(tmp_path / "ordering")["status"] == "VALID"


def test_the_canary_is_a_harness_authored_filler_event_that_passes_the_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RB3(c): a new event, never a mutated dataset payload."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider

    seen: list[list] = []

    class _Recording(HybridRagDirectProvider):
        def ingest_case(self, events, handle):
            seen.append(list(events))
            return super().ingest_case(events, handle)

    _install(monkeypatch, _Recording)
    _execute(tmp_path, "canary-authorship")
    case_batches = [batch for batch in seen if not batch[0].case_id.startswith("__probe__")]
    assert len(case_batches) == CASE_COUNT
    for batch in case_batches:
        planted = [event for event in batch if event.provenance.converter == "harness-canary"]
        assert len(planted) == 1, "exactly one harness-authored filler event per case"
        canary = planted[0]
        assert "canary-presence-" in canary.content
        assert canary.session_ordinal > max(event.session_ordinal for event in batch if event is not canary)
        for event in batch:
            assert event.content_sha256 == hashlib.sha256(event.content.encode()).hexdigest(), (
                "a dataset event's content or sha was mutated"
            )
        dataset_events = [event for event in batch if event.provenance.converter != "harness-canary"]
        assert all("canary-" not in event.content for event in dataset_events)

    # RB3(c): the planted event must survive the leakage scanner's contract —
    # its text goes through the authored-literal group and fires no
    # gold/label/identifier detector.
    from lme.dataset import load_dataset
    from lme.normalize import ingest_field_groups
    from protocol.leakage import scan_ingest
    from protocol.models import CaseGold, CaseHandle

    questions = {question.question_id: question for question in load_dataset(FIXTURE).questions}
    for batch in case_batches:
        question = questions[batch[0].case_id]
        handle = CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text)
        canary = next(event for event in batch if event.provenance.converter == "harness-canary")
        content_fields, authored_literals, harness_fields = ingest_field_groups(batch, handle)
        findings = scan_ingest(
            content_fields, {**authored_literals, "harness_canary": canary.content}, harness_fields,
            CaseGold(
                case_id=question.question_id, answer=question.answer,
                answer_session_ids=list(question.answer_session_ids),
                question_type=question.question_type, question=question.question,
            ),
            raw_upstream_session_ids=[session.session_id for session in question.sessions],
        )
        offending = [finding.detector for finding in findings if finding.detector != "question-text"]
        assert offending == [], f"the harness canary tripped {offending}"


def test_the_null_control_floors_every_metric_end_to_end(tmp_path: Path) -> None:
    """RM8: the negative control completes and retrieves nothing, so it can be a floor."""

    result = _execute(tmp_path, "null-floor", provider="no-memory")
    manifest = _manifest(result.run_dir)
    assert manifest["status"] == "VALID"
    assert manifest["contamination"] == "isolated"
    assert manifest["provider_variant"] == "no-memory"
    probes = _rows(result.run_dir / "probes.jsonl")
    assert {row["outcome"] for row in probes} == {"inconclusive-by-design"}
    hypotheses = _rows(result.run_dir / "hypotheses.jsonl")
    assert len(hypotheses) == CASE_COUNT
    equivalence = json.loads((result.run_dir / "equivalence.json").read_text(encoding="utf-8"))
    assert all(case["retrieved_ids"] == [] for case in equivalence["cases"])
    assert all(case["retrieved_text"] == [] for case in equivalence["cases"])
    assert all(case["packed_context"] == "" for case in equivalence["cases"])
    assert _strict_validate(result.run_dir)[0] == 0


# --------------------------------------------------------------------------
# RB2 / RB3 / RB4 / RM1: every path that must refuse a VALID verdict
# --------------------------------------------------------------------------


def test_a_provider_whose_retrieve_raises_never_finalises_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.runner import LmeRunInvalid

    class _Crashing(HybridRagDirectProvider):
        def ingest_case(self, events, handle):
            self._case_id = handle.case_id
            return super().ingest_case(events, handle)

        def retrieve(self, question_text, top_k):
            if not self._case_id.startswith("__probe__"):
                raise RuntimeError("provider retrieval exploded")
            return super().retrieve(question_text, top_k)

    _install(monkeypatch, _Crashing)
    with pytest.raises(LmeRunInvalid, match="provider failure"):
        _execute(tmp_path, "crashing")
    manifest = _manifest(tmp_path / "crashing")
    assert manifest["status"] == "INVALID"
    assert _strict_validate(tmp_path / "crashing")[1] == "INVALID"
    failures = _rows(tmp_path / "crashing" / "failures.jsonl")
    assert failures and failures[0]["detail"].startswith("RuntimeError")


def test_a_poisoned_cross_case_canary_invalidates_the_run_and_strict_validate_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance demo (b): a real cross-case leak, refused end to end."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.runner import LmeRunInvalid
    from protocol.canary import canary_for

    run_id = "poisoned-demo"

    class _CrossCaseLeak(HybridRagDirectProvider):
        def ingest_case(self, events, handle):
            inserted = super().ingest_case(events, handle)
            if handle.case_id.startswith("__probe__"):
                return inserted
            token = canary_for(run_id, handle.case_id + "-other", "cross_case")
            content = f"Neighbouring case material crossed the namespace boundary. Token: {token}."
            leak = events[0].model_copy(
                update={
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "session_ordinal": max(event.session_ordinal for event in events) + 1,
                    "ingestion_ordinal": max(event.ingestion_ordinal for event in events) + 1,
                }
            )
            return inserted + super().ingest_case([leak], handle)

    _install(monkeypatch, _CrossCaseLeak)
    with pytest.raises(LmeRunInvalid, match="contaminat"):
        _execute(tmp_path, run_id)
    manifest = _manifest(tmp_path / run_id)
    assert manifest["contamination"] == "contaminated"
    assert manifest["status"] != "VALID"
    assert manifest["status"] == "INVALID"
    code, output = _strict_validate(tmp_path / run_id)
    assert code == 0 and output == "INVALID"

    # The false-green shape the review found: contaminated but claiming VALID.
    poisoned = json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))
    poisoned["status"] = "VALID"
    false_green = tmp_path / "false-green"
    false_green.mkdir()
    (false_green / "manifest.json").write_text(json.dumps(poisoned), encoding="utf-8")
    code, output = _strict_validate(false_green)
    assert code == 2
    assert "contaminated" in output


@pytest.mark.parametrize(
    ("lane_kwargs", "expected_status"),
    [
        ({"requested": True, "verified": False, "method": "config-state", "evidence": "index never confirmed"}, "INVALID"),
        ({"requested": True, "verified": True, "method": "config-state", "evidence": "served", "fallback_detected": True}, "INVALID"),
        ({"requested": True, "verified": False, "method": "readiness-unverifiable", "evidence": "provider exposes no completion signal"}, "READINESS_UNVERIFIABLE"),
    ],
    ids=["verified-false", "fallback-detected", "unverifiable"],
)
def test_readiness_propagates_into_the_terminal_manifest_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_kwargs: dict, expected_status: str
) -> None:
    """RM1: the full per-case readiness list decides the run's terminal status."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.runner import LmeRunInvalid
    from protocol.models import LaneReadiness

    class _Readiness(HybridRagDirectProvider):
        def readiness(self):
            return [LaneReadiness(lane="semantic-store", **lane_kwargs)]

    _install(monkeypatch, _Readiness)
    if expected_status == "INVALID":
        with pytest.raises(LmeRunInvalid):
            _execute(tmp_path, "readiness")
    else:
        _execute(tmp_path, "readiness")
    manifest = _manifest(tmp_path / "readiness")
    assert manifest["status"] == expected_status
    assert any(lane["lane"] == "semantic-store" for lane in manifest["readiness"])
    if expected_status != "INVALID":
        # A non-VALID terminal status must still refuse to masquerade as VALID.
        assert _strict_validate(tmp_path / "readiness")[1] == expected_status


def test_a_failed_semantic_probe_invalidates_a_run_that_requested_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RB4: lexical passing does not license a semantic claim."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.runner import LmeRunInvalid

    class _SemanticBlind(HybridRagDirectProvider):
        def retrieve(self, question_text, top_k):
            if question_text.startswith("Retrieve the meaning-preserving"):
                return []
            return super().retrieve(question_text, top_k)

    _install(monkeypatch, _SemanticBlind)
    with pytest.raises(LmeRunInvalid, match="semantic known-answer readiness probe failed"):
        _execute(tmp_path, "semantic-blind")
    manifest = _manifest(tmp_path / "semantic-blind")
    assert manifest["status"] == "INVALID"
    probes = _rows(tmp_path / "semantic-blind" / "probes.jsonl")
    outcomes = {row["probe_kind"]: row["outcome"] for row in probes}
    assert outcomes["lexical-rare-token"] == "pass"
    assert outcomes["semantic-zero-overlap"] == "fail"


# --------------------------------------------------------------------------
# RM4: the runner emits the differ's input for two REAL run directories
# --------------------------------------------------------------------------


def test_two_real_runs_of_the_same_dataset_pass_the_equivalence_gate(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    left = _execute(tmp_path, "gate-left").run_dir
    right = _execute(tmp_path, "gate-right").run_dir
    payload = json.loads((left / "equivalence.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "equivalence-input.v1"
    assert len(payload["cases"]) == CASE_COUNT
    from equivalence.differ import EQUIVALENCE_KEYS

    assert all(set(EQUIVALENCE_KEYS) <= set(case) for case in payload["cases"])
    result = compare_runs(left, right, mode="blocking", out=tmp_path / "gate-clean")
    assert not result.blocking, "two honest runs of one dataset must not trip a blocking key"
    blocking = {diff.field for diff in result.diffs if diff.classification == "blocking"}
    assert blocking == set(), f"identity/config keys must be run-invariant, saw {sorted(blocking)}"
    # The run-scoped canary is deliberately unique per run, so it shows up in
    # the content keys — as REPORTED observations, never as a gate failure.
    assert {diff.classification for diff in result.diffs} <= {"reported"}


def test_a_perturbed_real_run_pair_is_classified_by_the_gate(tmp_path: Path) -> None:
    left = _execute(tmp_path, "perturb-left", top_k=10).run_dir
    right = _execute(tmp_path, "perturb-right", top_k=4).run_dir
    from equivalence.differ import compare_runs

    result = compare_runs(left, right, mode="blocking", out=tmp_path / "gate-perturbed")
    fields = {diff.field for diff in result.diffs}
    assert "top_k" in fields
    assert result.blocking, "a top_k difference is an identity/config difference and must block"
    assert all(diff.case_id != "run" for diff in result.diffs), "diffs are attributed per case"
