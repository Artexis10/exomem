"""The null-abstain floor, and the declared-intent seam it needs.

A floor contender produces artifacts byte-indistinguishable from a broken
harness — zero hits, everywhere. This suite has published 236 plausible zeros
before (4b.24), which is exactly why the retrieval floor guard exists, and
exactly why exempting anything from it is dangerous. The exemption is therefore
narrow by construction and held in both directions: only an adapter class may
declare it, and an adapter that declares it and then retrieves is invalid.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from membench.adapters import create_adapter
from membench.adapters.base import Capability, Profile
from membench.generate import generate_corpus
from membench.runner import (
    FLOOR_DECLARATION_BROKEN,
    FLOOR_DECLARED_NULL,
    FLOOR_VIOLATION,
    evaluate_retrieval_floor,
)

T00 = "t00_mini_smoke"


GOVERNANCE = "t16_governance_audiences"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("floor-corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


@pytest.fixture(scope="module")
def governance_corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """t00 carries no governance scenarios, so the vacuity claim needs t16."""

    root = tmp_path_factory.mktemp("floor-gov-corpus") / "s1"
    generate_corpus(1, root, template_ids=[GOVERNANCE])
    return root


def _ready(corpus: Path, workdir: Path):
    adapter = create_adapter("null-abstain")
    adapter.setup(workdir, Profile(name="null-floor"))
    adapter.ingest(corpus, workdir / "native")
    return adapter


def test_it_ingests_everything_and_retrieves_nothing(corpus: Path, tmp_path: Path) -> None:
    """Withholding retrieval is the point; withholding ingest would make it an
    empty-corpus artefact rather than a measurement, and would drop it out of
    the ingested-doc-count parity check it exists to anchor."""

    adapter = _ready(corpus, tmp_path)
    assert adapter.capabilities() == frozenset({Capability.INGEST_API, Capability.SEARCH})
    assert int(adapter.version_info()["ingested_sources"]) > 0
    for prompt in ("anything at all", "Project Quarrypoint deadline", ""):
        assert adapter.search(prompt, 10) == []


def test_the_declaration_lives_on_the_class_not_a_flag() -> None:
    """The exemption must not be purchasable by a real run.

    A CLI flag or environment variable would let any contender switch the
    zero-hit guard off, which is the one thing the guard cannot survive.
    """

    assert create_adapter("null-abstain").retrieves_nothing_by_design is True
    for other in ("oracle-retrieval",):
        assert getattr(create_adapter(other), "retrieves_nothing_by_design", False) is False


def test_an_undeclared_zero_still_invalidates() -> None:
    """The guard is unchanged for everyone else — 4b.24 must stay loud."""

    floor = evaluate_retrieval_floor(236, 0, 0)
    assert floor.status == FLOOR_VIOLATION
    assert floor.invalid


def test_a_declared_zero_is_a_measurement_not_a_fault() -> None:
    floor = evaluate_retrieval_floor(236, 0, 0, declares_null_retrieval=True)
    assert floor.status == FLOOR_DECLARED_NULL
    assert not floor.invalid
    assert "by design" in floor.detail


def test_a_declaration_that_retrieves_is_invalid() -> None:
    """Held in both directions. A declaration nobody checks is just a switch."""

    floor = evaluate_retrieval_floor(236, 5, 12, declares_null_retrieval=True)
    assert floor.status == FLOOR_DECLARATION_BROKEN
    assert floor.invalid
    assert "declared null retrieval, returned 12 hit(s)" in floor.detail


def test_the_floor_run_is_valid_and_scores(corpus: Path, tmp_path: Path) -> None:
    """End to end: the run completes, is not invalid, and produces tallies."""

    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_membench_runner import _spec

    spec = _spec(corpus, tmp_path, create_adapter("null-abstain"), None)
    spec.judge_backend = None
    result = __import__("membench.runner", fromlist=["execute_run"]).execute_run(spec)
    assert not result.invalid
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["retrieval_floor"]["status"] == FLOOR_DECLARED_NULL
    assert result.dimensions["factual_qa"]["pass"] == 0


def test_retrieving_nothing_is_the_best_governance_score(
    governance_corpus: Path, tmp_path: Path
) -> None:
    """The floor proves the vacuity the findings doc argued for.

    Under default-open governance the no-leak gate asks whether withheld
    content was returned, and a contender returning nothing satisfies it
    everywhere. That the *floor* posts the best governance sheet in the suite
    is the mechanical demonstration that these rows measure nothing, and the
    reason default-open governance is excluded from product comparison.
    """

    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_membench_runner import _spec

    spec = _spec(governance_corpus, tmp_path, create_adapter("null-abstain"), None)
    spec.judge_backend = None
    result = __import__("membench.runner", fromlist=["execute_run"]).execute_run(spec)
    governance = result.dimensions.get("governance", {})
    assert governance.get("fail", 0) == 0
    assert governance.get("pass", 0) > 0
