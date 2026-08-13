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
from dataclasses import replace
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


def _feedback6_config(out: Path, run_id: str, *, provider: str = "fixture"):
    from lme.runner import RunConfig

    return RunConfig(dataset=FIXTURE, out=out, run_id=run_id, provider=provider)


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def _direct_session(run_dir: Path, logical_question_id: str | None) -> str:
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    matches = [
        str(item["internal_session_id"])
        for item in environment["lme"]["lifecycle_attempts"]
        if item["logical_question_id"] == logical_question_id
    ]
    assert len(matches) == 1
    return matches[0]


def _install(monkeypatch: pytest.MonkeyPatch, factory) -> None:
    """Replace only the factory in the direct provider specification."""

    from lme.providers import registry
    from lme.providers.base import ProviderSpec

    original = registry.provider_spec

    def replacement(name: str) -> ProviderSpec:
        spec = original(name)
        return ProviderSpec(
            factory=factory,
            descriptor=spec.descriptor,
            namespace_kind=spec.namespace_kind,
            derive_namespace=spec.derive_namespace,
            runtime_binding=spec.runtime_binding,
        )

    monkeypatch.setattr(registry, "provider_spec", replacement)


def _feedback6_install(monkeypatch: pytest.MonkeyPatch, factory) -> None:
    """Replace only the factory while preserving every static descriptor field."""
    from lme.providers import registry

    original = registry.provider_spec("hybrid-rag-control")
    monkeypatch.setattr(registry, "provider_spec", lambda _name: replace(original, factory=factory))


def test_legacy_provider_factory_is_a_compatible_inert_spec_accessor() -> None:
    from lme.providers.registry import provider_factory, provider_spec

    factory = provider_factory("hybrid-rag-control")
    assert factory is provider_spec("hybrid-rag-control").factory


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
    assert manifest["schema_version"] == 2
    assert manifest["preregistration_identity"]["original"]["sha256"] == (
        "21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3"
    )
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
    outcomes = {row["probe_kind"]: row["outcome"] for row in probes}
    assert outcomes == {
        "lexical-rare-token": "pass",
        "semantic-zero-overlap": "pass",
        "update-current-state": "superseded",
    }
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
    records = list(CaseTraceReader(
        clean_run.run_dir,
        _direct_session(clean_run.run_dir, question.question_id),
    ))
    kinds = [record.record for record in records]
    assert kinds.count("ingest") == len(question.sessions) + 1, "one record per session plus the canary session"
    assert {"search", "timing", "answer", "cleanup"} <= set(kinds)
    cleanup = [record for record in records if record.record == "cleanup"][0]
    assert cleanup.observation_path.startswith("evidence/")
    assert len(cleanup.observation_sha256) == 64
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
    assert all(observed), "every provider construction must happen after the derived-identity manifest"
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

        def retrieve(self, question_text, top_k, purpose):
            if not self._case_id.startswith("__probe__"):
                raise RuntimeError("provider retrieval exploded")
            return super().retrieve(question_text, top_k, purpose)

    _install(monkeypatch, _Crashing)
    with pytest.raises(LmeRunInvalid, match="provider failure"):
        _execute(tmp_path, "crashing")
    manifest = _manifest(tmp_path / "crashing")
    assert manifest["status"] == "INVALID"
    assert _strict_validate(tmp_path / "crashing")[1] == "INVALID"
    failures = _rows(tmp_path / "crashing" / "failures.jsonl")
    assert failures and failures[0]["detail"] == "provider_operation_failed"


