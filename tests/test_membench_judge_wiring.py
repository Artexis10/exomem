"""The judge phase, wired into the runner — and unable to override a gate.

Four contracts are pinned here, in the order they matter:

1. The judge is DEFAULT OFF. A run with no backend configured completes
   normally and leaves UNSUPPORTED rows UNSUPPORTED.
2. A judged verdict resolves ONLY an UNSUPPORTED gate. A stub judge that
   contradicts every deterministic verdict moves nothing.
3. Judged verdicts are reported SEPARATELY from deterministic counts, with
   provenance, so a published figure can be audited and the judged
   contribution subtracted without rerunning.
4. A judge failure leaves rows UNSUPPORTED with the cause named. It never
   invalidates the run and never counts as a contender loss.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from membench.generate import generate_corpus
from membench.judge.backends import (
    DEFAULT_BACKEND_NAME,
    PhaseOutcome,
    build_judge_prompt,
    default_backend,
)
from membench.judge.blinding import normalize_for_judge
from membench.judge.handshake import (
    BATCH_NAME,
    HandshakeResponse,
    load_requests,
    responses_dir,
    write_requests,
)
from membench.reporting import JUDGED_SCORES_NAME, build_comparison_report
from membench.runner import execute_run
from membench.schema import ExpectedRecord, QueryRecord, load_jsonl
from membench.scoring import GateStatus, ScoreItem
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.judged import (
    JUDGE_RESOLVABLE_GATES,
    JUDGED_DIMENSION,
    JudgeCandidate,
    candidate_for,
    expected_summary_for,
    resolve,
    unresolved,
)
from test_membench_runner import T00, FakeAdapter, _spec  # noqa: E402  (rootdir on sys.path)

_PROBE_ITEMS = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "judge-agreement"
    / "judge-vs-gates"
    / "direction-discrimination-items.json"
)
_PROBE_CORPUS_IDENTITY = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "judge-agreement"
    / "probe-corpus-identity.json"
)
_RELEASE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "corpus"
    / "releases"
    / "v0.1-seed1.manifest.json"
)


class StubJudge:
    """A judge backend that answers from a script — never a real model call.

    ``verdicts`` maps query id → ``semantic_match``; ``default_match`` covers
    anything unscripted. ``raw`` lets a test inject malformed output verbatim.
    """

    name = "stub-judge"

    def __init__(
        self,
        *,
        verdicts: dict[str, bool] | None = None,
        default_match: bool = True,
        raw: str | None = None,
        model_id: str = "stub-model-1",
    ) -> None:
        self.verdicts = verdicts or {}
        self.default_match = default_match
        self.raw = raw
        self.model_id = model_id
        self.seen: list[str] = []

    def run_phase(self, run_dir, kind, items, *, samples=1, seed="membench"):
        batch = write_requests(run_dir, kind, items, samples=samples, seed=seed)
        directory = responses_dir(run_dir, kind)
        directory.mkdir(parents=True, exist_ok=True)
        lines = []
        for request in load_requests(run_dir, kind):
            self.seen.append(request.request_id)
            if self.raw is not None:
                payload = self.raw
            else:
                match = self.verdicts.get(request.request_id, self.default_match)
                payload = json.dumps(
                    {
                        "semantic_match": match,
                        "explanation_quality": 4,
                        "reason": "stubbed verdict",
                    }
                )
            lines.append(
                json.dumps(
                    HandshakeResponse(
                        request_id=request.request_id,
                        sample_index=request.sample_index,
                        model_id=self.model_id,
                        response=payload,
                    ).model_dump(),
                    sort_keys=True,
                )
            )
        (directory / BATCH_NAME).write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )
        return PhaseOutcome(
            kind=kind,
            backend=self.name,
            status="executed",
            note=f"stub judge answered {len(lines)} request(s)",
            requests_path=batch,
        )


class ExplodingJudge:
    """A backend that fails the way a real one does: mid-phase, unhelpfully."""

    name = "exploding-judge"

    def run_phase(self, run_dir, kind, items, *, samples=1, seed="membench"):
        raise TimeoutError("judge backend timed out after 120s")


#: The t00 smoke corpus, searched by the fake adapter, puts both a required
#: value and the value the oracle proves it superseded into four answers.
#: gate_state reports UNSUPPORTED on exactly those — the shape the judge was
#: measured on, at a size a test can assert exactly rather than approximately.
_EXPECTED_CANDIDATES = 4


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("judgewire-corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


def _run(corpus: Path, root: Path, *, run_id: str | None = None, judge=None):
    spec = _spec(corpus, root, FakeAdapter(), run_id)
    spec.judge_backend = judge
    return execute_run(spec)


def _gates(run_dir: Path) -> dict[tuple[str, str], str]:
    scores = json.loads((run_dir / "deterministic-scores.json").read_text())
    return {
        (row["query_id"], gate["gate"]): gate["status"]
        for row in scores["per_query"]
        for gate in row.get("gates", [])
    }


def _unsupported_gates(run_dir: Path) -> list[tuple[str, str]]:
    return sorted(key for key, status in _gates(run_dir).items() if status == "unsupported")


def _judge_resolvable_unsupported(run_dir: Path) -> list[tuple[str, str]]:
    """The candidate pool: UNSUPPORTED rows *on gates the judge may resolve*.

    UNSUPPORTED is a legitimate status on any gate — ``citations`` reports it
    wherever the oracle has no claim basis to check precision against — so
    "every UNSUPPORTED row is a judge candidate" was only ever true by
    accident of which gates happened to emit it.
    """

    return sorted(key for key in _unsupported_gates(run_dir) if key[1] in JUDGE_RESOLVABLE_GATES)


# --------------------------------------------------------------------------
# 1. Default off
# --------------------------------------------------------------------------


def test_default_backend_is_none_and_the_phase_does_not_run() -> None:
    assert DEFAULT_BACKEND_NAME == "none"
    assert default_backend().name == "none"


def test_run_without_a_judge_completes_and_leaves_the_rows_unsupported(
    corpus: Path, tmp_path: Path
) -> None:
    result = _run(corpus, tmp_path)
    assert not result.invalid
    assert not (result.run_dir / JUDGED_SCORES_NAME).exists()
    assert result.judged == {}
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["judge"]["backend"] == "none"
    assert manifest["judge"]["status"] == "not_run"
    assert manifest["judge"]["candidates"] == _EXPECTED_CANDIDATES
    # Never guessed: the rows a judge could have spoken to stay UNSUPPORTED.
    unsupported = _judge_resolvable_unsupported(result.run_dir)
    assert len(unsupported) == _EXPECTED_CANDIDATES
    assert {gate for _, gate in unsupported} <= JUDGE_RESOLVABLE_GATES
    # And the judge's scope is a real restriction, not a tautology: this corpus
    # also carries UNSUPPORTED `citations` rows, which must never be offered to
    # a judge that can only rule on which value an answer asserts.
    out_of_scope = [key for key in _unsupported_gates(result.run_dir) if key not in unsupported]
    assert out_of_scope, "expected UNSUPPORTED rows outside the judge's scope"
    assert manifest["judge"]["candidates"] == len(unsupported)
    assert "Judged lane" in (result.run_dir / "report.md").read_text()


def test_judge_presence_cannot_change_the_deterministic_record_at_all(
    corpus: Path, tmp_path: Path
) -> None:
    """The deterministic file must not depend on whether a judge was configured."""

    plain = _run(corpus, tmp_path / "a", run_id="fixed")
    judged = _run(corpus, tmp_path / "b", run_id="fixed", judge=StubJudge())
    assert (plain.run_dir / "deterministic-scores.json").read_bytes() == (
        judged.run_dir / "deterministic-scores.json"
    ).read_bytes()
    assert (judged.run_dir / JUDGED_SCORES_NAME).exists()


# --------------------------------------------------------------------------
# 2. A judged verdict can never move a deterministic one
# --------------------------------------------------------------------------


def test_a_contradicting_judge_moves_no_deterministic_verdict(
    corpus: Path, tmp_path: Path
) -> None:
    """The override proof: a judge that disagrees with everything changes nothing."""

    baseline = _run(corpus, tmp_path / "base")
    before = _gates(baseline.run_dir)
    before_dimensions = json.loads(
        (baseline.run_dir / "deterministic-scores.json").read_text()
    )["dimensions"]

    # Contradict every row in both directions at once: half the queries get
    # "match", half get "no match", regardless of what the gates decided.
    query_ids = sorted({query_id for query_id, _ in before})
    contradiction = {
        query_id: (index % 2 == 0) for index, query_id in enumerate(query_ids)
    }
    result = _run(corpus, tmp_path / "j", judge=StubJudge(verdicts=contradiction))
    after = _gates(result.run_dir)
    after_dimensions = json.loads(
        (result.run_dir / "deterministic-scores.json").read_text()
    )["dimensions"]
    assert after == before, "a judged verdict moved a deterministic gate"
    assert after_dimensions == before_dimensions
    # And no deterministic gate row ever carries a judge marker.
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    for row in scores["per_query"]:
        for gate in row.get("gates", []):
            assert "provenance" not in gate and gate.get("source") in (None, "deterministic")
    # The judge really did speak — and only about UNSUPPORTED rows, so the
    # test is not passing merely because nothing happened.
    judged = json.loads((result.run_dir / JUDGED_SCORES_NAME).read_text())["per_query"]
    assert len(judged) == _EXPECTED_CANDIDATES
    assert {row["status"] for row in judged} == {"pass", "fail"}
    for row in judged:
        assert before[(row["query_id"], row["gate"])] == "unsupported"


def test_resolving_a_decided_gate_is_refused() -> None:
    """Belt to the type system's braces: asking is an error, not a silent skip."""

    decided = ScoreItem("QRY-1", "current_state", "temporal", GateStatus.PASS)
    candidate = JudgeCandidate(
        query_id="QRY-1",
        question="q",
        expected_summary="e",
        candidate_answer="a",
        gates=(decided,),
    )
    with pytest.raises(ValueError, match="deterministic gates are final"):
        resolve([candidate], {}, backend="stub")
    with pytest.raises(ValueError, match="deterministic gates are final"):
        unresolved([candidate], cause="x")


