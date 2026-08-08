"""Runner contract: immutable run dirs, visible failures, invalid-run semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from membench.adapters.base import (
    AdapterEnvironmentError,
    Capability,
    Hit,
    OpResult,
    Profile,
)
from membench.generate import generate_corpus
from membench.ids import sentinels_in
from membench.reporting import build_comparison_report
from membench.runner import (
    FLOOR_NEAR_ZERO,
    FLOOR_NOT_APPLICABLE,
    FLOOR_OK,
    FLOOR_VIOLATION,
    RunSpec,
    evaluate_retrieval_floor,
    execute_run,
    retrieval_floor_from_run_dir,
)

T00 = "t00_mini_smoke"


class FakeAdapter:
    """Word-overlap searcher over the neutral corpus; no product involved."""

    name = "fake"
    supports_group_reuse = False

    def __init__(
        self,
        *,
        fail_on_substring: str | None = None,
        env_fail: bool = False,
        hit_budget: int | None = None,
    ) -> None:
        self.fail_on_substring = fail_on_substring
        self.env_fail = env_fail
        #: Total number of queries allowed to return hits. ``0`` is the broken
        #: harness; ``1`` is a genuinely dreadful but real contender.
        self.hit_budget = hit_budget
        self.searches = 0
        self.docs: list[tuple[str, str]] = []
        self.cleaned_up = False

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INGEST_API, Capability.SEARCH})

    def setup(self, workdir: Path, profile: Profile) -> None:
        self.workdir = Path(workdir)

    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        results = []
        sources = json.loads(
            "[" + ",".join((Path(corpus_dir) / "sources.jsonl").read_text().splitlines()) + "]"
        )
        for index, source in enumerate(sources):
            text = (Path(corpus_dir) / source["path"]).read_text(encoding="utf-8")
            self.docs.append((source["source_id"], text))
            results.append(
                OpResult(seq=index, op="ingest", source_id=source["source_id"], ok=True,
                         latency_ms=0.1)
            )
        return results

    def search(self, query: str, limit: int) -> list[Hit]:
        if self.env_fail:
            raise AdapterEnvironmentError("simulated environment fault")
        if self.fail_on_substring and self.fail_on_substring in query:
            raise RuntimeError("simulated per-query fault")
        self.searches += 1
        if self.hit_budget is not None and self.searches > self.hit_budget:
            return []
        terms = {t.lower().strip("?.,") for t in query.split() if len(t) > 3}
        scored = []
        for source_id, text in self.docs:
            lowered = text.lower()
            overlap = sum(1 for t in terms if t in lowered)
            if overlap:
                scored.append((overlap, source_id, text))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            Hit(
                rank=rank,
                provider_path=source_id,
                title=None,
                excerpt=text[:120],
                sentinels=tuple(sentinels_in(text)),
                raw={},
                text=text,
            )
            for rank, (_, source_id, text) in enumerate(scored[:limit], start=1)
        ]

    def export_state(self):  # pragma: no cover - unused by runner v0.1
        raise AdapterEnvironmentError("no state export in fake")

    def cleanup(self) -> None:
        self.cleaned_up = True

    def version_info(self) -> dict[str, str]:
        return {"provider": self.name, "version": "0"}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


def _spec(
    corpus: Path,
    tmp_path: Path,
    adapter,
    run_id: str | None = None,
    reference_environment=None,
) -> RunSpec:
    return RunSpec(
        corpus_dir=corpus,
        adapter=adapter,
        profile=Profile(name="fake-profile"),
        runs_root=tmp_path / "runs",
        top_k=10,
        run_id=run_id,
        reference_environment=reference_environment,
    )


def test_happy_run_writes_complete_artifacts(corpus: Path, tmp_path: Path) -> None:
    adapter = FakeAdapter()
    result = execute_run(_spec(corpus, tmp_path, adapter))
    assert not result.invalid
    for name in (
        "manifest.json",
        "environment.json",
        "corpus-manifest.json",
        "ingest.jsonl",
        "retrieval.jsonl",
        "answers.jsonl",
        "deterministic-scores.json",
        "failures.jsonl",
        "report.md",
    ):
        assert (result.run_dir / name).exists(), name
    assert adapter.cleaned_up
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    assert scores["dimensions"]["_run"]["failures"] == 0
    assert result.dimensions["factual_qa"]["pass"] >= 1
    retrieval_rows = (result.run_dir / "retrieval.jsonl").read_text().splitlines()
    assert retrieval_rows and '"text"' not in retrieval_rows[0]


def test_run_dir_is_never_overwritten(corpus: Path, tmp_path: Path) -> None:
    execute_run(_spec(corpus, tmp_path, FakeAdapter(), run_id="fixed-id"))
    with pytest.raises(FileExistsError):
        execute_run(_spec(corpus, tmp_path, FakeAdapter(), run_id="fixed-id"))


def test_per_query_failure_stays_in_denominator(corpus: Path, tmp_path: Path) -> None:
    adapter = FakeAdapter(fail_on_substring="deadline")
    result = execute_run(_spec(corpus, tmp_path, adapter))
    assert not result.invalid
    failures = (result.run_dir / "failures.jsonl").read_text().splitlines()
    assert failures, "expected recorded per-query failures"
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    statuses = {row["status"] for row in scores["per_query"]}
    assert "failed" in statuses
    assert scores["dimensions"]["_run"]["failures"] >= 1


def test_environment_fault_marks_run_invalid(corpus: Path, tmp_path: Path) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter(env_fail=True)))
    assert result.invalid and "environment" in (result.invalid_reason or "")
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["invalid"] is True
    assert (result.run_dir / "report.md").read_text().count("invalid: True") == 1


def test_agent_only_queries_are_visibly_out_of_scope(corpus: Path, tmp_path: Path) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter()))
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    assert any(row["status"] == "out_of_scope_mode" for row in scores["per_query"])


# ---------------------------------------------------------------------------
# Retrieval floor: zero hits everywhere is a fault, not a score.
# ---------------------------------------------------------------------------


def test_retrieval_floor_thresholds() -> None:
    # Too few queries for "the whole suite retrieved nothing" to mean anything.
    narrow = evaluate_retrieval_floor(5, 0, 0)
    assert narrow.status == FLOOR_NOT_APPLICABLE and not narrow.invalid
    # Exactly zero across a suite: no measurement happened.
    broken = evaluate_retrieval_floor(236, 0, 0)
    assert broken.status == FLOOR_VIOLATION and broken.invalid
    assert "0 hits on all 236" in broken.detail
    # One hit in 236 is a signal, and a dreadful one. It is SCORED.
    dreadful = evaluate_retrieval_floor(236, 1, 1)
    assert dreadful.status == FLOOR_NEAR_ZERO and not dreadful.invalid
    assert "SCORED" in dreadful.detail
    healthy = evaluate_retrieval_floor(236, 140, 452)
    assert healthy.status == FLOOR_OK and not healthy.invalid


def test_zero_hits_everywhere_invalidates_instead_of_publishing_zeros(
    corpus: Path, tmp_path: Path
) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter(hit_budget=0)))

    assert result.invalid
    assert "retrieval floor" in (result.invalid_reason or "")
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["retrieval_floor"]["status"] == FLOOR_VIOLATION
    assert manifest["retrieval_floor"]["queries_with_hits"] == 0
    report = (result.run_dir / "report.md").read_text()
    assert "WITHHELD" in report and "retrieval floor" in report
    # The point of the guard: no sheet of plausible-looking counts.
    assert "| factual_qa |" not in report


def test_floor_invalidation_is_never_a_contender_loss(corpus: Path, tmp_path: Path) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter(hit_budget=0)))

    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["invalid"] is True
    assert manifest["run_failures"] == 0
    assert result.dimensions == {}, "an invalid run publishes no dimensions"
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    # The tallies survive on disk as EVIDENCE for the invalidation, labelled
    # as such — they are not a result, and the caller never receives them.
    assert scores["invalid"] is True
    assert scores["dimensions"]["_run"]["failures"] == 0


def test_a_terrible_but_real_contender_is_still_scored(corpus: Path, tmp_path: Path) -> None:
    """One hit in the whole suite is a measurement. Bad is not broken."""

    result = execute_run(_spec(corpus, tmp_path, FakeAdapter(hit_budget=1)))

    assert not result.invalid, result.invalid_reason
    assert result.dimensions, "a scored run must report its dimensions"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    floor = manifest["retrieval_floor"]
    assert floor["queries_with_hits"] == 1
    assert floor["status"] in (FLOOR_OK, FLOOR_NEAR_ZERO)
    assert not evaluate_retrieval_floor(
        floor["queries"], floor["queries_with_hits"], floor["total_hits"]
    ).invalid


def test_retrieval_floor_reads_an_archived_run(corpus: Path, tmp_path: Path) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter()))
    floor = retrieval_floor_from_run_dir(result.run_dir)
    assert floor.status == FLOOR_OK
    assert floor.queries_with_hits > 0
    assert retrieval_floor_from_run_dir(tmp_path / "nowhere").status == FLOOR_NOT_APPLICABLE


@pytest.mark.parametrize(
    ("run_id", "expected_status", "expected_hits"),
    [
        # The published Aug-1 result, and the replay that produced 236 rows of
        # zeros on a different interpreter and was scored anyway.
        ("20260801T115138Z-exomem-local-postfix-lexical-v2-30586b", FLOOR_OK, 452),
        ("20260805T082324Z-exomem-local-zerohit-s1-probe-f5dc4f", FLOOR_VIOLATION, 0),
    ],
)
def test_floor_verdicts_on_the_real_runs(
    run_id: str, expected_status: str, expected_hits: int
) -> None:
    run_dir = Path(__file__).resolve().parents[1] / "benchmarks" / "runs" / run_id
    if not (run_dir / "retrieval.jsonl").is_file():
        pytest.skip(f"run artifacts absent (benchmarks/runs is not tracked): {run_dir}")
    floor = retrieval_floor_from_run_dir(run_dir)
    assert floor.queries == 236
    assert floor.total_hits == expected_hits
    assert floor.status == expected_status


# ---------------------------------------------------------------------------
# Environment verification: a blocking mismatch invalidates, and measures
# nothing while doing it.
# ---------------------------------------------------------------------------


def _pinned_environment(**overrides: object) -> dict:
    environment = {
        "generator_version": "membench-gen/0.1.0",
        "python": "3.12.3 (main, Jun 19 2026, 12:46:00) [GCC 13.3.0]",
        "python_version": "3.12.3",
        "python_implementation": "CPython",
        "platform": "Linux-6.6.87.2-x86_64",
        "machine": "x86_64",
        "exomem_version": "0.36.0",
        "repos": {"exomem": {"path": "/repo", "head": "b" * 40, "dirty": False}},
        "env_knobs": {"EXOMEM_DISABLE_EMBEDDINGS": "1"},
        "distributions": {"exomem": "0.36.0", "rank-bm25": "0.2.2"},
        "runtime_closure": ["exomem", "rank-bm25"],
    }
    environment.update(overrides)
    return environment


def test_blocking_reference_mismatch_invalidates_before_anything_is_measured(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _pinned_environment(python_version="3.14.6")
    monkeypatch.setattr("membench.runner.capture_environment", lambda **_: dict(observed))
    adapter = FakeAdapter()

    result = execute_run(
        _spec(corpus, tmp_path, adapter, reference_environment=_pinned_environment())
    )

    assert result.invalid
    assert "python_version" in (result.invalid_reason or "")
    assert adapter.searches == 0, "a run that cannot be compared must not measure"
    assert not (result.run_dir / "deterministic-scores.json").exists()
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    verification = manifest["environment_verification"]
    assert verification["status"] == "blocking_mismatch"
    assert [entry["field"] for entry in verification["blocking"]] == ["python_version"]
    # Not a contender loss, in every place a reader could mistake it for one.
    assert manifest["run_failures"] == 0
    assert result.dimensions == {}
    report = (result.run_dir / "report.md").read_text()
    assert "3.12.3" in report and "3.14.6" in report
    assert "not a contender result" in report
    # Visible in failures.jsonl (nothing is hidden) but not counted as one.
    failures = [
        json.loads(line)
        for line in (result.run_dir / "failures.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [entry["phase"] for entry in failures] == ["environment-verification"]


def test_matching_reference_environment_runs_normally(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _pinned_environment()
    monkeypatch.setattr("membench.runner.capture_environment", lambda **_: dict(environment))

    result = execute_run(
        _spec(corpus, tmp_path, FakeAdapter(), reference_environment=dict(environment))
    )

    assert not result.invalid, result.invalid_reason
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["environment_verification"]["status"] == "match"


def test_a_tooling_only_difference_does_not_invalidate_a_real_run(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Too strict is a failure mode: an unrelated bump must not kill a run."""

    observed = _pinned_environment(
        distributions={"exomem": "0.36.0", "rank-bm25": "0.2.2", "ruff": "0.20.0"},
        platform="Linux-6.6.99.9-x86_64",
    )
    monkeypatch.setattr("membench.runner.capture_environment", lambda **_: dict(observed))

    result = execute_run(
        _spec(corpus, tmp_path, FakeAdapter(), reference_environment=_pinned_environment())
    )

    assert not result.invalid, result.invalid_reason
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    verification = manifest["environment_verification"]
    assert verification["status"] == "reported_differences"
    assert verification["blocking"] == []
    assert {entry["field"] for entry in verification["reported"]} == {
        "distributions.ruff",
        "platform",
    }