def test_a_poisoned_cross_case_canary_invalidates_the_run_and_strict_validate_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance demo (b): a real cross-case leak, refused end to end."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.providers.base import ProviderHit, RetrievalPurpose
    from lme.runner import LmeRunInvalid
    run_id = "poisoned-demo"

    class _CrossCaseLeak(HybridRagDirectProvider):
        shared_tokens: list[str] = []

        def ingest_case(self, events, handle):
            inserted = super().ingest_case(events, handle)
            token = next((event.content.rsplit(": ", 1)[-1].rstrip(".") for event in events if "canary-presence-" in event.content), None)
            if token is not None:
                self.shared_tokens.append(token)
            return inserted

        def retrieve(self, question_text, top_k, purpose):
            if purpose is RetrievalPurpose.ABSENCE_PROBE_EXPECTED_EMPTY and question_text in self.shared_tokens:
                return [ProviderHit(hit_id="leak", text=question_text, score=1.0)]
            return super().retrieve(question_text, top_k, purpose)

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
    assert "lifecycle" in output or "environment" in output


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
    from protocol.probes import known_answer_probe_specs

    semantic_query = next(spec.query for spec in known_answer_probe_specs() if spec.kind == "semantic-zero-overlap")

    class _SemanticBlind(HybridRagDirectProvider):
        def retrieve(self, question_text, top_k, purpose):
            if question_text == semantic_query:
                return []
            return super().retrieve(question_text, top_k, purpose)

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


# --------------------------------------------------------------------------
# Recheck residuals: write ordering, and isolation claimed without probes
# --------------------------------------------------------------------------


class _FixtureAdapter:
    """The legacy no-provider path, driven without a real product vault."""

    def run_question(self, question, workdir, *, dataset_identity, case_ordinal, limit=10):
        del dataset_identity, case_ordinal, limit
        from lme.dataset import render_session
        from membench.adapters.base import OpResult

        Path(workdir).mkdir(parents=True, exist_ok=True)
        self.last_ingest_results = tuple(
            OpResult(seq=index, op="capture_source", source_id=session.session_id, ok=True, latency_ms=float(index + 1))
            for index, session in enumerate(question.sessions)
        )
        return [render_session(session) for session in question.sessions]


def _poisoning_provider(_run_id: str):
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.providers.base import ProviderHit, RetrievalPurpose

    class _CrossCaseLeak(HybridRagDirectProvider):
        shared_tokens: list[str] = []

        def ingest_case(self, events, handle):
            inserted = super().ingest_case(events, handle)
            token = next((event.content.rsplit(": ", 1)[-1].rstrip(".") for event in events if "canary-presence-" in event.content), None)
            if token is not None:
                self.shared_tokens.append(token)
            return inserted

        def retrieve(self, question_text, top_k, purpose):
            if purpose is RetrievalPurpose.ABSENCE_PROBE_EXPECTED_EMPTY and question_text in self.shared_tokens:
                return [ProviderHit(hit_id="leak", text=question_text, score=1.0)]
            return super().retrieve(question_text, top_k, purpose)

    return _CrossCaseLeak


