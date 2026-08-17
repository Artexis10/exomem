"""Planned-versus-recorded review: `review_memory(mode="plan-progress")`.

The closing beat of the Planning/Records programme. Planning already stores
authored intent with opaque `progress_evidence` descriptors; Records already
stores observed state behind saved views. This module is the read-only consumer
that runs the bound queries and puts intent next to observation.

The non-goals are load-bearing and are asserted here, not just documented: the
review never sets `health`, never mutates a plan or a record, and never computes
a score. Divergence is exact integers, and adjudication stays with the human.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from canonical_snapshot import canonical_digests

RECORDS_ID = "49622075-9ff4-4660-9ab7-414854b5bca2"
PLANNING_ID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
RECORDS_REF = f"exomem://memory/{RECORDS_ID}"
HIDDEN_ID = "81947000-4c22-46e4-9874-23fed028314b"
HIDDEN_REF = f"exomem://memory/{HIDDEN_ID}"
ABSENT_REF = "exomem://memory/5c252e6f-2639-4ee4-819a-fc9099200e1a"
PLANNING_REF = f"exomem://memory/{PLANNING_ID}"

_SCORE_SHAPED_KEY = re.compile(
    r"score|percent|pct|ratio|rank|severity|grade|confidence|weight|priority_index",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Pure-logic units
# --------------------------------------------------------------------------


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "collection_id": PLANNING_ID,
        "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516",
        "title": "Ship the thing",
        "kind": "outcome",
        "status": "active",
        "lifecycle": "active",
        "priority": "high",
        "commitment": "committed",
        "horizon": "quarter",
        "health": "unknown",
        "progress_evidence": [
            {"collection": RECORDS_REF, "role": "progress", "view": "worked"}
        ],
    }
    row.update(overrides)
    return row


def _binding(
    role: str = "progress", matched: int | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Mirror exactly the observed shape `_observe` produces — no aggregate."""
    if reason is not None:
        return {
            "role": role,
            "view": "worked",
            "collection": RECORDS_REF,
            "resolved": False,
            "unresolved_reason": reason,
            "observed": None,
        }
    return {
        "role": role,
        "view": "worked",
        "collection": RECORDS_REF,
        "resolved": True,
        "unresolved_reason": None,
        "observed": {
            "collection_id": RECORDS_ID,
            "snapshot": "0" * 64,
            "matched": matched,
            "returned": matched,
            "truncated": False,
        },
    }


def test_selection_takes_active_committed_items_that_carry_evidence() -> None:
    from exomem import plan_progress

    assert plan_progress.selects_item(_row()) is True


@pytest.mark.parametrize(
    "overrides",
    (
        {"status": "candidate", "commitment": "uncommitted", "horizon": "inbox"},
        {"status": "planned"},
        {"status": "blocked"},
        {"status": "completed"},
        {"commitment": "considering"},
        {"commitment": "uncommitted"},
        {"lifecycle": "archived"},
        {"progress_evidence": []},
        {"progress_evidence": None},
    ),
)
def test_selection_excludes_everything_outside_the_reviewed_slice(
    overrides: dict[str, Any],
) -> None:
    from exomem import plan_progress

    assert plan_progress.selects_item(_row(**overrides)) is False


def test_selection_excludes_an_area_because_it_carries_no_delivery_state() -> None:
    from exomem import plan_progress

    row = _row(kind="area")
    for forbidden in ("status", "priority", "commitment", "horizon"):
        row.pop(forbidden)

    assert plan_progress.selects_item(row) is False


