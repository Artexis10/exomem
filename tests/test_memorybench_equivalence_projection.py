"""4.6b: project a MemoryBench export into the differ's right-hand side.

Only `lme/runner.py` wrote `equivalence.json` before this, so the differ had
nothing to compare against. The projection mirrors that emitter's twelve keys
exactly, and leaves a key null when the export could not source it — null never
equals anything, so an unsourced key becomes a difference demanding an
explanation rather than a silent pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_SHA = "c" * 64
JUDGE = {
    "reader": "stub",
    "reader_model": "gpt-4o",
    "judge_model": "gpt-4o",
}


def _export(**overrides):
    from test_protocol_schema_conformance import _memorybench_payloads

    base = _memorybench_payloads()["MemoryBenchExport"]
    references = base["cases"][0]
    case = {
        "case_ordinal": 1,
        "case_id_hmac_sha256": "d" * 64,
        "question": {"text": "which lantern?", "type": "knowledge-update", "date": None},
        "container_tag_hmac_sha256": "e" * 64,
        "checkpoint": references["checkpoint"],
        "canonical_result": references["canonical_result"],
        "private_gold": references["private_gold"],
        "phases": {
            name: {"status": "completed", "failure_code": None}
            for name in ("ingest", "indexing", "search")
        },
        "hits": [{"content": "first session", "score": 0.0}, {"content": "second", "score": 0.0}],
        "failure_codes": [],
        "missing_fields": [],
        "search": {
            "transmitted_query": "which lantern?",
            "options": {"limit": 10},
            "normalized_hit_ids": ["Notes/a.md", "Notes/b.md"],
        },
        "ingest": {"transmitted_payload_sha256": [_SHA]},
    }
    export = {
        **base,
        "run_id": "mb-20260815T000000Z",
        "provider_variant": "exomem-source-only",
        "dataset": {
            "id": "longmemeval", "variant": "s", "source": "local",
            "revision": "pin", "sha256": "f" * 64, "case_count": 25,
        },
        "session_normalization": "memorybench.longmemeval_to_corpus/v1",
        "readiness": [{
            "lane": "semantic", "requested": True, "verified": True,
            "method": "doctor-check", "evidence": "hybrid doctor checks pass",
            "fallback_detected": False,
        }],
        "cases": [case],
    }
    export.update(overrides)
    return export


@pytest.mark.parametrize("field,value", [
    ("protocol_version", "9.9.9"), ("schema_version", 999),
    ("artifact_type", "memorybench-export.v99"), ("status", "partial"),
])
def test_projection_refuses_unrecognized_versions_and_partial_exports(field, value):
    with pytest.raises(ValueError):
        _project(_export(**{field: value}))


def test_v2_projection_uses_product_hashes_and_current_case_evidence():
    from lme.exomem_capture import CAPTURE_CONTRACT, NAMESPACE_PATTERN

    export = _export(session_normalization=CAPTURE_CONTRACT)
    case = export["cases"][0]
    case["ingest"]["product_payload_sha256"] = ["a" * 64]
    case["namespace_pattern"] = NAMESPACE_PATTERN
    case["readiness"] = export["readiness"]
    projected = _project(export)["cases"][0]
    assert projected["ingestion_payloads"] == ["a" * 64]
    assert projected["namespace"] == NAMESPACE_PATTERN
    assert projected["readiness"][0]["method"] == "doctor-check"


def _golds():
    return {"d" * 64: {"question_id": "q-01", "container_tag": "mb-container-01"}}


_UNSET = object()


def _project(export=None, golds=_UNSET, **kwargs):
    """`golds={}` means "no mapping", which is not the same as "use the default"."""

    from memorybench.equivalence_projection import project_export

    resolved = _golds() if golds is _UNSET else golds
    return project_export(export or _export(), resolved, judge_config=JUDGE, **kwargs)


def test_the_envelope_matches_what_the_differ_loads() -> None:
    payload = _project()

    assert payload["schema"] == "equivalence-input.v1"
    assert payload["run_id"] == "mb-20260815T000000Z"
    assert payload["provider_variant"] == "exomem-source-only"
    assert [case["case_id"] for case in payload["cases"]] == ["q-01"]


def test_every_one_of_the_twelve_keys_is_present_on_each_case() -> None:
    from equivalence.differ import EQUIVALENCE_KEYS

    case = _project()["cases"][0]
    assert set(EQUIVALENCE_KEYS) <= set(case)


def test_the_observed_search_facts_become_query_limit_and_hit_ids() -> None:
    case = _project()["cases"][0]

    assert case["exact_query"] == "which lantern?"
    assert case["top_k"] == 10
    assert case["retrieved_ids"] == ["Notes/a.md", "Notes/b.md"]


def test_retrieved_text_and_packed_context_use_the_readers_own_separator() -> None:
    from lme.reader import CONTEXT_SEPARATOR

    case = _project()["cases"][0]
    assert case["retrieved_text"] == ["first session", "second"]
    assert case["packed_context"] == CONTEXT_SEPARATOR.join(["first session", "second"])


def test_case_set_comes_from_private_gold_not_the_public_pseudonyms() -> None:
    """The public artifact carries HMACs; the comparison needs the real ids."""

    case = _project()["cases"][0]
    assert case["case_set"] == ["q-01"]
    assert "d" * 64 not in json.dumps(case["case_set"])


def test_an_unsourced_key_is_null_rather_than_invented() -> None:
    export = _export()
    export["cases"][0]["search"] = None
    export["cases"][0]["missing_fields"] = sorted([
        "search.transmitted_query", "search.options.limit", "search.normalized_hit_ids",
    ])
    case = _project(export)["cases"][0]

    assert case["exact_query"] is None
    assert case["top_k"] is None
    assert case["retrieved_ids"] is None


def test_absent_run_level_facts_are_null_on_every_case() -> None:
    export = _export(session_normalization=None, readiness=None)
    case = _project(export)["cases"][0]

    assert case["session_normalization"] is None
    assert case["readiness"] is None


def test_readiness_is_narrowed_to_the_fields_the_left_side_compares() -> None:
    """The emitter drops `evidence`, which is prose and would never match."""

    case = _project()["cases"][0]
    assert case["readiness"] == [{
        "lane": "semantic", "requested": True, "verified": True,
        "method": "doctor-check", "fallback_detected": False,
    }]


def test_the_judge_config_is_an_operator_declaration_with_a_per_case_prompt_digest() -> None:
    import hashlib

    case = _project()["cases"][0]
    assert case["answer_judge_prompt_model_config"] == {
        "reader": "stub", "reader_model": "gpt-4o", "judge_model": "gpt-4o",
        "prompt_sha256": hashlib.sha256("which lantern?".encode()).hexdigest(),
    }


def test_a_case_with_no_private_gold_mapping_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="private gold"):
        _project(golds={})


def test_the_projection_round_trips_through_the_differ_against_itself(tmp_path: Path) -> None:
    """Two identical projections must produce no blocking difference."""

    from equivalence.differ import compare_runs

    for name in ("left", "right"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "equivalence.json").write_text(json.dumps(_project()), encoding="utf-8")

    result = compare_runs(tmp_path / "left", tmp_path / "right", mode="blocking", out=tmp_path / "left")
    assert result.blocking is False


def test_both_sides_carry_the_same_keys_so_the_differ_compares_like_with_like() -> None:
    """The real left-side emitter, not a hand-copy of it."""

    from types import SimpleNamespace

    from lme.runner import _equivalence_case
    from protocol.models import DatasetIdentity, LaneReadiness

    identity = DatasetIdentity(
        id="longmemeval", variant="s", source="local", revision="pin",
        sha256="f" * 64, case_count=25,
    )
    left = _equivalence_case(
        question=SimpleNamespace(question_id="q-01", question="which lantern?"),
        case_ids=["q-01"],
        namespace_pattern="exomem/{run}/{session}",
        payload_shas=[_SHA],
        readiness=[LaneReadiness(
            lane="semantic", requested=True, verified=False,
            method="readiness-unverifiable", evidence="probes pending",
        )],
        retrieved_ids=["exomem-0"], retrieved_text=["first session"], top_k=10,
        dataset_identity=identity, reader_name="stub", reader_model="gpt-4o",
    )
    right = _project()["cases"][0]

    assert set(left) == set(right)
    # And the readiness rows are narrowed identically on both sides.
    assert set(left["readiness"][0]) == set(right["readiness"][0])


def test_a_changed_blocking_key_is_caught_by_the_differ(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    (tmp_path / "left").mkdir()
    (tmp_path / "left" / "equivalence.json").write_text(json.dumps(_project()), encoding="utf-8")

    widened = _export()
    widened["cases"][0]["search"]["options"]["limit"] = 30
    (tmp_path / "right").mkdir()
    (tmp_path / "right" / "equivalence.json").write_text(
        json.dumps(_project(widened)), encoding="utf-8"
    )

    result = compare_runs(tmp_path / "left", tmp_path / "right", mode="blocking", out=tmp_path / "left")
    assert result.blocking is True
    assert any(diff.field == "top_k" for diff in result.diffs)