def test_a_poisoned_run_ships_an_invalid_report_and_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contamination verdict must reach run.json and report.md, not only the manifest."""

    from lme.runner import LmeRunInvalid

    run_id = "ordering-poisoned"
    _install(monkeypatch, _poisoning_provider(run_id))
    with pytest.raises(LmeRunInvalid, match="contaminat"):
        _execute(tmp_path, run_id)
    run_dir = tmp_path / run_id
    legacy = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert legacy["invalid"] is True, "run.json shipped a false green for a contaminated run"
    assert legacy["invalid_reason"], "run.json recorded no reason for an invalidated run"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "INVALID" in report, "report.md shipped without an INVALID banner"
    assert "contamination=contaminated" in report


@pytest.mark.parametrize("poisoned", [False, True], ids=["clean", "poisoned"])
def test_all_three_report_entry_points_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poisoned: bool
) -> None:
    """RM5: runner-written, artifact-only, and judge re-rendered reports are one text."""

    from lme.judge_io import rerender_report
    from lme.report import render_run_report
    from lme.runner import LmeRunInvalid

    run_id = "agreement-poisoned" if poisoned else "agreement-clean"
    if poisoned:
        _install(monkeypatch, _poisoning_provider(run_id))
        with pytest.raises(LmeRunInvalid):
            _execute(tmp_path, run_id)
        run_dir = tmp_path / run_id
    else:
        run_dir = _execute(tmp_path, run_id).run_dir
    written = (run_dir / "report.md").read_text(encoding="utf-8")
    assert render_run_report(run_dir, offline=True) == written
    rerender_report(run_dir)
    assert (run_dir / "report.md").read_text(encoding="utf-8") == written


def test_the_legacy_path_never_claims_isolation_without_canary_probes(tmp_path: Path) -> None:
    """RB4: an isolation verdict with zero probes executed is a constructible zero."""

    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    result = execute_run(
        RunConfig(dataset=FIXTURE, out=tmp_path, reader_name="stub", run_id="legacy-path"),
        reader=StubReader(), adapter_factory=_FixtureAdapter,
    )
    assert not (result.run_dir / "probes.jsonl").exists(), "the legacy path runs no probes"
    manifest = _manifest(result.run_dir)
    assert manifest["contamination"] != "isolated", "isolation was claimed with no canary evidence"
    assert manifest["contamination"] == "unverifiable"
    # Honest, and still usable on its own; strict validation refuses it for any
    # comparative table, which is exactly what "unverifiable" is for.
    code, output = _strict_validate(result.run_dir)
    assert code == 2
    assert "unverifiable" in output


def test_leakage_invalidated_cases_is_a_real_per_case_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RB4: the summary counts cases, not the truthiness of one run-level flag."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.runner import LmeRunInvalid

    clean = _execute(tmp_path, "count-clean").run_dir
    assert _manifest(clean)["leakage"]["invalidated_cases"] == 0

    run_id = "count-poisoned"
    _install(monkeypatch, _poisoning_provider(run_id))
    with pytest.raises(LmeRunInvalid):
        _execute(tmp_path, run_id)
    poisoned = _manifest(tmp_path / run_id)["leakage"]
    assert poisoned["scanned_cases"] == CASE_COUNT
    assert poisoned["invalidated_cases"] == CASE_COUNT - 1, "every probe-observable contaminated case must be counted"

    class _Crashing(HybridRagDirectProvider):
        def ingest_case(self, events, handle):
            self._case_id = handle.case_id
            return super().ingest_case(events, handle)

        def retrieve(self, question_text, top_k, purpose):
            if not self._case_id.startswith("__probe__"):
                raise RuntimeError("provider retrieval exploded")
            return super().retrieve(question_text, top_k, purpose)

    _install(monkeypatch, _Crashing)
    with pytest.raises(LmeRunInvalid):
        _execute(tmp_path, "count-crashed")
    assert _manifest(tmp_path / "count-crashed")["leakage"]["invalidated_cases"] == 1


def test_probe_inconclusiveness_follows_a_declared_capability_not_a_variant_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that declares it retains nothing gets inconclusive-by-design probes."""

    from lme.providers.null_direct import NullDirectProvider

    class _RenamedNull(NullDirectProvider):
        def variant_id(self) -> str:
            return "some-other-empty-control"

    _install(monkeypatch, _RenamedNull)
    _execute(tmp_path, "declared-capability", provider="no-memory")
    probes = _rows(tmp_path / "declared-capability" / "probes.jsonl")
    assert {row["outcome"] for row in probes} == {"inconclusive-by-design"}
    assert _manifest(tmp_path / "declared-capability")["provider_variant"] == "some-other-empty-control"


def test_feedback6_preflight_and_execution_model_refuse_before_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.providers import registry
    from lme.providers.base import ProviderDescriptor, ProviderSpec
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run
    from protocol.custody import CustodyUnsupported, HeldDirectory

    registered = [registry.provider_spec(name) for name in registry.registered_provider_names()]
    assert {spec.descriptor.execution_model for spec in registered} == {
        "in-process-no-post-return-background"
    }
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("factory must not run")

    original = registered[0]
    declarations = ("background-capable", "unknown")
    for declaration in declarations:
        spec = ProviderSpec(
            factory=factory,
            descriptor=ProviderDescriptor(provider_id="fixture", execution_model=declaration),
            namespace_kind=original.namespace_kind,
            derive_namespace=original.derive_namespace,
            runtime_binding=original.runtime_binding,
        )
        monkeypatch.setattr(registry, "provider_spec", lambda _name, value=spec: value)
        config = RunConfig(dataset=FIXTURE, out=tmp_path / declaration, run_id="run", provider="fixture")
        with pytest.raises(ValueError):
            execute_run(config, reader=StubReader())

    real_proof = HeldDirectory.prove_supported

    def unsupported(_held):
        raise CustodyUnsupported("unsupported")

    foreground = ProviderSpec(
        factory=factory,
        descriptor=ProviderDescriptor(
            provider_id="fixture", execution_model="in-process-no-post-return-background"
        ),
        namespace_kind=original.namespace_kind,
        derive_namespace=original.derive_namespace,
        runtime_binding=original.runtime_binding,
    )
    monkeypatch.setattr(registry, "provider_spec", lambda _name: foreground)
    monkeypatch.setattr(HeldDirectory, "prove_supported", unsupported)
    try:
        with pytest.raises(CustodyUnsupported):
            execute_run(
                RunConfig(dataset=FIXTURE, out=tmp_path / "unsupported", run_id="run", provider="fixture"),
                reader=StubReader(),
            )
    finally:
        monkeypatch.setattr(HeldDirectory, "prove_supported", real_proof)
    assert factory_calls == 0