def test_evidence_normalization_keeps_authored_order_and_drops_invalid() -> None:
    from exomem import plan_progress

    row = _row(
        progress_evidence=[
            {"collection": RECORDS_REF, "role": "completion", "view": "shipped"},
            {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
            "not-a-mapping",
            {"collection": RECORDS_REF, "role": "progress"},
            {"collection": RECORDS_REF, "role": "guess", "view": "worked"},
            {"collection": RECORDS_REF, "role": "progress", "view": ""},
            {"collection": 7, "role": "progress", "view": "worked"},
            {"collection": RECORDS_REF, "role": "progress", "view": "worked", "extra": 1},
        ]
    )

    assert plan_progress.evidence_bindings(row) == [
        {"collection": RECORDS_REF, "role": "completion", "view": "shipped"},
        {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
    ]


def test_evidence_normalization_is_bounded() -> None:
    from exomem import plan_progress

    row = _row(
        progress_evidence=[
            {"collection": RECORDS_REF, "role": "progress", "view": f"view-{index}"}
            for index in range(plan_progress.MAX_EVIDENCE + 5)
        ]
    )

    assert len(plan_progress.evidence_bindings(row)) == plan_progress.MAX_EVIDENCE


def test_divergence_reports_exact_non_negative_integers() -> None:
    from exomem import plan_progress

    counts = plan_progress.divergence(
        [
            _binding("progress", matched=3),
            _binding("progress", matched=1),
            _binding("completion", matched=0),
            _binding("completion", reason="collection_unavailable"),
        ]
    )

    assert counts == {
        "evidence_bindings": 4,
        "resolved_bindings": 3,
        "unresolved_bindings": 1,
        "progress_bindings": 2,
        "completion_bindings": 2,
        "progress_observations": 4,
        "completion_observations": 0,
    }
    assert all(type(value) is int and value >= 0 for value in counts.values())


def test_divergence_never_produces_a_score_or_a_float() -> None:
    from exomem import plan_progress

    counts = plan_progress.divergence(
        [_binding("progress", matched=9), _binding("completion", matched=0)]
    )

    assert not any(isinstance(value, float) for value in counts.values())
    assert not any(_SCORE_SHAPED_KEY.search(name) for name in counts)


def test_item_order_is_identity_only_and_carries_no_ranking() -> None:
    from exomem import plan_progress

    items = [
        {"collection_id": "b", "plan_id": "2", "divergence": {"progress_observations": 99}},
        {"collection_id": "a", "plan_id": "2", "divergence": {"progress_observations": 0}},
        {"collection_id": "a", "plan_id": "1", "divergence": {"progress_observations": 50}},
    ]

    ordered = plan_progress.order_items(items)

    assert [(item["collection_id"], item["plan_id"]) for item in ordered] == [
        ("a", "1"),
        ("a", "2"),
        ("b", "2"),
    ]


# --------------------------------------------------------------------------
# Vault fixtures
# --------------------------------------------------------------------------


def _records_manifest(exomem_id: str = RECORDS_ID, title: str = "Delivery sessions") -> str:
    return f"""---
type: collection
exomem_id: {exomem_id}
title: {title}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [occurred_on, label]
  fields:
    occurred_on:
      type: date
      required: true
    label:
      type: string
      required: true
    minutes:
      type: integer
    status:
      type: enum
      enum: [worked, shipped]
views:
  worked:
    query:
      filters: {{status: worked}}
  shipped:
    query:
      filters: {{status: shipped}}
  latest:
    query:
      filters: {{status: worked}}
      aggregate: latest:occurred_on
  labels:
    query:
      filters: {{status: worked}}
      aggregate: distinct:label
  mean:
    query:
      filters: {{status: worked}}
      aggregate: avg:minutes
  grouped:
    query:
      aggregate: group:status
  tally:
    query:
      filters: {{status: worked}}
      aggregate: count
---

Ordinary delivery observations.
"""


_PLANNING_MANIFEST = f"""---
type: collection
exomem_id: {PLANNING_ID}
title: Planning work
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
    progress_evidence:
      type: array
      items: {{type: object}}
---
"""

PLANNING_COLLECTION = "Knowledge Base/Planning/Work/_collection.md"
RECORDS_COLLECTION = "Knowledge Base/Records/Delivery/_collection.md"
HIDDEN_COLLECTION = "Knowledge Base/Records/Hidden/_collection.md"


def _committed(title: str, evidence: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
    return {
        "title": title,
        "kind": "outcome",
        "status": "active",
        "commitment": "committed",
        "horizon": "quarter",
        "priority": "high",
        "progress_evidence": evidence,
        **extra,
    }


def _build_vault(root: Path, *, worked: int = 2) -> None:
    from exomem import planning, records

    (root / "Knowledge Base").mkdir(parents=True, exist_ok=True)
    (root / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    records.create_collection(
        root, RECORDS_COLLECTION, _records_manifest(), why="create records collection"
    )
    for index in range(worked):
        records.append_record(
            root,
            RECORDS_COLLECTION,
            item={
                "occurred_on": f"2026-08-{index + 1:02d}",
                "label": f"session-{index}",
                "minutes": 30 + index,
                "status": "worked",
            },
            why="record observed work",
        )
    planning.create_collection(
        root, PLANNING_COLLECTION, _PLANNING_MANIFEST, why="create planning collection"
    )


def _add_plan(root: Path, item: Mapping[str, Any], plan_id: str | None = None) -> str:
    from exomem import planning

    return planning.add(
        root,
        PLANNING_COLLECTION,
        item=dict(item),
        plan_id=plan_id,
        why="capture intent",
    )["plan_id"]


#: Identity order is Gamma < Beta < Alpha. Titles sort the other way, and the
#: Planning page arrives sorted by title, so identity order, arrival order and
#: insertion order are three different orders. Any test that asserts identity
#: order over this trio is non-vacuous.
_PINNED_IDS = {
    "Gamma": "11111111-1111-4111-8111-111111111111",
    "Beta": "22222222-2222-4222-8222-222222222222",
    "Alpha": "33333333-3333-4333-8333-333333333333",
}


def _seed_pinned_trio(root: Path) -> dict[str, str]:
    """Seed three items where identity, title and insertion order all disagree.

    identity order  : Gamma(1111), Beta(2222), Alpha(3333)
    arrival order   : Alpha, Beta, Gamma   (the query sorts by title)
    insertion order : Beta, Gamma, Alpha   (a third order again)
    observations along identity order: 0, 6, 3 — non-monotonic, so neither an
    ascending nor a descending divergence sort reproduces it.
    """
    worked = {"collection": RECORDS_REF, "role": "progress", "view": "worked"}
    shipped = {"collection": RECORDS_REF, "role": "progress", "view": "shipped"}
    _add_plan(root, _committed("Beta", [worked, dict(worked)]), _PINNED_IDS["Beta"])
    _add_plan(root, _committed("Gamma", [shipped]), _PINNED_IDS["Gamma"])
    _add_plan(root, _committed("Alpha", [worked]), _PINNED_IDS["Alpha"])
    return _PINNED_IDS


def _digest(root: Path) -> dict[str, str]:
    """Hash the vault's canonical bytes, ignoring derived-index residue.

    A graph rebuild running behind the request creates and removes
    `.graph-rebuild-<digest>.sqlite` and its SQLite companions inside the
    vault. Those are not canonical bytes, and since a write stopped joining its
    rebuild (#576) one can simply be in flight while this census runs -- so
    counting them makes "the review changed nothing" fail for a reason that has
    nothing to do with the review.
    """
    return canonical_digests(root)


def _walk(payload: Any, key: Any = None) -> Any:
    """Yield (key, value) for every node, including scalars nested in lists.

    List elements inherit their container's key, so a float buried in a
    `distinct` or `groups` list is still visible to the no-float assertion.
    """
    yield key, payload
    if isinstance(payload, Mapping):
        for name, value in payload.items():
            yield from _walk(value, name)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _walk(value, key)


# --------------------------------------------------------------------------
# Integration over a real two-profile vault
# --------------------------------------------------------------------------


def test_review_presents_intent_next_to_the_observed_counts(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path, worked=3)
    plan_id = _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
                {"collection": RECORDS_REF, "role": "completion", "view": "shipped"},
            ],
        ),
    )

    result = plan_progress.review(tmp_path)

    assert result["mode"] == "plan-progress"
    assert result["derived"] is True
    assert result["read_only"] is True
    assert result["items_matched"] == 1
    assert result["items_reviewed"] == 1
    assert result["truncated"] is False
    assert result["bindings_truncated"] is False
    [item] = result["items"]
    assert item["plan_id"] == plan_id
    assert item["plan_ref"] == f"exomem://plan/{PLANNING_ID}/{plan_id}"
    assert item["intent"]["title"] == "Ship the thing"
    assert item["intent"]["status"] == "active"
    assert item["intent"]["commitment"] == "committed"
    assert [(entry["role"], entry["view"]) for entry in item["evidence"]] == [
        ("progress", "worked"),
        ("completion", "shipped"),
    ]
    assert item["evidence"][0]["observed"]["matched"] == 3
    assert item["evidence"][1]["observed"]["matched"] == 0
    assert item["divergence"]["progress_observations"] == 3
    assert item["divergence"]["completion_observations"] == 0
    assert item["divergence"]["unresolved_bindings"] == 0


def test_review_omits_items_outside_the_reviewed_slice(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    evidence = [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}]
    reviewed = _add_plan(tmp_path, _committed("Reviewed", evidence))
    _add_plan(tmp_path, {"title": "Candidate", "progress_evidence": evidence})
    _add_plan(
        tmp_path,
        {
            "title": "Planned",
            "kind": "outcome",
            "status": "planned",
            "commitment": "considering",
            "horizon": "year",
            "progress_evidence": evidence,
        },
    )
    _add_plan(tmp_path, _committed("No evidence", []))

    result = plan_progress.review(tmp_path)

    assert [item["plan_id"] for item in result["items"]] == [reviewed]
    assert result["items_matched"] == 1


def test_absent_and_withheld_evidence_targets_are_indistinguishable(
    tmp_path: Path,
) -> None:
    from exomem import plan_progress, records
    from exomem.governance.principal import RequestPrincipal, request_scope

    _build_vault(tmp_path)
    records.create_collection(
        tmp_path,
        HIDDEN_COLLECTION,
        _records_manifest(HIDDEN_ID, "Hidden sessions"),
        why="create hidden records collection",
    )
    absent = _add_plan(
        tmp_path,
        _committed(
            "First target",
            [{"collection": ABSENT_REF, "role": "completion", "view": "shipped"}],
        ),
    )
    hidden = _add_plan(
        tmp_path,
        _committed(
            "Second target",
            [{"collection": HIDDEN_REF, "role": "completion", "view": "shipped"}],
        ),
    )
    _write_governance(tmp_path)

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        result = plan_progress.review(tmp_path)

    entries = {
        item["plan_id"]: item["evidence"][0]
        for item in result["items"]
        if item["plan_id"] in {absent, hidden}
    }
    assert len(entries) == 2
    assert entries[absent]["unresolved_reason"] == "collection_unavailable"
    assert entries[hidden]["unresolved_reason"] == "collection_unavailable"
    assert entries[absent]["observed"] is None
    assert entries[hidden]["observed"] is None
    assert result["unavailable"]["collection_unavailable"] == 2
    serialized = str(result)
    assert "Hidden sessions" not in serialized
    assert HIDDEN_COLLECTION not in serialized


def test_unknown_saved_view_is_a_bounded_reason(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Bad view",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "nope"},
                {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
            ],
        ),
    )

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    assert item["evidence"][0]["unresolved_reason"] == "view_unavailable"
    assert item["evidence"][1]["observed"]["matched"] == 2
    assert item["divergence"]["unresolved_bindings"] == 1
    assert result["unavailable"]["view_unavailable"] == 1


