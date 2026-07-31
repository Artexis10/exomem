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
from membench.runner import RunSpec, execute_run

T00 = "t00_mini_smoke"


class FakeAdapter:
    """Word-overlap searcher over the neutral corpus; no product involved."""

    name = "fake"
    supports_group_reuse = False

    def __init__(self, *, fail_on_substring: str | None = None, env_fail: bool = False) -> None:
        self.fail_on_substring = fail_on_substring
        self.env_fail = env_fail
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


def _spec(corpus: Path, tmp_path: Path, adapter, run_id: str | None = None) -> RunSpec:
    return RunSpec(
        corpus_dir=corpus,
        adapter=adapter,
        profile=Profile(name="fake-profile"),
        runs_root=tmp_path / "runs",
        top_k=10,
        run_id=run_id,
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