def test_feedback6_constructor_and_setup_attempts_are_non_authorizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    class SetupFailure:
        cleanups = 0

        def setup(self, _profile, _context):
            raise RuntimeError("setup-secret")

        def cleanup(self):
            self.cleanups += 1

        def variant_id(self):
            raise AssertionError("variant must not be invented after failed setup")

    for mode in ("constructor", "setup"):
        provider = SetupFailure()
        hooks: list[str] = []

        class HostileConstructionError(RuntimeError):
            def __str__(self):
                hooks.append("str")
                raise RuntimeError("constructor-render-hook")

            def __repr__(self):
                hooks.append("repr")
                raise RuntimeError("constructor-render-hook")

            def add_note(self, _note):
                hooks.append("add_note")
                raise RuntimeError("constructor-note-hook")

        hostile = HostileConstructionError()

        def factory():
            if mode == "constructor":
                raise hostile
            return provider

        if mode == "setup":
            provider.setup = lambda *_args: (_ for _ in ()).throw(hostile)

        _feedback6_install(monkeypatch, factory)
        with pytest.raises(LmeRunInvalid) as rejected:
            execute_run(
                _feedback6_config(tmp_path / mode, f"feedback6-{mode}"),
                reader=StubReader(),
            )
        environment = json.loads((rejected.value.run_dir / "environment.json").read_text(encoding="utf-8"))
        attempts = environment["lme"]["lifecycle_attempts"]
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt["factory_returned"] is (mode == "setup")
        assert attempt["setup_completed"] is False
        assert attempt["provider_variant"] is None
        assert attempt["failure_code"] == f"provider_{mode}_failed"
        assert environment["lme"]["lifecycle_expected_instances"] == []
        assert list((rejected.value.run_dir / "traces").glob("*.jsonl")) == []
        assert list((rejected.value.run_dir / "evidence").rglob("provider-cleanup-observation.json")) == []
        assert provider.cleanups == (1 if mode == "setup" else 0)
        assert hooks == []
        fd_targets = []
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                fd_targets.append(os.readlink(descriptor))
            except OSError:
                continue
        assert not any(
            str(rejected.value.run_dir) in target
            and str(attempt["internal_session_id"]) in target
            for target in fd_targets
        )