def test_planning_collection_named_as_evidence_reports_profile_mismatch(
    tmp_path: Path,
) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Wrong profile",
            [{"collection": PLANNING_REF, "role": "progress", "view": "week"}],
        ),
    )

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    assert item["evidence"][0]["unresolved_reason"] == "profile_mismatch"
    assert result["unavailable"]["profile_mismatch"] == 1


def test_repeated_binding_executes_once_and_reports_identical_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import plan_progress, record_governance

    _build_vault(tmp_path, worked=2)
    evidence = [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}]
    for title in ("First", "Second", "Third"):
        _add_plan(tmp_path, _committed(title, evidence))

    executions: list[Any] = []
    real = record_governance.query_collection

    def counted(*args: Any, **kwargs: Any) -> Any:
        # The Planning page itself runs through the same governed reader with
        # no view; only the bound evidence views are counted here.
        if kwargs.get("view") is not None:
            executions.append(kwargs["view"])
        return real(*args, **kwargs)

    monkeypatch.setattr(record_governance, "query_collection", counted)
    result = plan_progress.review(tmp_path)

    assert executions == ["worked"]
    assert result["bindings_executed"] == 1
    assert {item["evidence"][0]["observed"]["matched"] for item in result["items"]} == {2}


def test_execution_budget_truncates_explicitly(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Budgeted",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
                {"collection": RECORDS_REF, "role": "completion", "view": "shipped"},
            ],
        ),
    )

    result = plan_progress.review(tmp_path, execution_budget=1)

    [item] = result["items"]
    assert result["bindings_truncated"] is True
    assert result["bindings_executed"] == 1
    assert item["evidence"][0]["observed"]["matched"] == 2
    assert item["evidence"][1]["unresolved_reason"] == "budget_exhausted"
    assert result["unavailable"]["budget_exhausted"] == 1


