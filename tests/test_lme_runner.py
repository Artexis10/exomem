from __future__ import annotations

import json
from pathlib import Path

import pytest

from lme.dataset import LmeQuestion, render_session
from lme import cli as cli_module
from lme.fetch import file_sha256
from lme.judge_io import ingest_judge_labels, load_labels, rerender_report
from lme.reader import MeteredApprovalRequired, StubReader
from lme.runner import FullRunApprovalRequired, RunConfig, execute_run
from membench.adapters.base import OpResult


FIXTURE = Path("benchmarks/lme/fixtures/mini.json")


class FixtureAdapter:
    def run_question(self, question: LmeQuestion, workdir: Path, *, dataset_identity, case_ordinal: int, limit: int = 10) -> list[str]:
        del limit, dataset_identity, case_ordinal
        workdir.mkdir(parents=True, exist_ok=True)
        self.last_ingest_results = tuple(
            OpResult(
                seq=index,
                op="capture_source",
                source_id=session.session_id,
                ok=True,
                latency_ms=float(index + 1),
            )
            for index, session in enumerate(question.sessions)
        )
        return [render_session(session) for session in question.sessions]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_runner_writes_official_hypotheses_bounds_environment_and_ability_report(
    tmp_path: Path,
) -> None:
    result = execute_run(
        RunConfig(
            dataset=FIXTURE,
            out=tmp_path,
            reader_name="stub",
            run_id="fixture-run",
            metered_approval="recorded-token",
        ),
        reader=StubReader(),
        adapter_factory=FixtureAdapter,
    )
    hypotheses = _rows(result.run_dir / "hypotheses.jsonl")
    assert len(hypotheses) == 6
    assert all(set(row) == {"question_id", "hypothesis"} for row in hypotheses)
    abstention = next(row for row in hypotheses if row["question_id"].endswith("_abs"))
    assert abstention["hypothesis"] != "I don't know."
    assert (result.run_dir / "bounds" / "gold-evidence-ceiling.jsonl").is_file()
    assert (result.run_dir / "bounds" / "null-abstain-floor.jsonl").is_file()
    assert (result.run_dir / "environment.json").is_file()
    assert (result.run_dir / "OFFICIAL_JUDGE_COMMAND.txt").is_file()
    assert "UNVERIFIED" in (result.run_dir / "OFFICIAL_JUDGE_COMMAND.txt").read_text(
        encoding="utf-8"
    )
    manifest = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["metered_approval"] == "recorded-token"
    report = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "awaiting official judge" in report
    assert "Aggregate" not in report and "aggregate" not in report
    for ability in (
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "multi-session",
        "temporal-reasoning",
        "knowledge-update",
    ):
        assert ability in report
    assert "abstention" in report
    assert "| single-session-assistant | 0 |" in report
    assert "UNVERIFIED" in report

    with pytest.raises(FileExistsError, match="immutable"):
        execute_run(
            RunConfig(dataset=FIXTURE, out=tmp_path, reader_name="stub", run_id="fixture-run"),
            reader=StubReader(),
            adapter_factory=FixtureAdapter,
        )


def test_runner_never_passes_gold_abstention_identity_to_the_reader(tmp_path: Path) -> None:
    class RecordingReader:
        name = "recording"

        def __init__(self) -> None:
            self.seen_abs = False

        def answer(self, question, retrieved_text):
            if question.is_abstention:
                self.seen_abs = True
            return "reader-produced"

    reader = RecordingReader()
    result = execute_run(
        RunConfig(dataset=FIXTURE, out=tmp_path, run_id="no-gold-leak"),
        reader=reader,
        adapter_factory=FixtureAdapter,
    )
    assert reader.seen_abs
    abs_row = next(
        row
        for row in _rows(result.run_dir / "hypotheses.jsonl")
        if row["question_id"].endswith("_abs")
    )
    assert abs_row["hypothesis"] == "reader-produced"


def test_pilot_is_stratified_and_records_measured_evidence(tmp_path: Path) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    copies = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for row in copies:
        if row["question_id"].endswith("_abs"):
            row["question_id"] = row["question_id"][:-4] + "-copy_abs"
        else:
            row["question_id"] += "-copy"
    parent = tmp_path / "parent.json"
    parent.write_text(json.dumps(rows + copies), encoding="utf-8")
    result = execute_run(
        RunConfig(
            dataset=parent,
            dataset_sha256=file_sha256(parent),
            out=tmp_path,
            run_id="pilot",
            pilot=9,
        ),
        reader=StubReader(),
        adapter_factory=FixtureAdapter,
    )
    manifest = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["pilot"]["size"] == 9
    assert len(manifest["pilot"]["question_ids"]) == 9
    selected = _rows(result.run_dir / "hypotheses.jsonl")
    assert len(selected) == 9
    selected_dataset = json.loads((result.run_dir / "dataset.json").read_text(encoding="utf-8"))
    first_round = selected_dataset[:6]
    abilities = {
        "abstention" if row["question_id"].endswith("_abs") else row["question_type"]
        for row in first_round
    }
    assert len(abilities) == 6
    outcomes = _rows(result.run_dir / "question-outcomes.jsonl")
    assert all(outcome["ingest_sessions"] for outcome in outcomes)
    assert all("reader_call_count" in outcome for outcome in outcomes)
    assert all("reader_input_tokens" in outcome for outcome in outcomes)
    assert all("reader_output_tokens" in outcome for outcome in outcomes)
    assert all("reader_cost_usd" in outcome for outcome in outcomes)
    evidence = json.loads((result.run_dir / "pilot-evidence.json").read_text(encoding="utf-8"))
    assert evidence["pilot"]["size"] == 9
    assert evidence["measured_ingest_wall_time_seconds"] > 0
    assert evidence["ingest_wall_time_extrapolation_seconds"] > 0
    assert "api_cost_extrapolation" in evidence