def test_feedback6_direct_run_id_refuses_unsafe_components_before_factory_or_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.reader import StubReader
    from lme.runner import execute_run

    called = 0

    def factory():
        nonlocal called
        called += 1
        raise AssertionError("unsafe run id reached factory")

    _feedback6_install(monkeypatch, factory)
    violations: list[str] = []

    def snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
        rows: list[tuple[str, str, bytes]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                rows.append((relative, "symlink", os.readlink(path).encode()))
            elif path.is_dir():
                rows.append((relative, "directory", b""))
            else:
                rows.append((relative, "file", path.read_bytes()))
        return tuple(rows)

    for index, raw in enumerate(("", ".", "..", "../escape", "a/b", "a\\b", "/absolute")):
        root = tmp_path / f"case-{index}"
        outside = root / "outside"
        outside.mkdir(parents=True)
        (outside / "sentinel").write_bytes(b"outside")
        before = snapshot(root)
        try:
            execute_run(_feedback6_config(root / "out", raw), reader=StubReader())
        except ValueError:
            pass
        except BaseException:
            violations.append(raw)
        else:
            violations.append(raw)
        if snapshot(root) != before:
            violations.append(f"disk:{raw}")
    assert called == 0
    assert violations == []


def test_feedback6_raw_question_ids_are_logical_only_and_internal_ids_injective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))[:2]
    raw_ids = ("../collision", "collision")
    for row, raw_id in zip(rows, raw_ids, strict=True):
        row["question_id"] = raw_id
    dataset = tmp_path / "unsafe-ids.json"
    dataset.write_text(json.dumps(rows), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside")
    contexts = []

    class Recording(HybridRagDirectProvider):
        def setup(self, profile, context):
            contexts.append(context)
            return super().setup(profile, context)

    _feedback6_install(monkeypatch, Recording)
    with mock.patch.dict(os.environ, _fixture_env()):
        result = execute_run(
            RunConfig(
                dataset=dataset, dataset_sha256=hashlib.sha256(dataset.read_bytes()).hexdigest(),
                dataset_revision="fixture", pilot=2, out=tmp_path / "out", run_id="safe-run",
                provider="hybrid-rag-control",
            ),
            reader=StubReader(),
        )
    environment = json.loads((result.run_dir / "environment.json").read_text(encoding="utf-8"))["lme"]
    scored = [row for row in environment["lifecycle_attempts"] if row["logical_question_id"] is not None]
    assert [row["logical_question_id"] for row in scored] == list(raw_ids)
    internal = [row["internal_session_id"] for row in scored]
    assert len(internal) == len(set(internal)) == 2
    assert all(value not in raw_ids and "/" not in value and "\\" not in value for value in internal)
    assert {path.stem for path in (result.run_dir / "traces").iterdir()} == {
        row["internal_session_id"] for row in environment["lifecycle_attempts"] if row["factory_returned"]
    }
    serialized = "\n".join(
        path.read_text(encoding="utf-8") for path in result.run_dir.rglob("*.json*") if path.is_file()
    )
    assert f"/proc/{os.getpid()}/fd/" not in serialized
    assert not (result.run_dir / "collision.jsonl").exists()
    assert outside_sentinel.read_bytes() == b"outside"
    scored_contexts = contexts[1:]
    assert len(scored_contexts) == 2
    assert all(str(context.work_root).startswith(f"/proc/{os.getpid()}/fd/") for context in scored_contexts)
    assert all(str(context.evidence_root).startswith(f"/proc/{os.getpid()}/fd/") for context in scored_contexts)
    for context in scored_contexts:
        assert not context.work_ref.is_absolute() and ".." not in context.work_ref.parts
        assert not context.evidence_ref.is_absolute() and ".." not in context.evidence_ref.parts


def test_feedback6_session_work_and_evidence_fds_exist_before_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    run_dir = tmp_path / "out" / "feedback6-prefactory-fds"
    observed: dict[str, bool] = {}
    replacements: list[Path] = []

    def fd_holds(path: Path) -> bool:
        identity = os.lstat(path)
        for candidate in Path("/proc/self/fd").iterdir():
            try:
                status = os.stat(candidate)
            except OSError:
                continue
            if (status.st_dev, status.st_ino) == (identity.st_dev, identity.st_ino):
                return True
        return False

    class ConstructorStop(RuntimeError):
        pass

    def factory():
        for label in ("sessions", "work", "evidence"):
            parent = run_dir / label
            children = list(parent.iterdir())
            assert len(children) == 1
            original = children[0]
            observed[label] = fd_holds(original)
            displaced = tmp_path / f"displaced-{label}"
            original.rename(displaced)
            original.mkdir()
            sentinel = original / "replacement-sentinel"
            sentinel.write_bytes(label.encode())
            replacements.append(sentinel)
        raise ConstructorStop()

    _feedback6_install(monkeypatch, factory)
    with pytest.raises(LmeRunInvalid):
        execute_run(
            _feedback6_config(tmp_path / "out", "feedback6-prefactory-fds"),
            reader=StubReader(),
        )
    assert observed == {"sessions": True, "work": True, "evidence": True}
    assert [path.read_bytes() for path in replacements] == [b"sessions", b"work", b"evidence"]


def test_feedback6_hostile_runner_ledger_and_terminalization_never_mask_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lme.runner as runner
    from lme.reader import StubReader

    hooks: list[str] = []

    class Hostile(BaseException):
        @property
        def fact(self):
            hooks.append("fact")
            raise RuntimeError("fact-hook-ran")

        def __str__(self):
            hooks.append("str")
            raise RuntimeError("rendered-secret")

        def __repr__(self):
            hooks.append("repr")
            raise RuntimeError("rendered-secret")

        def add_note(self, _note):
            hooks.append("add_note")
            raise RuntimeError("mutated-primary")

    primary = Hostile()
    ledger_failure = Hostile()
    terminal_failure = Hostile()

    class Interrupting:
        def setup(self, _profile, _context):
            raise primary

        def cleanup(self):
            return None

        def variant_id(self):
            return "unreachable"

    _feedback6_install(monkeypatch, Interrupting)
    real_write_json = runner._write_json
    environment_writes = 0

    def write_json(path, payload):
        nonlocal environment_writes
        if path.name == "environment.json":
            environment_writes += 1
            if environment_writes > 1:
                raise ledger_failure
        return real_write_json(path, payload)

    monkeypatch.setattr(runner, "_write_json", write_json)
    monkeypatch.setattr(runner, "finalize_manifest", lambda *_args, **_kwargs: (_ for _ in ()).throw(terminal_failure))
    caught: BaseException | None = None
    try:
        runner.execute_run(
            _feedback6_config(tmp_path, "feedback6-hostile-runner"),
            reader=StubReader(),
        )
    except BaseException as exc:
        caught = exc
    assert caught is primary
    assert hooks == []
    artifacts = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (tmp_path / "feedback6-hostile-runner").rglob("*") if path.is_file()
    )
    assert "rendered-secret" not in artifacts