def test_item_limit_truncates_by_identity_not_by_arrival(tmp_path: Path) -> None:
    """Ordering must happen BEFORE the cap, or the cap becomes a covert ranking.

    If the cap were applied to the arrival page and the survivors ordered
    afterwards, the retained set would be chosen by the Planning query's title
    sort. The retained set here distinguishes the two.
    """
    from exomem import plan_progress

    _build_vault(tmp_path, worked=3)
    ids = _seed_pinned_trio(tmp_path)

    result = plan_progress.review(tmp_path, limit=2)

    assert result["items_matched"] == 3
    assert result["items_reviewed"] == 2
    assert result["truncated"] is True
    # Identity order keeps Gamma and Beta. Truncating the arrival page first
    # would have kept Alpha and Beta instead.
    assert [item["plan_id"] for item in result["items"]] == [
        ids["Gamma"],
        ids["Beta"],
    ]
    assert [item["intent"]["title"] for item in result["items"]] == ["Gamma", "Beta"]


def test_review_selector_restricts_to_one_planning_collection(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Scoped",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )

    result = plan_progress.review(tmp_path, collection=PLANNING_COLLECTION)

    assert result["collections_scanned"] == 1
    assert result["items_reviewed"] == 1


def test_review_bounds_are_pinned_and_clamped() -> None:
    """Production never passes a budget, so the defaults are the real bound.

    Pinned by value: a mutation that widens either default or drops the clamp
    would otherwise make "bounded independently of vault size" vacuous.
    """
    from exomem import plan_progress

    assert plan_progress.DEFAULT_ITEM_LIMIT == 25
    assert plan_progress.MAX_ITEM_LIMIT == 100
    assert plan_progress.DEFAULT_EXECUTION_BUDGET == 64
    assert plan_progress.MAX_EXECUTION_BUDGET == 256
    assert plan_progress.MAX_EVIDENCE == 16
    assert plan_progress._bounded(7, 25, 100) == 7
    assert plan_progress._bounded(10_000, 25, 100) == 100
    assert plan_progress._bounded(0, 25, 100) == 25
    assert plan_progress._bounded(-3, 25, 100) == 25
    assert plan_progress._bounded("many", 25, 100) == 25
    assert plan_progress._bounded(None, 25, 100) == 25
    assert plan_progress._bounded(True, 25, 100) == 25


def test_unresolvable_selector_is_counted_not_silently_empty(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)

    result = plan_progress.review(
        tmp_path, collection="Knowledge Base/Planning/Absent/_collection.md"
    )

    assert result["collections_scanned"] == 0
    assert result["collections_unavailable"] == 1
    assert result["items"] == []
    assert "Absent" not in str(result)


def test_records_collection_as_selector_is_counted_not_scanned(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)

    result = plan_progress.review(tmp_path, collection=RECORDS_COLLECTION)

    assert result["collections_scanned"] == 0
    assert result["collections_unavailable"] == 1
    assert result["items"] == []


def test_planning_collection_that_refuses_its_query_is_counted(tmp_path: Path) -> None:
    """A refusing collection must leave a signal, not read as no divergence."""
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )
    broken = tmp_path / "Knowledge Base" / "Planning" / "Broken"
    (broken / "Items").mkdir(parents=True)
    (broken / "_collection.md").write_text(
        """---
type: collection
exomem_id: 7c1f0a44-6d2b-4a51-9f33-0b8e2c4d5a61
title: Broken planning
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
---
""",
        encoding="utf-8",
    )

    result = plan_progress.review(tmp_path)

    assert result["collections_scanned"] == 1
    assert result["collections_unavailable"] == 1
    assert result["items_reviewed"] == 1
    assert "Broken" not in str(result)