def test_candidates_are_only_minted_for_unsupported_resolvable_gates() -> None:
    query = QueryRecord.model_validate(
        {
            "query_id": "QRY-1",
            "family": "temporal",
            "template_id": "t01",
            "query_kind": "current_state",
            "prompt_text": "What is the deadline?",
            "modes": ["qa"],
            "ask": {"knowledge_week": 4},
        }
    )
    expected = ExpectedRecord.model_validate(
        {
            "query_id": "QRY-1",
            "answer": {"kind": "date", "values": ["2025-03-28"]},
        }
    )
    answer = AnswerRecord(query_id="QRY-1", answer_text="It is now 2025-03-28.")

    unsupported = ScoreItem("QRY-1", "current_state", "temporal", GateStatus.UNSUPPORTED)
    assert candidate_for(query, expected, answer, [unsupported]) is not None

    for status in (GateStatus.PASS, GateStatus.FAIL, GateStatus.NOT_APPLICABLE):
        item = ScoreItem("QRY-1", "current_state", "temporal", status)
        assert candidate_for(query, expected, answer, [item]) is None

    # An UNSUPPORTED gate the judge was never measured on stays out of scope.
    out_of_scope = ScoreItem(
        "QRY-1", "non_activation", "behavior", GateStatus.UNSUPPORTED
    )
    assert candidate_for(query, expected, answer, [out_of_scope]) is None
    citations = ScoreItem("QRY-1", "citations", "provenance", GateStatus.UNSUPPORTED)
    assert candidate_for(query, expected, answer, [citations]) is None

    # Nothing to grade: an abstention is already decided by gate_abstention.
    abstained = AnswerRecord(query_id="QRY-1", answer_text="", abstained=True)
    assert candidate_for(query, expected, abstained, [unsupported]) is None


