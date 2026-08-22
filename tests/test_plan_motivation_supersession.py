"""Plans premised on superseded knowledge surface in the plan-progress review.

`motivation` is a bounded list of `exomem://memory/` refs from a Planning item
to the knowledge that motivates it. It was stored, validated and read by
nothing, so a plan citing a belief the vault has since replaced kept executing
unexamined. This module covers the read-only consumer that surfaces it.

The load-bearing constraint is disclosure, and it is asserted separately in
`test_plan_motivation_disclosure.py`: a missing target, a withheld target, a
blocked target, an ambiguous identity and a malformed reference must all reach
the reader as one indistinguishable outcome.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from canonical_snapshot import canonical_digests

RECORDS_ID = "49622075-9ff4-4660-9ab7-414854b5bca2"
PLANNING_ID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
RECORDS_REF = f"exomem://memory/{RECORDS_ID}"

LIVE_ID = "0f6c1a2b-3d4e-4f50-8a61-72b83c94d5e6"
DEAD_ID = "1a2b3c4d-5e6f-4071-8283-94a5b6c7d8e9"
SUCCESSOR_ID = "2b3c4d5e-6f70-4182-9394-a5b6c7d8e9fa"
TWIN_ID = "3c4d5e6f-7081-4293-84a5-b6c7d8e9fa0b"
ABSENT_ID = "4d5e6f70-8192-43a4-95b6-c7d8e9fa0b1c"

LIVE_REF = f"exomem://memory/{LIVE_ID}"
DEAD_REF = f"exomem://memory/{DEAD_ID}"
SUCCESSOR_REF = f"exomem://memory/{SUCCESSOR_ID}"
TWIN_REF = f"exomem://memory/{TWIN_ID}"
ABSENT_REF = f"exomem://memory/{ABSENT_ID}"

MOTIVATION_KEYS = (
    "motivation_refs",
    "motivation_resolved",
    "motivation_unresolved",
    "motivation_superseded",
)

PLANNING_COLLECTION = "Knowledge Base/Planning/Work/_collection.md"
RECORDS_COLLECTION = "Knowledge Base/Records/Delivery/_collection.md"

SUCCESSOR_PAGE = "Knowledge Base/Notes/Open/Successor Belief.md"
LIVE_PAGE = "Knowledge Base/Notes/Open/Live Belief.md"
DEAD_PAGE = "Knowledge Base/Notes/Open/Dead Belief.md"
TWIN_A_PAGE = "Knowledge Base/Notes/Open/Twin A.md"
TWIN_B_PAGE = "Knowledge Base/Notes/Open/Twin B.md"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


_RECORDS_MANIFEST = f"""---
type: collection
exomem_id: {RECORDS_ID}
title: Delivery sessions
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
    status:
      type: enum
      enum: [worked, shipped]
views:
  worked:
    query:
      filters: {{status: worked}}
---

