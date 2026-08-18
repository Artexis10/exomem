"""Acceptance for correcting a captured source's classification.

Classification was a one-way door: `edit.py` refuses every write into
`Sources/`, so a source captured under the wrong kind stayed wrong forever, and
the `source_classification_debt` advisory shipped with the open taxonomy had
nothing to act on. This suite binds the correction path:

- kind and domain become correctable, and the location follows the corrected
  values through the same projection capture uses;
- the body is byte-identical afterwards, and identity and provenance survive;
- every inbound reference follows the source and the old path stays discoverable;
- nothing reclassifies on its own, and nothing guesses a kind for you.

Every fixture here is synthetic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import reclassify_source as rc
from exomem import schema as schema_module
from exomem import source_taxonomy as st

TODAY = dt.date(2026, 8, 18)
KB = "Knowledge Base"


def _capture(
    vault: Path, source_schema: schema_module.SourceSchema, **kwargs: object
) -> add_module.AddResult:
    kwargs.setdefault("content", "Body text for a captured source.\n\nSecond paragraph.")
    kwargs.setdefault("today", TODAY)
    return add_module.add(vault, source_schema, **kwargs)  # type: ignore[arg-type]


def _front(vault: Path, rel: str) -> dict:
    text = (vault / rel).read_text(encoding="utf-8")
    front, _, _ = text.partition("\n---\n")
    return yaml.safe_load(front.removeprefix("---\n"))


def _body(vault: Path, rel: str) -> str:
    from exomem.vault import parse_frontmatter

    _, body, _ = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
    return body


# ---------------------------------------------------------------------------
# The correction itself
# ---------------------------------------------------------------------------
def test_a_fallback_capture_is_corrected_to_a_real_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    assert captured.path.startswith(f"{KB}/Sources/Other/Travel/")

    result = rc.reclassify(
        vault,
        path=captured.path,
        source_kind="research-report",
        reason="it is a written investigation, not unclassified material",
        today=TODAY,
    )

    assert result.new_path == f"{KB}/Sources/Reports/Travel/2026-08-18-airfare-notes.md"
    assert result.relocated is True
    assert not (vault / captured.path).exists()
    front = _front(vault, result.new_path)
    assert front["source_type"] == "research-report"
    assert front["domain"] == "travel"


def test_a_domain_is_corrected_without_touching_the_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Kelp survey", source_type="research-report",
        domain="travel",
    )
    result = rc.reclassify(
        vault, path=captured.path, domain="marine-biology",
        reason="the subject is the survey, not the trip", today=TODAY,
    )
    assert result.kind == "research-report"
    front = _front(vault, result.new_path)
    assert front["source_type"] == "research-report"
    assert front["domain"] == "marine-biology"
    assert result.new_path.startswith(f"{KB}/Sources/Reports/Marine Biology/")


def test_a_correction_that_does_not_change_the_projection_moves_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Order confirmation", source_type="invoice-receipt",
        domain="equipment",
    )
    result = rc.reclassify(
        vault, path=captured.path, source_kind="invoice-receipt",
        reason="confirming the existing classification after review", today=TODAY,
    )
    assert result.relocated is False
    assert result.new_path == captured.path
    assert (vault / captured.path).exists()
    front = _front(vault, result.new_path)
    assert front["source_type"] == "invoice-receipt"
    assert front["domain"] == "equipment"
    assert front["reclassified_reason"] == (
        "confirming the existing classification after review"
    )
    # The in-place branch still has to write the correction: a no-relocation
    # correction that records nothing would silently discard the caller's reason.
    front = _front(vault, result.new_path)
    assert front["source_type"] == "invoice-receipt"
    assert front["domain"] == "equipment"
    assert front["reclassified_reason"] == (
        "confirming the existing classification after review"
    )


def test_an_unseen_kind_registers_itself_like_a_capture_does(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Hedgerow notes", domain="travel")
    result = rc.reclassify(
        vault, path=captured.path, source_kind="field-notebook",
        reason="handwritten observations recorded on site", today=TODAY,
    )
    registry = yaml.safe_load(st.registry_path(vault).read_text(encoding="utf-8"))
    assert "field-notebook" in registry["source_kinds"]
    assert result.new_path.startswith(f"{KB}/Sources/Field Notebook/Travel/")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_a_correction_with_nothing_to_correct_is_refused(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    with pytest.raises(rc.ReclassifyError) as exc:
        rc.reclassify(vault, path=captured.path, reason="tidying", today=TODAY)
    assert exc.value.code == "NO_CHANGE_REQUESTED"
    assert (vault / captured.path).exists()


def test_a_correction_without_a_reason_is_refused(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    for reason in (None, "", "   "):
        with pytest.raises(rc.ReclassifyError) as exc:
            rc.reclassify(
                vault, path=captured.path, source_kind="research-report",
                reason=reason, today=TODAY,
            )
        assert exc.value.code == "REASON_REQUIRED"
    assert _front(vault, captured.path)["source_type"] == "other"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../escape", "/absolute", "travel/reports", "travel\\reports", "..", ".",
        "", "   ", "con", "nul", "a" * 80, "\u0000null", "trailing. ",
    ],
)
def test_an_unsafe_corrected_value_never_reaches_a_path(
    vault: Path, source_schema: schema_module.SourceSchema, hostile: str
) -> None:
    """Refused, or normalized into a safe key — never a path segment as supplied.

    The taxonomy normalizes rather than refuses whenever a safe canonical key
    survives, so asserting refusal would test the wrong property. The invariant
    that matters is that nothing hostile reaches the filesystem.
    """
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    try:
        result = rc.reclassify(
            vault, path=captured.path, source_kind=hostile,
            reason="attempting an unsafe value", today=TODAY,
        )
    except rc.ReclassifyError as error:
        assert error.code == "INVALID_CLASSIFICATION"
        assert (vault / captured.path).exists()
        assert _front(vault, captured.path)["source_type"] == "other"
        return

    assert result.new_path.startswith(f"{KB}/Sources/")
    assert ".." not in result.new_path
    assert "\\" not in result.new_path
    assert result.new_path.count("/") == 4  # KB/Sources/<Kind>/<Domain>/<file>
    landed = (vault / result.new_path).resolve()
    assert landed.is_file()
    assert landed.is_relative_to((vault / KB / "Sources").resolve())


def test_a_compiled_note_is_refused_because_it_carries_no_source_kind(
    vault: Path,
) -> None:
    note = vault / KB / "Notes" / "Insights" / "example.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntype: insight\n---\n\n# Example\n", encoding="utf-8")
    with pytest.raises(rc.ReclassifyError) as exc:
        rc.reclassify(
            vault, path=f"{KB}/Notes/Insights/example.md", source_kind="article",
            reason="wrong tree", today=TODAY,
        )
    assert exc.value.code == "NOT_A_SOURCE"


def test_a_missing_source_is_refused(vault: Path) -> None:
    with pytest.raises(rc.ReclassifyError) as exc:
        rc.reclassify(
            vault, path=f"{KB}/Sources/Other/nope.md", source_kind="article",
            reason="does not exist", today=TODAY,
        )
    assert exc.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Immutability — the body and the provenance
# ---------------------------------------------------------------------------
def test_the_body_survives_a_correction_byte_identically(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Airfare notes", domain="travel",
        content="First paragraph.\n\n- a bullet\n- another\n\nClosing line.",
    )
    before = _body(vault, captured.path)
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )
    assert _body(vault, result.new_path) == before


def test_identity_and_provenance_survive_a_correction(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Airfare notes", domain="travel",
        url="https://example.com/fares", tags=["fares"],
        why_captured="baseline for later comparison",
    )
    before = _front(vault, captured.path)
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )
    after = _front(vault, result.new_path)
    for preserved in ("exomem_id", "title", "captured", "url", "tags", "ingested_into"):
        assert after[preserved] == before[preserved], preserved


def test_the_correction_records_its_reason_and_previous_path(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="it is a written investigation", today=TODAY,
    )
    after = _front(vault, result.new_path)
    assert str(after["reclassified"]) == TODAY.isoformat()
    assert after["reclassified_from"] == captured.path
    assert after["reclassified_reason"] == "it is a written investigation"


def test_a_correction_that_does_not_relocate_records_no_previous_path(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Order confirmation",
        source_type="invoice-receipt", domain="equipment",
    )
    result = rc.reclassify(
        vault, path=captured.path, source_kind="invoice-receipt",
        reason="confirmed on review", today=TODAY,
    )
    assert "reclassified_from" not in _front(vault, result.new_path)


# ---------------------------------------------------------------------------
# References follow the source
# ---------------------------------------------------------------------------
def test_inbound_references_follow_the_source(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    stem = captured.path.removesuffix(".md")

    citing = vault / KB / "Notes" / "Insights" / "fares-conclusion.md"
    citing.parent.mkdir(parents=True, exist_ok=True)
    citing.write_text(
        f'---\ntype: insight\nsources:\n  - "[[{stem}]]"\n---\n\n'
        f"# Fares conclusion\n\nSee [[{stem}]] for the raw material.\n\n"
        f"## Observations\n\n- [finding] Fares varied by booking window #fares\n",
        encoding="utf-8",
    )

    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )

    text = citing.read_text(encoding="utf-8")
    new_stem = result.new_path.removesuffix(".md")
    assert stem not in text
    assert text.count(new_stem) == 2
    assert result.references_updated >= 2


def test_the_sources_index_reference_is_rewritten_too(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert captured.path.removesuffix(".md") not in index
    assert result.new_path.removesuffix(".md") in index


# ---------------------------------------------------------------------------
# Proposals report evidence and decline rather than guess
# ---------------------------------------------------------------------------
def test_a_proposal_writes_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    before = (vault / captured.path).read_text(encoding="utf-8")
    proposal = rc.propose(vault, captured.path)
    assert proposal.path == captured.path
    assert (vault / captured.path).read_text(encoding="utf-8") == before


def test_a_domain_already_in_the_location_is_proposed_with_its_evidence(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A legacy source filed under `Other/<Domain>/` never got a `domain:` field."""
    legacy = vault / KB / "Sources" / "Other" / "Travel" / "2026-01-02-legacy.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\ntype: source\ntitle: Legacy item\nsource_type: other\n"
        "captured: 2026-01-02T00:00:00Z\ntags: []\ningested_into: []\n---\n\n"
        "# Legacy item\n\nBody.\n",
        encoding="utf-8",
    )
    proposal = rc.propose(vault, f"{KB}/Sources/Other/Travel/2026-01-02-legacy.md")
    assert proposal.current_domain is None
    assert proposal.proposed_domain == "travel"
    assert any("Travel" in item for item in proposal.domain_evidence)