def test_unexpected_query_refusal_reports_query_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import plan_progress, record_governance
    from exomem.structured_collections import CollectionError

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )
    real = record_governance.query_collection

    def refusing(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("view") is not None:
            raise CollectionError("RECORD_RESPONSE_TOO_LARGE", "rendered query is too large")
        return real(*args, **kwargs)

    monkeypatch.setattr(record_governance, "query_collection", refusing)
    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    assert item["evidence"][0]["unresolved_reason"] == "query_unavailable"
    assert item["evidence"][0]["observed"] is None
    assert result["unavailable"]["query_unavailable"] == 1
    assert item["divergence"]["progress_observations"] == 0


def test_undeclared_evidence_field_does_not_refuse_the_collection(
    tmp_path: Path,
) -> None:
    """A Planning manifest need not declare the optional evidence field."""
    from exomem import plan_progress, planning

    _build_vault(tmp_path)
    plain = "Knowledge Base/Planning/Plain/_collection.md"
    planning.create_collection(
        tmp_path,
        plain,
        _PLANNING_MANIFEST.replace(
            "    progress_evidence:\n      type: array\n      items: {type: object}\n", ""
        ).replace(PLANNING_ID, "3f5b9c02-71ae-4d18-8c47-6a2e19b0d334"),
        why="create planning collection without evidence",
    )
    planning.add(
        tmp_path,
        plain,
        item={
            "title": "Committed but unbound",
            "kind": "outcome",
            "status": "active",
            "commitment": "committed",
            "horizon": "quarter",
        },
        why="capture intent",
    )

    result = plan_progress.review(tmp_path)

    assert result["collections_scanned"] == 2
    assert result["collections_unavailable"] == 0
    assert result["items"] == []


# --------------------------------------------------------------------------
# Hard non-goals
# --------------------------------------------------------------------------


def test_returned_sequence_is_identity_ordered_not_divergence_ordered(
    tmp_path: Path,
) -> None:
    """The response sequence itself must carry no ranking.

    Three items bind views with very different match counts. If anything
    anywhere re-orders by divergence, this sequence changes.
    """
    from exomem import plan_progress

    _build_vault(tmp_path, worked=3)
    ids = _seed_pinned_trio(tmp_path)

    result = plan_progress.review(tmp_path)

    # Titles descend while identity ascends: a no-op ordering would return the
    # rows in arrival (title) order and fail here.
    assert [item["intent"]["title"] for item in result["items"]] == [
        "Gamma",
        "Beta",
        "Alpha",
    ]
    assert [item["plan_id"] for item in result["items"]] == [
        ids["Gamma"],
        ids["Beta"],
        ids["Alpha"],
    ]
    assert [
        item["divergence"]["progress_observations"] for item in result["items"]
    ] == [0, 6, 3]


def test_review_leaves_the_vault_byte_identical(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path, worked=3)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
                {"collection": RECORDS_REF, "role": "completion", "view": "shipped"},
            ],
        ),
    )

    before = _digest(tmp_path)
    plan_progress.review(tmp_path)
    plan_progress.review(tmp_path)
    after = _digest(tmp_path)

    assert after == before