Ordinary delivery observations.
"""


def _planning_manifest(*, motivation: str | None = "array") -> str:
    """A Planning manifest, optionally declaring `motivation` and in what shape.

    `motivation=None` omits the field entirely; `"string"` declares the legacy
    free-text shape a vault may legally already carry.
    """
    declared = ""
    if motivation == "array":
        declared = "    motivation:\n      type: array\n      items: {type: string}\n"
    elif motivation == "string":
        declared = "    motivation:\n      type: string\n"
    return f"""---
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
{declared}---
"""


def _page(exomem_id: str, title: str, *, status: str = "active", successor: str = "") -> str:
    lines = [
        "---",
        "type: note",
        f"exomem_id: {exomem_id}",
        f"title: {title}",
        f"status: {status}",
    ]
    if successor:
        lines.append("superseded_by:")
        lines.append(f"  - '[[{successor}]]'")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append("A belief this vault holds.")
    lines.append("")
    return "\n".join(lines)


def _write_page(root: Path, relative: str, markdown: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")


def _build_vault(root: Path, *, motivation: str | None = "array", worked: int = 2) -> None:
    from exomem import planning, records

    (root / "Knowledge Base").mkdir(parents=True, exist_ok=True)
    (root / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    records.create_collection(
        root, RECORDS_COLLECTION, _RECORDS_MANIFEST, why="create records collection"
    )
    for index in range(worked):
        records.append_record(
            root,
            RECORDS_COLLECTION,
            item={
                "occurred_on": f"2026-08-{index + 1:02d}",
                "label": f"session-{index}",
                "status": "worked",
            },
            why="record observed work",
        )
    planning.create_collection(
        root,
        PLANNING_COLLECTION,
        _planning_manifest(motivation=motivation),
        why="create planning collection",
    )


def _seed_beliefs(root: Path) -> None:
    """Write the knowledge pages the plans below cite."""
    _write_page(root, SUCCESSOR_PAGE, _page(SUCCESSOR_ID, "Successor Belief"))
    _write_page(root, LIVE_PAGE, _page(LIVE_ID, "Live Belief"))
    _write_page(
        root,
        DEAD_PAGE,
        _page(DEAD_ID, "Dead Belief", status="superseded", successor="Notes/Open/Successor Belief"),
    )
    _write_page(root, TWIN_A_PAGE, _page(TWIN_ID, "Twin A"))
    _write_page(root, TWIN_B_PAGE, _page(TWIN_ID, "Twin B"))


def _committed(title: str, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "kind": "outcome",
        "status": "active",
        "commitment": "committed",
        "horizon": "quarter",
        "priority": "high",
    }
    item.update(extra)
    return item


def _add_plan(root: Path, item: Mapping[str, Any], plan_id: str | None = None) -> str:
    from exomem import planning

    return planning.add(
        root, PLANNING_COLLECTION, item=dict(item), plan_id=plan_id, why="capture intent"
    )["plan_id"]


def _worked() -> list[dict[str, str]]:
    return [{"collection": RECORDS_REF, "role": "progress", "view": "worked"}]


def _sidecar_bytes(root: Path) -> bytes | None:
    from exomem import memory_refs

    path = memory_refs.sidecar_path(root)
    return path.read_bytes() if path.exists() else None


def _motivation_entry(item: Mapping[str, Any], index: int = 0) -> Mapping[str, Any]:
    return item["motivation"][index]


def _item_by_title(result: Mapping[str, Any], title: str) -> Mapping[str, Any]:
    [item] = [entry for entry in result["items"] if entry["intent"]["title"] == title]
    return item


# --------------------------------------------------------------------------
# The batch resolution primitive
# --------------------------------------------------------------------------


def test_batch_resolver_returns_every_path_holding_one_identity(tmp_path: Path) -> None:
    """A duplicated identity is a tuple of two, never an `AMBIGUOUS_REFERENCE`.

    The ambiguity message carries a count of the pages a caller may hold no
    release decision over, so a caller forbidden to disclose that count cannot
    afford to catch the exception.
    """
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)

    resolved = memory_refs.paths_for_ids_read_only(tmp_path, [TWIN_ID, LIVE_ID])

    assert resolved[TWIN_ID] == (
        "Knowledge Base/Notes/Open/Twin A.md",
        "Knowledge Base/Notes/Open/Twin B.md",
    )
    assert resolved[LIVE_ID] == ("Knowledge Base/Notes/Open/Live Belief.md",)


def test_batch_resolver_reports_an_absent_identity_as_an_empty_tuple(tmp_path: Path) -> None:
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)

    resolved = memory_refs.paths_for_ids_read_only(tmp_path, [ABSENT_ID])

    assert resolved == {ABSENT_ID: ()}


def test_batch_resolver_drops_a_malformed_identity_without_raising(tmp_path: Path) -> None:
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)

    resolved = memory_refs.paths_for_ids_read_only(
        tmp_path, ["not-a-uuid", "", None, LIVE_ID, LIVE_ID]
    )

    assert resolved == {LIVE_ID: ("Knowledge Base/Notes/Open/Live Belief.md",)}


def test_batch_resolver_scans_the_corpus_once_for_the_whole_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One scan per batch, never one per identifier.

    Both per-ref helpers cost a corpus pass each, which is what makes them the
    wrong primitive for a review that may hold hundreds of refs.
    """
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)

    scans: list[int] = []
    real = memory_refs._scan_pages

    def counted(vault_root: Path) -> Any:
        scans.append(1)
        return real(vault_root)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted)
    resolved = memory_refs.paths_for_ids_read_only(
        tmp_path, [LIVE_ID, DEAD_ID, TWIN_ID, ABSENT_ID, SUCCESSOR_ID]
    )

    assert len(scans) == 1
    assert resolved[DEAD_ID] == ("Knowledge Base/Notes/Open/Dead Belief.md",)