def test_an_undecidable_kind_is_reported_not_guessed(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    proposal = rc.propose(vault, captured.path)
    assert proposal.proposed_kind is None
    assert st.FALLBACK_KIND not in (proposal.proposed_kind or "")
    assert any("no kind is proposed" in item for item in proposal.kind_evidence)


def test_a_proposal_reports_how_many_references_would_move(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    proposal = rc.propose(vault, captured.path)
    # The sources index links every capture, so at least one reference exists.
    assert proposal.references >= 1


# ---------------------------------------------------------------------------
# Never automatic
# ---------------------------------------------------------------------------
def test_capturing_more_material_relocates_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    first = _capture(vault, source_schema, title="Loose one", domain="media")
    for index in range(3):
        _capture(vault, source_schema, title=f"Loose {index}", domain="media")
    assert (vault / first.path).exists()
    assert _front(vault, first.path)["source_type"] == "other"


def test_a_registry_path_label_rename_migrates_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Report", source_type="research-report",
        domain="travel",
    )
    st.registry_path(vault).write_text(
        "schema_version: 1\nsource_kinds:\n  research-report:\n"
        "    path_label: Investigations\ndomains: {}\n",
        encoding="utf-8",
    )
    assert (vault / captured.path).exists()
    assert captured.path.startswith(f"{KB}/Sources/Reports/")
    # The rename only takes effect for material corrected or captured afterwards.
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="adopting the renamed folder", today=TODAY,
    )
    assert result.new_path.startswith(f"{KB}/Sources/Investigations/Travel/")