def test_feedback7_runner_retirement_control_flow_keeps_exact_first_control_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.reader import StubReader
    from lme.runner import execute_run
    from protocol.custody import HeldDirectory

    class RetirementControl(BaseException):
        pass

    control = RetirementControl()
    real_retire = HeldDirectory.retire

    def interrupt_work(held, **kwargs):
        if held.logical_ref.parts and held.logical_ref.parts[0] == "work":
            raise control
        return real_retire(held, **kwargs)

    _feedback6_install(monkeypatch, HybridRagDirectProvider)
    monkeypatch.setattr(HeldDirectory, "retire", interrupt_work)
    caught: BaseException | None = None
    try:
        execute_run(_feedback6_config(tmp_path, "feedback7-retirement"), reader=StubReader())
    except BaseException as exc:
        caught = exc

    assert caught is control


def test_feedback8_runner_retirement_keeps_first_control_and_closes_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.reader import StubReader
    from lme.runner import execute_run
    from protocol.custody import HeldDirectory

    class ProviderControl(BaseException):
        pass

    class RetirementControl(BaseException):
        pass

    primary = ProviderControl()
    secondary = RetirementControl()
    baseline = set(os.listdir("/proc/self/fd"))

    class Interrupting:
        def setup(self, _profile, _context) -> None:
            raise primary

        def cleanup(self) -> None:
            return None

        def variant_id(self) -> str:
            return "unreachable"

    real_retire = HeldDirectory.retire

    def interrupt_retirement(held, **kwargs):
        if held.logical_ref.parts and held.logical_ref.parts[0] == "work":
            raise secondary
        return real_retire(held, **kwargs)

    _feedback6_install(monkeypatch, Interrupting)
    monkeypatch.setattr(HeldDirectory, "retire", interrupt_retirement)
    caught: BaseException | None = None
    try:
        execute_run(_feedback6_config(tmp_path, "feedback8-retirement"), reader=StubReader())
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert set(os.listdir("/proc/self/fd")) == baseline