def test_batch_resolver_prefers_a_current_sidecar_over_a_corpus_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)
    memory_refs.ReferenceIndex(tmp_path).rebuild_all()

    def refuse(vault_root: Path) -> Any:
        raise AssertionError("the sidecar was current; no scan should have run")

    monkeypatch.setattr(memory_refs, "_scan_pages", refuse)
    resolved = memory_refs.paths_for_ids_read_only(tmp_path, [LIVE_ID, TWIN_ID])

    assert resolved[LIVE_ID] == ("Knowledge Base/Notes/Open/Live Belief.md",)
    assert len(resolved[TWIN_ID]) == 2


def test_batch_resolver_never_creates_or_rewrites_the_sidecar(tmp_path: Path) -> None:
    """Read-only in the strict sense, because its caller must not write.

    `.refs.sqlite` is registered internal state, so the canonical byte census
    skips it — an accidental rebuild here is invisible to the shipped
    write-guard tests and has to be asserted directly.
    """
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)

    memory_refs.paths_for_ids_read_only(tmp_path, [LIVE_ID, ABSENT_ID])
    assert not memory_refs.sidecar_path(tmp_path).exists()

    memory_refs.ReferenceIndex(tmp_path).rebuild_all()
    before = _sidecar_bytes(tmp_path)
    memory_refs.paths_for_ids_read_only(tmp_path, [LIVE_ID, ABSENT_ID, TWIN_ID])

    assert _sidecar_bytes(tmp_path) == before


def test_batch_resolver_chunks_a_batch_wider_than_the_sqlite_variable_limit(
    tmp_path: Path,
) -> None:
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)
    memory_refs.ReferenceIndex(tmp_path).rebuild_all()
    wide = [f"00000000-0000-4000-8000-{index:012d}" for index in range(5000)]

    resolved = memory_refs.paths_for_ids_read_only(tmp_path, [*wide, LIVE_ID])

    assert resolved[LIVE_ID] == ("Knowledge Base/Notes/Open/Live Belief.md",)
    assert all(resolved[identifier] == () for identifier in wide)