def test_bound_presence_is_derived_from_artifacts_minus_failures(tmp_path: Path) -> None:
    target = "mini-single-user"

    class CeilingFailureReader:
        name = "ceiling-failure"

        def answer(self, question, retrieved_text):
            if question.question_id == target and retrieved_text and retrieved_text[0] != "main":
                raise RuntimeError("ceiling failed")
            return "answer"

    class MainOnlyAdapter:
        def run_question(self, question, workdir, *, dataset_identity, case_ordinal, limit=10):
            del question, dataset_identity, case_ordinal, limit
            workdir.mkdir(parents=True, exist_ok=True)
            self.last_ingest_results = ()
            return ["main"]

    result = execute_run(
        RunConfig(dataset=FIXTURE, out=tmp_path, run_id="bound-failure"),
        reader=CeilingFailureReader(),
        adapter_factory=MainOnlyAdapter,
    )
    report = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "blocked: missing gold-evidence ceiling" in report
    rerender_report(result.run_dir)
    rerendered = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "blocked: missing gold-evidence ceiling" in rerendered


def test_generated_pilot_evidence_unlocks_an_approved_non_pilot_run(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.json"
    parent.write_bytes(FIXTURE.read_bytes())
    checksum = file_sha256(parent)
    pilot = execute_run(
        RunConfig(
            dataset=parent,
            dataset_sha256=checksum,
            out=tmp_path,
            run_id="pilot-source",
            pilot=6,
        ),
        reader=StubReader(),
        adapter_factory=FixtureAdapter,
    )
    full = execute_run(
        RunConfig(
            dataset=parent,
            dataset_sha256=checksum,
            out=tmp_path,
            run_id="approved-full",
            pilot_evidence=pilot.run_dir / "pilot-evidence.json",
            full_run_approval="founder-approved-after-pilot",
        ),
        reader=StubReader(),
        adapter_factory=FixtureAdapter,
    )
    manifest = json.loads((full.run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["pilot"] is None
    assert manifest["pilot_evidence"]["generated_by"] == "benchmarks.lme.runner"


def test_official_labels_can_be_ingested_and_rerender_the_report(tmp_path: Path) -> None:
    result = execute_run(
        RunConfig(dataset=FIXTURE, out=tmp_path, reader_name="stub", run_id="labels-run"),
        reader=StubReader(),
        adapter_factory=FixtureAdapter,
    )
    labels = tmp_path / "labels.jsonl"
    questions = [row["question_id"] for row in _rows(result.run_dir / "hypotheses.jsonl")]
    labels.write_text(
        "\n".join(
            json.dumps({"question_id": question_id, "autoeval_label": {"correct": True}})
            for question_id in questions
        )
        + "\n",
        encoding="utf-8",
    )
    assert set(load_labels(labels).values()) == {True}
    ingest_judge_labels(result.run_dir, labels, lane="main")
    ingest_judge_labels(result.run_dir, labels, lane="ceiling")
    ingest_judge_labels(result.run_dir, labels, lane="floor")
    report = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "6/6" not in report
    assert "1/1" in report
    assert "awaiting official judge" not in report


def test_cli_run_propagates_metered_approval_into_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = {}

    def fake_execute(config):
        captured["config"] = config
        return type("Result", (), {"run_dir": tmp_path / "run", "failure_count": 0})()

    monkeypatch.setattr(cli_module, "execute_run", fake_execute)
    assert (
        cli_module.main(
            [
                "run",
                "--dataset",
                str(FIXTURE),
                "--reader",
                "stub",
                "--out",
                str(tmp_path),
                "--metered-approval",
                "founder-approved-pilot",
                "--pilot",
                "6",
            ]
        )
        == 0
    )
    assert captured["config"].metered_approval == "founder-approved-pilot"
    assert captured["config"].pilot == 6
    assert str(tmp_path / "run") in capsys.readouterr().out


def test_refused_metered_run_leaves_no_directory(tmp_path: Path) -> None:
    with pytest.raises(MeteredApprovalRequired, match="Pilot And Spend Gates"):
        execute_run(
            RunConfig(dataset=FIXTURE, out=tmp_path, reader_name="openai", run_id="refused")
        )
    assert not (tmp_path / "refused").exists()


def test_cli_metered_refusal_runs_through_the_real_execute_path(tmp_path: Path) -> None:
    with pytest.raises(MeteredApprovalRequired, match="Pilot And Spend Gates"):
        cli_module.main(
            [
                "run",
                "--dataset",
                str(FIXTURE),
                "--reader",
                "openai",
                "--out",
                str(tmp_path),
                "--run-id",
                "cli-refused",
            ]
        )
    assert not (tmp_path / "cli-refused").exists()


def test_non_fixture_dataset_requires_recorded_parent_checksum(tmp_path: Path) -> None:
    dataset = tmp_path / "real.json"
    dataset.write_bytes(FIXTURE.read_bytes())
    with pytest.raises(ValueError, match="dataset-sha256"):
        execute_run(RunConfig(dataset=dataset, out=tmp_path, run_id="missing-sha"))
    assert not (tmp_path / "missing-sha").exists()

    with pytest.raises(FullRunApprovalRequired, match="pilot evidence"):
        execute_run(
            RunConfig(
                dataset=dataset,
                dataset_sha256=file_sha256(dataset),
                out=tmp_path,
                run_id="full-gated",
            )
        )
    assert not (tmp_path / "full-gated").exists()