# --------------------------------------------------------------------------
# 3. Separate reporting, with provenance
# --------------------------------------------------------------------------


def test_judged_verdicts_are_reported_separately_with_provenance(
    corpus: Path, tmp_path: Path
) -> None:
    result = _run(corpus, tmp_path, judge=StubJudge(model_id="stub-model-7"))
    run_dir = result.run_dir
    payload = json.loads((run_dir / JUDGED_SCORES_NAME).read_text())
    assert len(payload["per_query"]) == _EXPECTED_CANDIDATES

    assert payload["meta"]["backend"] == "stub-judge"
    assert payload["meta"]["dimension"] == JUDGED_DIMENSION
    assert payload["meta"]["prompt_id"].startswith("judge-prompt:")
    assert "UPPER BOUND" in payload["meta"]["caveat"]

    for row in payload["per_query"]:
        assert row["source"] == "judge"
        assert row["gate"] in JUDGE_RESOLVABLE_GATES
        provenance = row["provenance"]
        assert provenance["model_ids"] == ["stub-model-7"]
        assert provenance["backend"] == "stub-judge"
        assert provenance["prompt_id"].startswith("judge-prompt:")
        assert "UPPER BOUND" in provenance["caveat"]

    # Judged tallies live under their own dimension and never in the
    # deterministic ones.
    deterministic = json.loads((run_dir / "deterministic-scores.json").read_text())
    assert JUDGED_DIMENSION not in deterministic["dimensions"]
    assert set(payload["summary"]["by_dimension"]) == {JUDGED_DIMENSION}

    report = (run_dir / "report.md").read_text()
    assert "judged (separate)" in report
    assert "NOT part of the counts above" in report
    assert "UPPER BOUND" in report


