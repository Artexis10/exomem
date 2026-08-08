"""Native answers: score the product's attribution, not the harness's.

The harness authors the answer, so every dimension scoring a property of the
ANSWER has been scoring `extractive.py` rather than the contender — provenance
(the citations it picks), abstention (it abstains only on zero hits), and
calibration (it cannot generate hedging at all). Only dimensions scoring a
property of RETRIEVAL measure the product.

Measured on seed-1 before this change: exomem abstained 0 times in 240 answers
while 52 queries required abstention, and scored 0/208 provenance while a
perfect retriever scored 198/204 through the same answerer. Neither number was
about the product.

This seam lets an adapter answer in its own words with its own attribution.
Two invariants keep it from becoming the next fairness defect:

- **Unsupported is never zero.** A contender with no native answer surface is
  not scored badly on answer-property dimensions; it reports its mode and those
  rows are excluded from cross-contender comparison.
- **Modes never mix silently.** Comparing a native-answer contender against a
  harness-answered one on provenance is exactly the 4b.29 shape — a
  configuration difference read as a product difference. The report refuses it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from membench.adapters.base import Capability, Hit, NativeAnswer, OpResult, Profile
from membench.generate import generate_corpus
from membench.ids import sentinel
from membench.scoring.answer_contract import AnswerRecord, extract_structure

T00 = "t00_mini_smoke"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("native-corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


# --------------------------------------------------------------------------
# The citation-closure rule
# --------------------------------------------------------------------------


def test_harness_answers_still_harvest_sentinels_from_text() -> None:
    """Unchanged for the extractive path: it cites what it quotes."""

    record = AnswerRecord(
        query_id="QRY-1",
        answer_text=f"The deadline is 2025-03-28. {sentinel('SRC-AAAA1111')}",
        citations=[],
    )
    assert extract_structure(record).citations == ["SRC-AAAA1111"]


def test_native_citations_are_closed_against_text_harvesting() -> None:
    """A product's attribution claim is what it SAYS it used.

    Without this the seam would be pointless: a native answer that quotes a
    document would silently acquire that document as a citation, and provenance
    would go back to measuring text content instead of the product's own claim
    about its basis.
    """

    record = AnswerRecord(
        query_id="QRY-1",
        answer_text=f"Per the revision memo {sentinel('SRC-QUOTED0')}, it is 2025-03-28.",
        citations=["SRC-CLAIMED0"],
        citations_are_native=True,
    )
    assert extract_structure(record).citations == ["SRC-CLAIMED0"]


def test_native_mode_still_derives_hedging_when_unset() -> None:
    """Add-only normalization is preserved; only citation harvesting closes."""

    record = AnswerRecord(
        query_id="QRY-1",
        answer_text="The figure is disputed between two sources.",
        citations=[],
        citations_are_native=True,
    )
    assert extract_structure(record).hedged is True


# --------------------------------------------------------------------------
# The adapter seam
# --------------------------------------------------------------------------


class NativeAdapter:
    """Answers in its own words, cites exactly what it chose to cite."""

    name = "fake-native"
    supports_group_reuse = False

    def __init__(self, *, answer_for=None, **_kw) -> None:
        self._answer_for = answer_for or (
            lambda q: NativeAnswer(
                text="a native answer", citations=("SRC-NATIVE1",), abstained=False
            )
        )

    def capabilities(self):
        return frozenset(
            {Capability.INGEST_API, Capability.SEARCH, Capability.NATIVE_ANSWER}
        )

    def setup(self, workdir, profile) -> None:
        self.workdir = Path(workdir)

    def ingest(self, corpus_dir, native_dir):
        sources = json.loads(
            "[" + ",".join((Path(corpus_dir) / "sources.jsonl").read_text().splitlines()) + "]"
        )
        return [
            OpResult(seq=i, op="ingest", source_id=s["source_id"], ok=True, latency_ms=0.1)
            for i, s in enumerate(sources)
        ]

    def search(self, query: str, limit: int) -> list[Hit]:
        return [
            Hit(
                rank=1,
                provider_path="SRC-NATIVE1",
                title=None,
                excerpt="x",
                sentinels=(sentinel("SRC-NATIVE1"),),
                raw={},
                text="x",
            )
        ]

    def answer(self, query: str, limit: int) -> NativeAnswer:
        return self._answer_for(query)

    def cleanup(self) -> None:
        pass

    def version_info(self) -> dict:
        return {"provider": self.name}


def _scored_answers(run_dir: Path) -> list[dict]:
    """Answers the runner actually scored.

    Queries outside the run's modes get a minimal ``{query_id, status}`` stub
    rather than an answer envelope, so they carry none of the fields under test.
    """

    rows = [
        json.loads(line)
        for line in (run_dir / "answers.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return [row for row in rows if row.get("status") != "out_of_scope_mode"]


def _run(corpus: Path, root: Path, adapter):
    from membench.runner import execute_run
    from test_membench_runner import _spec

    spec = _spec(corpus, root, adapter, None)
    spec.judge_backend = None
    return execute_run(spec)


def test_a_declaring_adapter_supplies_the_scored_answer(corpus: Path, tmp_path: Path) -> None:
    result = _run(corpus, tmp_path, NativeAdapter())
    answers = _scored_answers(result.run_dir)
    assert answers, "no answers written"
    assert all(a["answer_text"] == "a native answer" for a in answers)
    assert all(a["citations_are_native"] for a in answers)
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["answer_mode"] == "native"


def test_a_non_declaring_adapter_still_gets_the_extractive_answerer(
    corpus: Path, tmp_path: Path
) -> None:
    """No existing adapter changes behaviour, and the mode says so."""

    from test_membench_runner import FakeAdapter

    result = _run(corpus, tmp_path, FakeAdapter())
    answers = _scored_answers(result.run_dir)
    assert not any(a["citations_are_native"] for a in answers)
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["answer_mode"] == "harness"


def test_a_native_adapter_can_abstain_on_its_own_judgment(corpus: Path, tmp_path: Path) -> None:
    """The point of the seam.

    The extractive answerer abstains only when retrieval returns nothing, which
    is why exomem abstained 0/240 while retrieving on 236/236. A native adapter
    decides for itself, so abstention becomes a product behaviour.
    """

    adapter = NativeAdapter(
        answer_for=lambda q: NativeAnswer(text="", citations=(), abstained=True)
    )
    result = _run(corpus, tmp_path, adapter)
    answers = _scored_answers(result.run_dir)
    assert answers
    assert all(a["abstained"] for a in answers)
    assert not result.invalid, "abstaining by judgment is a measurement, not a fault"


# --------------------------------------------------------------------------
# Mixed modes must never be compared silently
# --------------------------------------------------------------------------


def test_answer_dimensions_refuse_a_mixed_mode_comparison() -> None:
    """The 4b.29 shape: a configuration difference read as a product difference.

    Retrieval-property dimensions stay comparable across modes, because the
    answerer does not decide them.
    """

    from membench.reporting import ANSWER_MODE_DIMENSIONS, _answer_mode_conflict

    assert "provenance" in ANSWER_MODE_DIMENSIONS
    assert "abstention" in ANSWER_MODE_DIMENSIONS
    assert "factual_qa" not in ANSWER_MODE_DIMENSIONS
    assert _answer_mode_conflict(["native", "harness"]) is True
    assert _answer_mode_conflict(["native", "native"]) is False
    assert _answer_mode_conflict(["harness"]) is False


# --------------------------------------------------------------------------
# Answer mode must be a run-level variable, not an adapter property
# --------------------------------------------------------------------------


def test_exomem_declares_native_answer_only_when_the_run_asks() -> None:
    """Mirrors how GOVERNED_VIEWS is declared only under active wiring.

    Answer mode decides whether provenance, abstention and calibration measure
    the product or the harness, so it must be a variable a run can hold fixed
    and A/B. Declaring the capability unconditionally makes the single
    comparison that would attribute its effect impossible to run — which is
    exactly what happened on the first full-strength run, where environment and
    answer mode both changed and neither could be credited.
    """

    from membench.adapters.exomem_local import ExomemLocalAdapter

    assert Capability.NATIVE_ANSWER not in ExomemLocalAdapter().capabilities()
    assert (
        Capability.NATIVE_ANSWER
        in ExomemLocalAdapter(answer_mode="native").capabilities()
    )


def test_an_unknown_answer_mode_is_refused_loudly() -> None:
    from membench.adapters.exomem_local import ExomemLocalAdapter

    with pytest.raises(ValueError, match="unknown answer_mode"):
        ExomemLocalAdapter(answer_mode="magic")