def test_review_under_a_configured_audience_writes_only_governance_receipts(
    tmp_path: Path,
) -> None:
    """The kernel audits its own reads; the review still writes nothing itself."""
    from exomem import plan_progress
    from exomem.governance.principal import RequestPrincipal, request_scope

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )
    _write_governance(tmp_path)

    before = _digest(tmp_path)
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        plan_progress.review(tmp_path)
    after = _digest(tmp_path)

    assert not [path for path in set(after) & set(before) if after[path] != before[path]]
    assert not set(before) - set(after)
    assert all(
        path.startswith("Knowledge Base/_Governance/events/")
        or path.startswith("Knowledge Base/.governance.sqlite")
        for path in set(after) - set(before)
    )


def test_review_echoes_authored_health_and_proposes_nothing(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    plan_id = _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "completion", "view": "shipped"}],
            health="unknown",
        ),
    )
    item_path = (
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / f"{plan_id}.md"
    )
    before = item_path.read_bytes()

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    assert item["intent"]["health"] == "unknown"
    assert item_path.read_bytes() == before
    health_values = {value for key, value in _walk(result) if key == "health"}
    assert health_values == {"unknown"}


def test_review_response_carries_no_score_shaped_value(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path, worked=3)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
                {"collection": RECORDS_REF, "role": "progress", "view": "mean"},
                {"collection": RECORDS_REF, "role": "completion", "view": "shipped"},
            ],
        ),
    )

    result = plan_progress.review(tmp_path)

    assert not [key for key, _ in _walk(result) if _SCORE_SHAPED_KEY.search(str(key))]
    assert not [value for _, value in _walk(result) if isinstance(value, float)]
    for item in result["items"]:
        assert all(type(value) is int for value in item["divergence"].values())