def test_comparison_report_keeps_the_judged_lane_out_of_the_dimension_table(
    corpus: Path, tmp_path: Path
) -> None:
    run_dir = _run(corpus, tmp_path, judge=StubJudge()).run_dir
    out = build_comparison_report([run_dir], tmp_path / "comparison.md")
    text = out.read_text()
    assert "## Judged lane (separate — never added to the dimension table)" in text
    assert "Every count in this table came from a deterministic gate." in text
    dimension_block = text.split("## Dimensions")[1].split("## Retrieval")[0]
    assert JUDGED_DIMENSION not in dimension_block


def test_expected_summary_matches_the_probe_that_justifies_the_dimension() -> None:
    """The prompt shipped must be the prompt that scored 19/19.

    ``expected_summary_for`` is pinned to the committed probe artifact. If this
    fails, the wording changed and the 19/19 evidence no longer describes what
    the runner sends — re-run the probe before touching the string.
    """

    identity = json.loads(_PROBE_CORPUS_IDENTITY.read_text(encoding="utf-8"))
    current_identity = hashlib.sha256(_RELEASE_MANIFEST.read_bytes()).hexdigest()
    if identity["release_manifest_sha256"] != current_identity:
        pytest.skip("probe artifact stale vs current corpus (4b.45)")

    items = {row["id"]: row["prompt"] for row in json.loads(_PROBE_ITEMS.read_text())}
    probe_id = "QRY-C30F6519|correct|plain"
    assert probe_id in items, "probe artifact no longer carries the pinned item"

    corpus_dir = Path(__file__).resolve().parents[1] / "benchmarks/corpus/generated/s1"
    queries = {q.query_id: q for q in load_jsonl(QueryRecord, corpus_dir / "queries.jsonl")}
    expected = {
        e.query_id: e for e in load_jsonl(ExpectedRecord, corpus_dir / "expected.jsonl")
    }
    query_id = probe_id.split("|")[0]
    rebuilt = build_judge_prompt(
        normalize_for_judge(queries[query_id].prompt_text),
        expected_summary_for(expected[query_id]),
        "It was previously 2025-03-14, and it is now 2025-03-28.",
    )
    assert rebuilt == items[probe_id]