def test_a_run_without_a_reference_is_recorded_but_unverified(
    corpus: Path, tmp_path: Path
) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter()))
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["environment_verification"]["status"] == "unverified"
    environment = json.loads((result.run_dir / "environment.json").read_text())
    # The capture itself is complete even when nothing verified it.
    assert isinstance(environment["distributions"], dict) and environment["distributions"]
    assert environment["runtime_closure"]
    assert environment["python_version"]


def test_report_surfaces_environment_and_floor_without_opening_json(
    corpus: Path, tmp_path: Path
) -> None:
    result = execute_run(_spec(corpus, tmp_path, FakeAdapter()))
    report = (result.run_dir / "report.md").read_text()
    environment = json.loads((result.run_dir / "environment.json").read_text())
    assert "## Environment (blocking vs reported)" in report
    assert "## Retrieval floor" in report
    assert str(environment["python_version"]) in report
    assert f"distributions recorded: {len(environment['distributions'])}" in report
    assert "unverified" in report


def test_cross_run_report_marks_incomparable_environments(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "membench.runner.capture_environment", lambda **_: _pinned_environment()
    )
    first = execute_run(_spec(corpus, tmp_path, FakeAdapter(), run_id="run-3-12"))
    monkeypatch.setattr(
        "membench.runner.capture_environment",
        lambda **_: _pinned_environment(python_version="3.14.6"),
    )
    second = execute_run(_spec(corpus, tmp_path, FakeAdapter(), run_id="run-3-14"))

    out = build_comparison_report([first.run_dir, second.run_dir], tmp_path / "cmp.md")
    text = out.read_text()

    assert "## Environment (comparability" in text
    # Assert on the ROWS, not on the section's boilerplate: a banner that is
    # always printed proves nothing about the run it is supposed to describe.
    rows = [line for line in text.splitlines() if line.startswith("| ") and "run-3-14" in line]
    assert any("NOT COMPARABLE" in line for line in rows), rows
    assert any(
        "python_version" in line and "3.12.3" in line and "3.14.6" in line for line in rows
    ), rows
    first_rows = [line for line in text.splitlines() if line.startswith("| ") and "run-3-12" in line]
    assert any("reference" in line for line in first_rows), first_rows
    assert "## Retrieval floor" in text
