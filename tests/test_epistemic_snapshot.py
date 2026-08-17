"""Neutral snapshot schema: strict, JSON round-trippable, evidence-required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epistemic.snapshot import (
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    Relation,
    StateItem,
)

PROJECTOR = ProjectorMeta(
    name="fixture-projector",
    version="0.1.0",
    author="benchmark-harness",
    endpoints_used=("fixture:in-memory",),
    loc=1,
)


def _snapshot() -> EpistemicStateSnapshot:
    return EpistemicStateSnapshot(
        provider="fixture",
        variant="native",
        phase="p1",
        taken_at="2026-01-01T00:00:00Z",
        items=(
            StateItem(
                id="src-1",
                kind="raw_source",
                title="Retrieval budget capture",
                text="A raw capture kept verbatim.",
                current="yes",
                authored_by="human",
                locator="Knowledge Base/Sources/Articles/2026-01-05-retrieval-budget",
                locator_kind="file",
                observed_at="2026-01-05T00:00:00Z",
                raw={"type": "source"},
            ),
            StateItem(
                id="claim-1",
                kind="claim",
                title="Retrieval budget should be bounded",
                text="Bounded retrieval beats unbounded scans.",
                current="yes",
                cites=("src-1",),
                uncertainty="single source only",
                authored_by="agent",
            ),
        ),
        relations=(Relation(subject="claim-1", predicate="cites", object="src-1"),),
        declarations=(
            FieldDeclaration(
                field="cites",
                status="declared",
                evidence="src/exomem/_scaffold/_Schema/references/frontmatter.md:59",
            ),
        ),
        projector=PROJECTOR,
        completeness_notes="hand-built",
    )


def test_snapshot_json_round_trip_is_lossless() -> None:
    original = _snapshot()
    restored = EpistemicStateSnapshot.model_validate_json(original.model_dump_json())
    assert restored == original


def test_snapshot_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StateItem(id="x", kind="claim", title="t", text="", unexpected="nope")


def test_snapshot_rejects_unknown_item_kind() -> None:
    with pytest.raises(ValidationError):
        StateItem(id="x", kind="belief", title="t", text="")


def test_snapshot_rejects_duplicate_item_ids() -> None:
    duplicate = StateItem(id="dup", kind="claim", title="t", text="")
    with pytest.raises(ValidationError):
        EpistemicStateSnapshot(
            provider="fixture",
            variant="native",
            phase="p1",
            taken_at="2026-01-01T00:00:00Z",
            items=(duplicate, duplicate),
            projector=PROJECTOR,
        )


def test_field_declaration_requires_non_empty_evidence() -> None:
    with pytest.raises(ValidationError):
        FieldDeclaration(field="cites", status="declared", evidence="")
    with pytest.raises(ValidationError):
        FieldDeclaration(field="cites", status="declared", evidence="   ")


def test_field_declaration_status_vocabulary_is_closed() -> None:
    ok = FieldDeclaration(
        field="prior_revision",
        status="available_via:vcs",
        evidence="docs/example.md:1",
    )
    assert ok.mechanism == "vcs"
    for status in ("declared", "absent_by_design", "unavailable"):
        assert FieldDeclaration(field="f", status=status, evidence="d.md:1").status == status
    with pytest.raises(ValidationError):
        FieldDeclaration(field="f", status="maybe", evidence="d.md:1")
    with pytest.raises(ValidationError):
        FieldDeclaration(field="f", status="available_via:", evidence="d.md:1")


def test_projector_meta_publishes_size_and_endpoints() -> None:
    assert PROJECTOR.loc >= 0
    assert PROJECTOR.endpoints_used
    with pytest.raises(ValidationError):
        ProjectorMeta(
            name="p", version="1", author="a", endpoints_used=("x",), loc=-1
        )


def test_snapshot_lookup_helpers() -> None:
    snapshot = _snapshot()
    assert snapshot.item("claim-1").kind == "claim"
    assert snapshot.item("missing") is None
    assert snapshot.declaration("cites").status == "declared"
    assert snapshot.declaration("nothing") is None