def test_aggregate_declaring_views_leak_no_statistic(tmp_path: Path) -> None:
    """`avg:` yields a float mean and `group:`/`distinct:` yield record values.

    A bound view may declare any of them. None of it may reach the review.
    """
    from exomem import plan_progress

    _build_vault(tmp_path, worked=3)
    _add_plan(
        tmp_path,
        _committed(
            "Aggregating evidence",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "mean"},
                {"collection": RECORDS_REF, "role": "progress", "view": "grouped"},
                {"collection": RECORDS_REF, "role": "completion", "view": "tally"},
            ],
        ),
    )

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    assert [entry["resolved"] for entry in item["evidence"]] == [True, True, True]
    assert not [value for _, value in _walk(result) if isinstance(value, float)]
    assert "avg" not in str(result)
    assert "groups" not in str(result)
    # The matched count survives every aggregate shape, so nothing is lost.
    assert item["evidence"][0]["observed"]["matched"] == 3
    assert item["divergence"]["progress_observations"] == 6


def test_review_returns_no_record_rows_bodies_or_identities(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path, worked=2)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [
                {"collection": RECORDS_REF, "role": "progress", "view": "worked"},
                # `latest:` returns a whole record row and `distinct:` returns
                # record values; both are reachable from an authored view.
                {"collection": RECORDS_REF, "role": "progress", "view": "latest"},
                {"collection": RECORDS_REF, "role": "completion", "view": "labels"},
            ],
        ),
    )

    result = plan_progress.review(tmp_path)

    serialized = str(result)
    assert "session-0" not in serialized
    assert "session-1" not in serialized
    assert "record_id" not in serialized
    assert "item_version" not in serialized
    assert "latest_by" not in serialized
    assert "distinct" not in serialized
    assert "rows" not in serialized
    assert "body" not in serialized


def test_review_produces_no_triageable_reference(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )

    result = plan_progress.review(tmp_path)

    assert "exomem://review/" not in str(result)
    assert not [key for key, _ in _walk(result) if key in {"ref", "fingerprint"}]
    assert not (tmp_path / "Knowledge Base" / "_Review").exists()


def test_review_module_reaches_no_mutating_entry_point() -> None:
    from exomem import plan_progress

    source = Path(plan_progress.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "append_record",
        "update_record",
        "create_collection",
        "write_text",
        "write_bytes",
        "unlink",
        "mkdir",
        "rmtree",
        "planning.add",
        "planning.update",
        "planning.triage",
    ):
        assert forbidden not in source, f"review module reaches {forbidden}"


def test_plan_progress_is_not_an_attention_category() -> None:
    from exomem import attention, plan_progress

    assert plan_progress.MODE not in attention.ATTENTION_CATEGORIES
    assert plan_progress.MODE not in attention.DEFAULT_ATTENTION_CATEGORIES


# --------------------------------------------------------------------------
# Command wiring
# --------------------------------------------------------------------------


def test_review_memory_routes_the_plan_progress_mode(tmp_path: Path) -> None:
    from exomem import commands, plan_progress

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )

    result = commands.op_review_memory(tmp_path, mode="plan-progress")

    assert result["mode"] == plan_progress.MODE
    assert result["items_reviewed"] == 1


