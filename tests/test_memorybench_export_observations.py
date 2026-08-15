"""4.6a: the export publishes what the guest already observed, or says it didn't.

The honesty coupling is the whole point. A closed `missing_fields` vocabulary
that names a value the schema cannot carry lets a consumer read "sometimes
absent" where the truth is "never available". Every observation added here is
bound to its own missing-field label so the two can never disagree.
"""

from __future__ import annotations

import pytest

_SHA = "a" * 64


def _dataset():
    from protocol.models import DatasetIdentity

    return DatasetIdentity(
        id="longmemeval", variant="s", source="local", revision="pin",
        sha256=_SHA, case_count=25,
    )


def _harness():
    """The pinned identity is frozen in the models; mirror it rather than invent one."""

    from protocol import models

    return {
        "repository": models.MEMORYBENCH_REPOSITORY,
        "commit": models.MEMORYBENCH_COMMIT,
        "tree": models.MEMORYBENCH_TREE,
        "bun_lock_sha256": models.MEMORYBENCH_BUN_LOCK_SHA256,
    }


def _phases(status: str = "completed"):
    return {
        "ingest": {"status": status, "failure_code": None},
        "indexing": {"status": status, "failure_code": None},
        "search": {"status": status, "failure_code": None},
    }


def _case(**overrides):
    """A self-consistent case: both observations published, nothing declared missing.

    Tests vary one observation at a time, so the other stays present and never
    becomes an accidental second cause of a validation error.
    """

    base = {
        "case_ordinal": 1,
        "case_id_hmac_sha256": _SHA,
        "question": {"text": "which lantern?", "type": "knowledge-update", "date": None},
        "container_tag_hmac_sha256": _SHA,
        "checkpoint": None,
        "canonical_result": None,
        "private_gold": None,
        "phases": _phases(),
        "hits": [{"content": "a hit", "score": 0.0}],
        "failure_codes": [],
        "missing_fields": [],
        "search": {
            "transmitted_query": "which lantern?",
            "options": {"limit": 10},
            "normalized_hit_ids": ["Knowledge Base/Notes/a.md"],
        },
        "ingest": {"transmitted_payload_sha256": [_SHA]},
    }
    base.update(overrides)
    return base


SEARCH_LABELS = ["search.normalized_hit_ids", "search.options.limit", "search.transmitted_query"]


def test_a_search_observation_can_be_carried_at_all() -> None:
    """The gap that blocked 4.6: there was nowhere to put these."""

    from protocol.models import MemoryBenchExportCase

    case = MemoryBenchExportCase.model_validate(_case(search={
        "transmitted_query": "which lantern?",
        "options": {"limit": 10},
        "normalized_hit_ids": ["Knowledge Base/Notes/a.md"],
    }))
    assert case.search is not None
    assert case.search.options.limit == 10
    assert case.search.transmitted_query == "which lantern?"
    assert case.search.normalized_hit_ids == ["Knowledge Base/Notes/a.md"]


def test_an_absent_search_observation_must_declare_every_one_of_its_labels() -> None:
    from protocol.models import MemoryBenchExportCase

    with pytest.raises(ValueError, match="missing_fields"):
        MemoryBenchExportCase.model_validate(_case(search=None, missing_fields=[]))

    # Declaring only some of them is the dishonest middle ground.
    with pytest.raises(ValueError, match="missing_fields"):
        MemoryBenchExportCase.model_validate(
            _case(search=None, missing_fields=["search.transmitted_query"])
        )

    case = MemoryBenchExportCase.model_validate(
        _case(search=None, missing_fields=sorted(SEARCH_LABELS))
    )
    assert case.search is None


def test_a_present_search_observation_may_not_also_be_declared_missing() -> None:
    """Presence and its absence-label are mutually exclusive, both ways."""

    from protocol.models import MemoryBenchExportCase

    with pytest.raises(ValueError, match="missing_fields"):
        MemoryBenchExportCase.model_validate(_case(
            search={
                "transmitted_query": "q", "options": {"limit": 3},
                "normalized_hit_ids": ["p"],
            },
            missing_fields=sorted(SEARCH_LABELS),
        ))


def test_ingest_payload_digests_are_carried_or_declared() -> None:
    from protocol.models import MemoryBenchExportCase

    case = MemoryBenchExportCase.model_validate(_case())
    assert case.ingest is not None
    assert case.ingest.transmitted_payload_sha256 == [_SHA]

    # Absent, but its label not declared.
    with pytest.raises(ValueError, match="missing_fields"):
        MemoryBenchExportCase.model_validate(_case(ingest=None))

    declared = MemoryBenchExportCase.model_validate(
        _case(ingest=None, missing_fields=["ingest.transmitted_payloads"])
    )
    assert declared.ingest is None


def test_the_search_limit_must_be_a_real_positive_integer() -> None:
    from protocol.models import MemoryBenchExportCase

    for bad in (0, -1):
        with pytest.raises(ValueError):
            MemoryBenchExportCase.model_validate(_case(search={
                "transmitted_query": "q", "options": {"limit": bad},
                "normalized_hit_ids": [],
            }))


def test_hit_ids_may_not_outnumber_the_limit_that_was_sent() -> None:
    """An over-limit response is a contract breach the guest already refuses."""

    from protocol.models import MemoryBenchExportCase

    with pytest.raises(ValueError, match="limit"):
        MemoryBenchExportCase.model_validate(_case(search={
            "transmitted_query": "q", "options": {"limit": 1},
            "normalized_hit_ids": ["a", "b"],
        }))


def test_run_level_readiness_and_normalization_are_carried() -> None:
    from protocol.models import MemoryBenchExport

    export = MemoryBenchExport.model_validate({
        "protocol_version": "1.0.0", "schema_version": 1,
        # The fixture case carries no source references, so "partial" is the
        # only status its evidence supports.
        "artifact_type": "memorybench-export.v1", "status": "partial",
        "run_id": "mb-20260815T000000Z", "provider": "exomem",
        "provider_variant": "exomem-source-only", "benchmark": "longmemeval",
        "harness": _harness(),
        "dataset": _dataset().model_dump(),
        "executed_stages": ["ingest", "indexing", "search"],
        "excluded_stages": ["answer", "evaluate", "report"],
        "privacy": {
            "classification": "provider_safe_reader_input",
            "contains_ground_truth": False,
            "source_results_contain_ground_truth": True,
        },
        "latency": {"publishable": False, "reason": "host_unvalidated"},
        "failure_codes": [],
        "session_normalization": "memorybench.longmemeval_to_corpus/v1",
        "readiness": [{
            "lane": "semantic", "requested": True, "verified": True,
            "method": "doctor-check", "evidence": "hybrid doctor checks pass",
            "fallback_detected": False,
        }],
        "cases": [_case(
            search={"transmitted_query": "q", "options": {"limit": 10}, "normalized_hit_ids": ["p"]},
            ingest={"transmitted_payload_sha256": [_SHA]},
        )],
    })
    assert export.session_normalization == "memorybench.longmemeval_to_corpus/v1"
    assert export.readiness is not None
    assert export.readiness[0].method == "doctor-check"