# --------------------------------------------------------------------------
# 4. A judge failure is not a contender loss
# --------------------------------------------------------------------------


def _judged_rows(run_dir: Path) -> list[dict]:
    path = run_dir / JUDGED_SCORES_NAME
    return json.loads(path.read_text())["per_query"] if path.exists() else []


def _failure_phases(run_dir: Path) -> set[str]:
    return {
        json.loads(line).get("phase")
        for line in (run_dir / "failures.jsonl").read_text().splitlines()
        if line.strip()
    }


@pytest.mark.parametrize(
    "backend_factory",
    [
        pytest.param(lambda: ExplodingJudge(), id="backend-raises"),
        pytest.param(lambda: StubJudge(raw="not json at all"), id="not-json"),
        pytest.param(lambda: StubJudge(raw="I will not grade this."), id="refusal"),
        pytest.param(
            lambda: StubJudge(raw='{"semantic_match": "maybe"}'), id="wrong-schema"
        ),
        pytest.param(lambda: StubJudge(raw="```json\n{"), id="unterminated-fence"),
    ],
)
def test_a_broken_judge_leaves_rows_unsupported_and_the_run_valid(
    corpus: Path, tmp_path: Path, backend_factory
) -> None:
    baseline = _run(corpus, tmp_path / "base")
    result = _run(corpus, tmp_path / "run", judge=backend_factory())

    assert not result.invalid, "a judge fault must never invalidate a run"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["invalid"] is False
    # Judge failures are visible but never enter a deterministic denominator,
    # so the contender's deterministic sheet is identical to the clean run's.
    assert manifest["run_failures"] == 0
    assert (baseline.run_dir / "deterministic-scores.json").read_bytes() == (
        result.run_dir / "deterministic-scores.json"
    ).read_bytes()

    rows = _judged_rows(result.run_dir)
    assert len(rows) == _EXPECTED_CANDIDATES
    for row in rows:
        assert row["status"] == "unsupported"
        assert "no judged verdict:" in row["evidence"]
    # Whatever went wrong is on the record, under a judge phase.
    assert {phase for phase in _failure_phases(result.run_dir) if "judge" in (phase or "")}


def test_a_backend_that_raises_names_the_exception_on_every_row(
    corpus: Path, tmp_path: Path
) -> None:
    result = _run(corpus, tmp_path, judge=ExplodingJudge())
    rows = _judged_rows(result.run_dir)
    assert len(rows) == _EXPECTED_CANDIDATES
    assert all("TimeoutError" in row["evidence"] for row in rows)
    assert "judge" in _failure_phases(result.run_dir)


def test_malformed_judge_json_is_recorded_not_repaired(
    corpus: Path, tmp_path: Path
) -> None:
    result = _run(corpus, tmp_path, judge=StubJudge(raw="I refuse to grade this."))
    rows = _judged_rows(result.run_dir)
    assert len(rows) == _EXPECTED_CANDIDATES
    assert all(row["status"] == "unsupported" for row in rows)
    assert all("malformed judge verdict" in row["evidence"] for row in rows)
    # The raw sample survives verbatim next to the verdict for auditing.
    merged = json.loads((result.run_dir / "judge-scores.json").read_text())
    assert all(
        "error" in sample
        for row in merged["per_query"]
        for sample in row["samples"]
    )