def test_review_memory_plan_progress_honours_path_and_limit(tmp_path: Path) -> None:
    from exomem import commands

    _build_vault(tmp_path)
    evidence = [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}]
    for title in ("First", "Second"):
        _add_plan(tmp_path, _committed(title, evidence))

    result = commands.op_review_memory(
        tmp_path, mode="plan-progress", path=PLANNING_COLLECTION, limit=1
    )

    assert result["collections_scanned"] == 1
    assert result["items_reviewed"] == 1
    assert result["items_matched"] == 2
    assert result["truncated"] is True


def test_plan_progress_round_trips_over_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surface parity: the generated command carries the mode to REST too."""
    from starlette.testclient import TestClient

    from exomem import commands, server

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Ship the thing",
            [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}],
        ),
    )
    for surface in ("mcp", "rest", "cli"):
        assert "review_memory" in {c.name for c in commands.product_commands_for(surface)}

    # The server resolves and validates a vault root through its schema docs;
    # the review leaf needs only the collections, so the repo's own schema
    # scaffold is copied in for the facade alone.
    shutil.copytree(
        Path(__file__).parent / "fixtures" / "Knowledge Base" / "_Schema",
        tmp_path / "Knowledge Base" / "_Schema",
    )

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "plan-progress-key")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_AUD", raising=False)
    client = TestClient(server.build_server(require_auth=False).http_app())

    response = client.post(
        "/api/review_memory",
        json={"mode": "plan-progress"},
        headers={"Authorization": "Bearer plan-progress-key"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True, payload
    data = payload["data"]
    assert data["mode"] == "plan-progress"
    assert data["items_reviewed"] == 1
    assert data["items"][0]["evidence"][0]["observed"]["matched"] == 2


def test_attention_mode_is_unaffected_by_plan_progress(tmp_path: Path) -> None:
    """Divergence never enters the ranked Inbox.

    The queue is seeded with a real `unprocessed_source` finding first: against
    an empty queue every exclusion assertion below would pass over nothing and
    would keep passing even if attention DID anchor on Planning items.
    """
    from exomem import commands

    _build_vault(tmp_path)
    _add_plan(
        tmp_path,
        _committed(
            "Diverging outcome",
            [{"collection": RECORDS_REF, "role": "completion", "view": "shipped"}],
        ),
    )
    source = tmp_path / "Knowledge Base" / "Sources" / "Observed Elsewhere.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntype: source\ntitle: Observed elsewhere\ncaptured: 2025-01-01\n---\n\n"
        "Raw captured material that nothing has compiled yet.\n",
        encoding="utf-8",
    )

    attention = commands.op_review_memory(tmp_path, mode="attention")

    # Non-vacuity guard: the queue really does surface something.
    assert attention["total"] >= 1
    assert attention["summary"] == {"unprocessed_source": 1}
    # ...and what it surfaces is exactly the source, never the diverging plan.
    assert [item["path"] for item in attention["items"]] == [
        "Knowledge Base/Sources/Observed Elsewhere.md"
    ]
    serialized = str(attention)
    assert "exomem://plan/" not in serialized
    assert "plan-progress" not in serialized
    assert "divergence" not in serialized
    assert "Diverging outcome" not in serialized


def test_invalid_review_mode_names_plan_progress(tmp_path: Path) -> None:
    from exomem import commands

    (tmp_path / "Knowledge Base").mkdir()

    with pytest.raises(ValueError, match="plan-progress"):
        commands.op_review_memory(tmp_path, mode="not-a-mode")


def _write_governance(vault: Path) -> None:
    """Release the reviewed layers to an external audience and block one target."""
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "open.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FD1\n"
        "name: open\n"
        'paths: ["Planning/**", "Records/Delivery/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "open.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FD2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FD1"]\n'
        "audience: external\nceiling: 6\n",
        encoding="utf-8",
    )
    (root / "scopes" / "blocked.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FC5\n"
        "name: blocked\n"
        'paths: ["Records/Hidden/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "blocked.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FC6\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC5"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