def test_batch_resolver_reads_a_read_only_vault_without_a_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault whose sidecar cannot be opened still resolves from Markdown."""
    from exomem import memory_refs

    (tmp_path / "Knowledge Base").mkdir()
    _seed_beliefs(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(memory_refs.ReferenceIndex, "_connect", refuse)
    monkeypatch.setattr(memory_refs.ReferenceIndex, "_connect_readonly", refuse)

    resolved = memory_refs.paths_for_ids_read_only(tmp_path, [LIVE_ID])

    assert resolved[LIVE_ID] == ("Knowledge Base/Notes/Open/Live Belief.md",)


# --------------------------------------------------------------------------
# Selection widening
# --------------------------------------------------------------------------


def test_selection_admits_a_committed_item_carrying_only_motivation() -> None:
    from exomem import plan_progress

    row = {
        "collection_id": PLANNING_ID,
        "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516",
        "title": "Premised on a belief",
        "kind": "outcome",
        "status": "active",
        "lifecycle": "active",
        "commitment": "committed",
        "motivation": [DEAD_REF],
    }

    assert plan_progress.selects_item(row) is True


@pytest.mark.parametrize("motivation", ([], None, "not-a-list", [7], [""]))
def test_selection_still_excludes_an_item_with_neither_evidence_nor_motivation(
    motivation: Any,
) -> None:
    from exomem import plan_progress

    row = {
        "collection_id": PLANNING_ID,
        "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516",
        "title": "Unbound",
        "kind": "outcome",
        "status": "active",
        "lifecycle": "active",
        "commitment": "committed",
        "motivation": motivation,
    }

    assert plan_progress.selects_item(row) is False


def test_selection_widening_does_not_relax_lifecycle_status_or_commitment() -> None:
    from exomem import plan_progress

    base = {
        "collection_id": PLANNING_ID,
        "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516",
        "title": "Premised on a belief",
        "kind": "outcome",
        "status": "active",
        "lifecycle": "active",
        "commitment": "committed",
        "motivation": [DEAD_REF],
    }
    for override in ({"status": "planned"}, {"lifecycle": "archived"}, {"commitment": "considering"}):
        assert plan_progress.selects_item({**base, **override}) is False


def test_motivation_refs_keep_authored_order_and_are_bounded() -> None:
    from exomem import plan_progress

    refs = [f"exomem://memory/00000000-0000-4000-8000-{index:012d}" for index in range(20)]
    row = {"motivation": [*refs, 7, None, {"ref": DEAD_REF}]}

    kept = plan_progress.motivation_refs(row)

    assert kept == refs[: plan_progress.MAX_MOTIVATION]
    assert plan_progress.MAX_MOTIVATION == 16


def test_motivation_refs_keep_a_malformed_but_bounded_string() -> None:
    """A malformed reference must stay visible, so it can collapse like the rest.

    `INVALID_REFERENCE` is one of the five outcomes the disclosure rule folds
    together. Dropping it during normalization would make it the one case a
    caller could distinguish, by counting.
    """
    from exomem import plan_progress

    row = {"motivation": ["exomem://memory/not-a-uuid", DEAD_REF]}

    assert plan_progress.motivation_refs(row) == ["exomem://memory/not-a-uuid", DEAD_REF]


# --------------------------------------------------------------------------
# Divergence counts
# --------------------------------------------------------------------------


def test_divergence_called_with_one_argument_keeps_the_shipped_seven_keys() -> None:
    """The shipped call shape is `divergence(entries)` and pins seven keys.

    `tests/test_plan_progress_review.py` asserts that dict by equality, so the
    motivation counts must arrive through an explicitly supplied second
    argument rather than by widening the single-argument result.
    """
    from exomem import plan_progress

    counts = plan_progress.divergence([])

    assert set(counts) == set(plan_progress._DIVERGENCE_KEYS)
    assert not any(key in counts for key in MOTIVATION_KEYS)


def test_divergence_reports_four_motivation_counts_as_plain_integers() -> None:
    from exomem import plan_progress

    counts = plan_progress.divergence(
        [],
        [
            {"memory": DEAD_REF, "resolved": True, "unresolved_reason": None, "superseded": True},
            {"memory": LIVE_REF, "resolved": True, "unresolved_reason": None, "superseded": False},
            {
                "memory": ABSENT_REF,
                "resolved": False,
                "unresolved_reason": "motivation_unavailable",
                "superseded": None,
            },
        ],
    )

    assert counts["motivation_refs"] == 3
    assert counts["motivation_resolved"] == 2
    assert counts["motivation_unresolved"] == 1
    assert counts["motivation_superseded"] == 1
    # `type(True) is int` is False, and the shipped suite asserts exactly that
    # over every divergence value. No motivation key may be a boolean.
    assert all(type(counts[key]) is int for key in MOTIVATION_KEYS)


def test_divergence_motivation_counts_hold_their_invariants() -> None:
    from exomem import plan_progress

    entries = [
        {"memory": DEAD_REF, "resolved": True, "unresolved_reason": None, "superseded": True},
        {"memory": LIVE_REF, "resolved": True, "unresolved_reason": None, "superseded": False},
        {
            "memory": ABSENT_REF,
            "resolved": False,
            "unresolved_reason": "motivation_unavailable",
            "superseded": None,
        },
        {
            "memory": TWIN_REF,
            "resolved": False,
            "unresolved_reason": "motivation_budget_exhausted",
            "superseded": None,
        },
    ]

    counts = plan_progress.divergence([], entries)

    assert counts["motivation_refs"] == counts["motivation_resolved"] + counts["motivation_unresolved"]
    assert counts["motivation_superseded"] <= counts["motivation_resolved"]


def test_divergence_with_an_empty_motivation_list_still_carries_the_four_keys() -> None:
    """Every reviewed item carries the four counts, so zero reads as zero."""
    from exomem import plan_progress

    counts = plan_progress.divergence([], [])

    assert [counts[key] for key in MOTIVATION_KEYS] == [0, 0, 0, 0]


# --------------------------------------------------------------------------
# Review integration
# --------------------------------------------------------------------------


def test_review_flags_a_plan_premised_on_superseded_knowledge(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Premised on a dead belief", motivation=[DEAD_REF]))

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    entry = _motivation_entry(item)
    assert entry["memory"] == DEAD_REF
    assert entry["resolved"] is True
    assert entry["superseded"] is True
    assert entry["unresolved_reason"] is None
    assert item["divergence"]["motivation_refs"] == 1
    assert item["divergence"]["motivation_resolved"] == 1
    assert item["divergence"]["motivation_superseded"] == 1
    assert item["divergence"]["motivation_unresolved"] == 0


def test_review_reports_a_live_motivation_reference_as_not_superseded(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Premised on a live belief", motivation=[LIVE_REF]))

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    entry = _motivation_entry(item)
    assert entry["resolved"] is True
    assert entry["superseded"] is False
    assert item["divergence"]["motivation_superseded"] == 0


def test_a_hand_edited_supersession_pointer_is_not_supersession(tmp_path: Path) -> None:
    """`status == "superseded"` alone decides it.

    A non-empty `superseded_by` is also true of a page whose status was never
    flipped. Treating that as supersession would report an inconsistency as a
    fact; reporting the inconsistency belongs to the audit surface.
    """
    from exomem import plan_progress

    _build_vault(tmp_path)
    _write_page(tmp_path, SUCCESSOR_PAGE, _page(SUCCESSOR_ID, "Successor Belief"))
    _write_page(
        tmp_path,
        LIVE_PAGE,
        _page(LIVE_ID, "Live Belief", status="active", successor="Notes/Open/Successor Belief"),
    )
    _add_plan(tmp_path, _committed("Half-edited target", motivation=[LIVE_REF]))

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    entry = _motivation_entry(item)
    assert entry["resolved"] is True
    assert entry["superseded"] is False
    assert item["divergence"]["motivation_superseded"] == 0


def test_review_admits_a_motivation_only_item_alongside_evidence_items(tmp_path: Path) -> None:
    """The unwidened slice would be silent on every real case.

    `motivation` is a cold field with no production usage, and `selects_item`
    required evidence bindings, so a plan citing a dead belief without them was
    never examined at all.
    """
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Motivation only", motivation=[DEAD_REF]))
    _add_plan(tmp_path, _committed("Evidence only", progress_evidence=_worked()))
    _add_plan(tmp_path, _committed("Neither"))

    result = plan_progress.review(tmp_path)

    assert sorted(item["intent"]["title"] for item in result["items"]) == [
        "Evidence only",
        "Motivation only",
    ]
    evidence_only = _item_by_title(result, "Evidence only")
    assert evidence_only["motivation"] == []
    assert evidence_only["divergence"]["motivation_refs"] == 0
    assert _item_by_title(result, "Motivation only")["evidence"] == []


def test_every_reviewed_item_carries_the_four_motivation_counts(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _add_plan(tmp_path, _committed("Evidence only", progress_evidence=_worked()))

    result = plan_progress.review(tmp_path)

    [item] = result["items"]
    assert all(key in item["divergence"] for key in MOTIVATION_KEYS)
    assert all(type(value) is int for value in item["divergence"].values())


def test_motivation_is_projected_only_where_the_manifest_declares_the_array_form(
    tmp_path: Path,
) -> None:
    """A legacy free-text `motivation` is not the governed ref list.

    `name in manifest.schema.fields` is not the predicate: the governed shape
    additionally requires `type == "array"`, which is exactly the legacy case
    the predicate exists to exclude. Reading a string field as refs would
    invent counts from prose.
    """
    from exomem import plan_progress

    _build_vault(tmp_path, motivation="string")
    _seed_beliefs(tmp_path)
    _add_plan(
        tmp_path,
        _committed("Legacy prose", progress_evidence=_worked(), motivation="because we said so"),
    )

    result = plan_progress.review(tmp_path)

    assert result["collections_unavailable"] == 0
    [item] = result["items"]
    assert item["motivation"] == []
    assert item["divergence"]["motivation_refs"] == 0
    assert "because we said so" not in str(result)


def test_undeclared_motivation_field_does_not_refuse_the_collection(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path, motivation=None)
    _add_plan(tmp_path, _committed("Evidence only", progress_evidence=_worked()))

    result = plan_progress.review(tmp_path)

    assert result["collections_scanned"] == 1
    assert result["collections_unavailable"] == 0
    [item] = result["items"]
    assert item["motivation"] == []


def test_motivation_budget_is_spent_on_retained_items_only(tmp_path: Path) -> None:
    """Budget after ordering and truncation, or it is spent on absent items."""
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(
        tmp_path,
        _committed("Aaa", motivation=[DEAD_REF]),
        "11111111-1111-4111-8111-111111111111",
    )
    _add_plan(
        tmp_path,
        _committed("Bbb", motivation=[LIVE_REF]),
        "22222222-2222-4222-8222-222222222222",
    )

    result = plan_progress.review(tmp_path, limit=1, motivation_budget=1)

    assert result["items_reviewed"] == 1
    assert result["motivation_consulted"] == 1
    assert result["motivation_truncated"] is False
    [item] = result["items"]
    assert _motivation_entry(item)["resolved"] is True


def test_motivation_budget_truncates_explicitly_and_before_any_target_is_consulted(
    tmp_path: Path,
) -> None:
    """The budget verdict is a counter, never an observation of the target.

    Computing it before anything is consulted is what keeps
    `motivation_budget_exhausted` existence-independent, so it cannot become a
    second probe channel beside the collapsed unavailable reason.
    """
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Two beliefs", motivation=[DEAD_REF, LIVE_REF]))

    result = plan_progress.review(tmp_path, motivation_budget=1)

    [item] = result["items"]
    first, second = item["motivation"]
    assert first["resolved"] is True
    assert second["resolved"] is False
    assert second["unresolved_reason"] == "motivation_budget_exhausted"
    assert second["superseded"] is None
    assert result["motivation_truncated"] is True
    assert result["motivation_consulted"] == 1
    assert result["unavailable"]["motivation_budget_exhausted"] == 1
    # The invariant holds through truncation: a skipped ref is unresolved.
    assert item["divergence"]["motivation_refs"] == 2
    assert item["divergence"]["motivation_unresolved"] == 1


def test_motivation_budget_is_separate_from_the_execution_budget(tmp_path: Path) -> None:
    """`budget_exhausted` keeps meaning exactly one thing: a Records view was skipped."""
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(
        tmp_path,
        _committed("Both", progress_evidence=_worked(), motivation=[DEAD_REF, LIVE_REF]),
    )

    result = plan_progress.review(tmp_path, motivation_budget=1)

    assert result["bindings_executed"] == 1
    assert result["bindings_truncated"] is False
    assert result["unavailable"]["budget_exhausted"] == 0
    assert result["unavailable"]["motivation_budget_exhausted"] == 1
    assert plan_progress.DEFAULT_MOTIVATION_BUDGET == 64
    assert plan_progress.MAX_MOTIVATION_BUDGET == 256


def test_one_identity_cited_by_several_plans_is_resolved_once(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    for title in ("First", "Second", "Third"):
        _add_plan(tmp_path, _committed(title, motivation=[DEAD_REF]))

    result = plan_progress.review(tmp_path)

    assert result["motivation_consulted"] == 1
    assert [_motivation_entry(item)["superseded"] for item in result["items"]] == [True] * 3
    assert sum(item["divergence"]["motivation_superseded"] for item in result["items"]) == 3


def test_review_never_names_the_successor(tmp_path: Path) -> None:
    """Non-goal, deliberately: naming it needs a second unreleased hop.

    The successor is reached through a wikilink to a distinct disclosure
    subject, and the obvious resolver applies no release check at all.
    Emitting nothing about it keeps the entry shape uniform and stops
    "successor known" becoming a second probe channel.
    """
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Premised on a dead belief", motivation=[DEAD_REF]))

    result = plan_progress.review(tmp_path)

    serialized = str(result)
    assert "Successor Belief" not in serialized
    assert SUCCESSOR_REF not in serialized
    assert "superseded_by" not in serialized
    assert "successor" not in serialized


def test_review_discloses_no_path_or_title_for_a_resolved_motivation(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Premised on a dead belief", motivation=[DEAD_REF]))

    result = plan_progress.review(tmp_path)

    serialized = str(result)
    assert "Dead Belief" not in serialized
    assert "Notes/Open" not in serialized
    assert not [key for key, _ in _walk(result) if key in {"ref", "fingerprint"}]


def test_review_leaves_the_reference_sidecar_byte_identical(tmp_path: Path) -> None:
    """`.refs.sqlite` is registered internal state, so the canonical census skips it.

    An accidental `rebuild_all()` inside a review is therefore invisible to
    both shipped write-guard tests, and has to be asserted on the sidecar's own
    bytes.
    """
    from exomem import memory_refs, plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(
        tmp_path,
        _committed("Premised", progress_evidence=_worked(), motivation=[DEAD_REF, ABSENT_REF]),
    )
    memory_refs.ReferenceIndex(tmp_path).rebuild_all()

    canonical_before = canonical_digests(tmp_path)
    sidecar_before = _sidecar_bytes(tmp_path)
    assert sidecar_before is not None

    plan_progress.review(tmp_path)
    plan_progress.review(tmp_path)

    assert _sidecar_bytes(tmp_path) == sidecar_before
    assert canonical_digests(tmp_path) == canonical_before


def test_review_creates_no_reference_sidecar_where_none_existed(tmp_path: Path) -> None:
    from exomem import memory_refs, plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(tmp_path, _committed("Premised", motivation=[DEAD_REF, ABSENT_REF]))
    sidecar = memory_refs.sidecar_path(tmp_path)
    if sidecar.exists():
        for companion in sidecar.parent.glob(f"{sidecar.name}*"):
            companion.unlink()

    plan_progress.review(tmp_path)

    assert not sidecar.exists()


def test_review_module_reaches_no_reference_writing_entry_point() -> None:
    """The shipped source grep does not name these, and they all write."""
    from exomem import plan_progress

    source = Path(plan_progress.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "rebuild_all",
        "ReferenceIndex(",
        "refresh_paths",
        "upsert_after_write",
        # The paren matters: `resolve_identifier` is a substring of the
        # read-only variant, and only the writing one is forbidden.
        "resolve_identifier(",
    ):
        assert forbidden not in source, f"review module reaches {forbidden}"


def test_review_response_carries_no_score_shaped_motivation_value(tmp_path: Path) -> None:
    from exomem import plan_progress

    _build_vault(tmp_path)
    _seed_beliefs(tmp_path)
    _add_plan(
        tmp_path,
        _committed("Premised", progress_evidence=_worked(), motivation=[DEAD_REF, LIVE_REF]),
    )

    result = plan_progress.review(tmp_path)

    assert not [value for _, value in _walk(result) if isinstance(value, float)]
    for item in result["items"]:
        assert all(type(value) is int for value in item["divergence"].values())


def _walk(payload: Any, key: Any = None) -> Any:
    yield key, payload
    if isinstance(payload, Mapping):
        for name, value in payload.items():
            yield from _walk(value, name)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _walk(value, key)
