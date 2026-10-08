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
    # What a capture with no agent in the loop records: no kind was chosen.
    kwargs.setdefault("source_type", st.UNCLASSIFIED_KIND)
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
    assert captured.path.startswith(f"{KB}/Sources/Unclassified/Travel/")

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
    assert _front(vault, captured.path)["source_type"] == "unclassified"


@pytest.mark.parametrize("unchosen", ["other", "unclassified"])
def test_a_correction_cannot_file_a_source_under_no_chosen_kind(
    vault: Path, source_schema: schema_module.SourceSchema, unchosen: str
) -> None:
    captured = _capture(
        vault, source_schema, title="Field log", source_type="field-notebook"
    )
    before = (vault / captured.path).read_bytes()

    with pytest.raises(rc.ReclassifyError) as preview:
        rc.propose(vault, captured.path, source_kind=unchosen)
    with pytest.raises(rc.ReclassifyError) as correction:
        rc.reclassify(
            vault, path=captured.path, source_kind=unchosen,
            reason="unsure what it is", today=TODAY,
        )

    assert preview.value.code == correction.value.code == "SOURCE_KIND_REQUIRED"
    assert (vault / captured.path).read_bytes() == before


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
        assert _front(vault, captured.path)["source_type"] == "unclassified"
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


def test_a_crlf_source_is_corrected_and_keeps_its_line_endings(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    page = vault / captured.path
    page.write_bytes(page.read_bytes().replace(b"\n", b"\r\n"))

    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )

    raw = (vault / result.new_path).read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    from exomem.vault import parse_frontmatter

    front, _, _ = parse_frontmatter(raw.decode("utf-8"))
    assert front["source_type"] == "research-report"
    assert front["reclassified_reason"] == "a written investigation"


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
    assert after["reclassified_from"] == [
        {"path": captured.path, "kind": "unclassified", "domain": "travel"}
    ]
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
    assert (proposal.proposed_kind or "") not in st.UNCHOSEN_KINDS
    assert any("no kind is proposed" in item for item in proposal.kind_evidence)


def test_a_caller_previews_the_correction_it_has_decided_on(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The agent reads the source and decides; the preview is what it shows first.

    Without this the read-only mode can only preview its own (usually empty)
    proposal, so an agent that has judged the kind has no way to show the user
    where the file lands before writing -- which makes propose-then-confirm
    unusable for exactly the case it exists to serve.
    """
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    before = (vault / captured.path).read_text(encoding="utf-8")

    proposal = rc.propose(vault, captured.path, source_kind="research-report")

    assert proposal.proposed_kind == "research-report"
    assert proposal.destination == f"{KB}/Sources/Reports/Travel/2026-08-18-airfare-notes.md"
    assert proposal.relocation_required is True
    assert "supplied by the caller" in proposal.kind_evidence
    assert (vault / captured.path).read_text(encoding="utf-8") == before


def test_a_previewed_domain_is_canonicalized_not_echoed(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Kelp survey", source_type="research-report")

    proposal = rc.propose(vault, captured.path, domain="Marine Biology")

    assert proposal.proposed_domain == "marine-biology"
    assert proposal.destination is not None
    assert proposal.destination.startswith(f"{KB}/Sources/Reports/Marine Biology/")


def test_a_previewed_destination_is_never_an_escape_hatch(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A hostile supplied value must not become a previewed path.

    The taxonomy normalizes rather than refuses whenever a safe canonical key
    survives, so the property to bind is the one that matters: whatever a caller
    supplies, the destination it is shown stays inside the source tree at the
    projection's fixed depth. Otherwise the preview is the place a traversal
    reaches the user as an approved-looking suggestion.
    """
    captured = _capture(vault, source_schema, title="Loose item", domain="media")

    for hostile in ("../../escape", "..", "/etc", "a\\b", "travel/reports"):
        try:
            proposal = rc.propose(vault, captured.path, source_kind=hostile)
        except st.TaxonomyError:
            continue  # refused outright is the other acceptable outcome
        destination = proposal.destination
        if destination is None:
            continue
        assert destination.startswith(f"{KB}/Sources/")
        assert ".." not in destination.split("/")
        assert destination.count("/") == 4
        assert (vault / destination).resolve().is_relative_to((vault / KB).resolve())


def test_the_preview_is_reachable_through_the_operation(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import commands

    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    out = commands.op_manage_memory_file(
        vault,
        operation="propose-reclassification",
        path=captured.path,
        source_kind="research-report",
    )
    assert out["proposed_kind"] == "research-report"
    assert out["relocation_required"] is True


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
    assert _front(vault, first.path)["source_type"] == "unclassified"


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


# ---------------------------------------------------------------------------
# The product surface
# ---------------------------------------------------------------------------
def test_the_correction_is_reachable_through_the_governed_file_operation(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """No 30th tool: the file-lifecycle operation already owns governed moves."""
    from exomem import commands

    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    out = commands.op_manage_memory_file(
        vault,
        operation="reclassify",
        path=captured.path,
        source_kind="research-report",
        reason="a written investigation",
    )
    assert out["source_type"] == "research-report"
    assert out["path"].startswith(f"{KB}/Sources/Reports/Travel/")
    assert out["relocated"] is True


def test_the_proposal_is_reachable_and_writes_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import commands

    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    before = (vault / captured.path).read_text(encoding="utf-8")
    out = commands.op_manage_memory_file(
        vault, operation="propose-reclassification", path=captured.path
    )
    assert out["proposed_kind"] is None
    assert (vault / captured.path).read_text(encoding="utf-8") == before


def test_a_refusal_surfaces_its_code_through_the_operation(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import commands

    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    with pytest.raises(ValueError, match="REASON_REQUIRED"):
        commands.op_manage_memory_file(
            vault, operation="reclassify", path=captured.path,
            source_kind="research-report",
        )


def test_an_unknown_operation_still_names_the_new_ones(vault: Path) -> None:
    from exomem import commands

    with pytest.raises(ValueError, match="reclassify"):
        commands.op_manage_memory_file(vault, operation="nonsense")


def test_both_operations_are_release_covered_selectors() -> None:
    """A new `operation=` value is invisible to the leaf tests above.

    Every other test here calls `op_manage_memory_file` directly, which is not
    the path a client takes. The shared dispatcher classifies the invocation
    first, and an unregistered selector is refused with `RECEIPT_OUTCOME_MISSING`
    before the leaf ever runs -- so the operation can be fully implemented,
    fully tested, and still unreachable from MCP, REST, hosted, and the CLI
    alike. That is how this shipped broken once; this binds the registration.
    """
    from exomem.commands import invocation_is_read_only
    from exomem.product_invoke import product_command

    command = product_command("manage_memory_file")

    assert invocation_is_read_only(command, {"operation": "propose-reclassification"}) is True
    assert invocation_is_read_only(command, {"operation": "reclassify"}) is False


def test_a_correction_that_registers_new_vocabulary_says_so(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A correction is where a typo is most likely, so registration must be visible.

    The caller is naming a kind it just decided on, with no prior capture to
    compare against. Registering it silently is how a mistyped kind becomes a
    permanent category, so the correction reports it the same way capture does.
    """
    captured = _capture(vault, source_schema, title="Hedgerow notes", domain="media")

    result = rc.reclassify(
        vault,
        path=captured.path,
        source_kind="field-notebook",
        reason="A running observational log is a field notebook.",
        today=TODAY,
    )

    assert any(
        warning.startswith("NEW_SOURCE_KIND: registered 'field-notebook'")
        for warning in result.warnings
    ), result.warnings
    assert all(isinstance(warning, str) for warning in result.warnings)
    assert result.as_dict()["warnings"] == list(result.warnings)


def test_a_correction_into_known_vocabulary_stays_quiet(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Hedgerow notes", domain="media")

    result = rc.reclassify(
        vault,
        path=captured.path,
        source_kind="research-report",
        reason="A written investigation.",
        today=TODAY,
    )

    assert result.warnings == ()
    assert "warnings" not in result.as_dict()


# ---------------------------------------------------------------------------
# The episode kind is reserved in both directions
# ---------------------------------------------------------------------------
def _recap(vault: Path, source_schema: schema_module.SourceSchema) -> add_module.AddResult:
    return _capture(
        vault,
        source_schema,
        title="Harbor Lamp purchase",
        source_type=st.EPISODE_KIND,
        slug="harbor-lamp-purchase-epa1a1a1a1a1a1-20260818t091233000000-dddddddd",
        content="### Worked on\n\n- Compared two lamps for Project Alpha",
        extra_frontmatter={
            "summary": "Chose the brass lamp.",
            "episode": "ep-" + "a1" * 16,
            "episode_digest": "d" * 64,
        },
    )


def test_a_capture_cannot_be_reclassified_into_the_episode_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A recap is only ever what `episode_memory` recorded and bound."""
    captured = _capture(
        vault, source_schema, title="Lamp report", source_type="research-report"
    )
    before = (vault / captured.path).read_bytes()

    for call in (
        lambda: rc.propose(vault, captured.path, source_kind=st.EPISODE_KIND),
        lambda: rc.reclassify(
            vault, path=captured.path, source_kind="episodes", reason="probe", today=TODAY
        ),
    ):
        with pytest.raises(rc.ReclassifyError) as error:
            call()
        assert error.value.code == "EPISODE_KIND_RESERVED"

    assert (vault / captured.path).read_bytes() == before
    assert not (vault / KB / "Sources" / "Episodes").exists()


@pytest.mark.parametrize(
    "change", [{"source_kind": "research-report"}, {"domain": "travel"}], ids=["kind", "domain"]
)
def test_a_recap_cannot_be_reclassified_out_of_the_episode_folder(
    vault: Path, source_schema: schema_module.SourceSchema, change: dict
) -> None:
    """Out of the kind, or into a domain subfolder: either way the recap leaves
    the one folder its revisions are found in."""
    recap = _recap(vault, source_schema)
    before = (vault / recap.path).read_bytes()

    with pytest.raises(rc.ReclassifyError) as proposed:
        rc.propose(vault, recap.path, **change)
    with pytest.raises(rc.ReclassifyError) as applied:
        rc.reclassify(vault, path=recap.path, reason="probe", today=TODAY, **change)

    assert proposed.value.code == applied.value.code == "EPISODE_KIND_RESERVED"
    assert (vault / recap.path).read_bytes() == before


# ---------------------------------------------------------------------------
# Draining legacy sources: append-only referrers, history and revert
# ---------------------------------------------------------------------------
def _legacy_source(vault: Path, name: str = "2026-01-02-tide-table") -> str:
    """A source filed under the retired catch-all, as the legacy drain finds it."""
    rel = f"{KB}/Sources/Other/{name}.md"
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: source\ntitle: Tide table\nsource_type: other\n"
        "captured: 2026-01-02T00:00:00Z\n"
        "# kept exactly as the capture wrote it\n"
        "tags: [ harbor,  tides ]\ningested_into: []\n---\n\n"
        "# Tide table\n\nHigh water 06:12, low water 12:31.\n",
        encoding="utf-8",
    )
    return rel


def _write(vault: Path, rel: str, text: str) -> Path:
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    return page


def test_a_legacy_source_is_previewed_moved_twice_and_reverted_through_the_operation(
    vault: Path,
) -> None:
    """The drain workflow, through the dispatcher every client uses.

    An Evidence page names the source by basename, alias and heading, which
    resolve unchanged after a move that keeps the basename, so it must neither
    be rewritten nor refuse the move. A mutable note names it by path and
    follows it there and back.
    """
    from exomem import commands, writer_lease

    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "manage_memory_file")
    source = _legacy_source(vault)
    stem = "2026-01-02-tide-table"
    original = (vault / source).read_text(encoding="utf-8")
    evidence = _write(
        vault,
        f"{KB}/Evidence/Harbor/tide-proof.md",
        f"---\ntype: evidence\n---\n\nTables: [[{stem}]], [[{stem}|the tides]], "
        f"[[{stem}#Tide table]].\n",
    )
    evidence_bytes = evidence.read_bytes()
    note = _write(
        vault,
        f"{KB}/Notes/Insights/tide-note.md",
        f"---\ntype: insight\nstatus: draft\n---\n\n# Tide note\n\n"
        f"See [[{source.removesuffix('.md')}]].\n",
    )
    note_bytes = note.read_bytes()

    preview = writer_lease.invoke_command(
        command, vault, operation="propose-reclassification", path=source,
        source_kind="dataset", domain="travel",
    )
    first = writer_lease.invoke_command(
        command, vault, operation="reclassify", path=source,
        source_kind="dataset", domain="travel", reason="a table of measurements",
    )
    second = writer_lease.invoke_command(
        command, vault, operation="reclassify", path=first["path"],
        source_kind="research-report", reason="the table is an appendix of a survey",
    )
    history = _front(vault, second["path"])["reclassified_from"]
    note_after_moves = note.read_text(encoding="utf-8")
    back_once = writer_lease.invoke_command(
        command, vault, operation="revert-reclassification", path=second["path"],
        reason="the survey attribution was wrong",
    )
    back_twice = writer_lease.invoke_command(
        command, vault, operation="revert-reclassification", path=back_once["path"],
        reason="keep it in the drain queue for now",
    )

    assert preview["destination"] == first["path"]
    assert preview["referrers"] == {
        "rewritten": [f"{KB}/Notes/Insights/tide-note.md"],
        "append_only_unchanged": {"basename": [f"{KB}/Evidence/Harbor/tide-proof.md"]},
    }
    assert preview["refusals"] == []
    assert history == [
        {"path": source, "kind": "other", "domain": None},
        {"path": first["path"], "kind": "dataset-export", "domain": "travel"},
    ]
    assert second["path"].removesuffix(".md") in note_after_moves
    assert back_once["path"] == first["path"]
    assert back_twice["path"] == source
    assert evidence.read_bytes() == evidence_bytes
    assert note.read_bytes() == note_bytes
    restored = (vault / source).read_text(encoding="utf-8")
    assert _body(vault, source) == original.split("---\n", 2)[2].removeprefix("\n")
    front_lines = restored.split("---\n", 2)[1].splitlines()
    # Only the correction record remains; every original byte is kept in place.
    assert [line for line in front_lines if not line.startswith("reclassified")] == (
        original.split("---\n", 2)[1].splitlines()
    )
    assert "reclassified_from" not in _front(vault, source)


def test_an_evidence_path_link_refuses_the_move_and_nothing_changes(vault: Path) -> None:
    """A path-form link in an append-only page would dangle, so the source stays."""
    source = _legacy_source(vault)
    evidence = _write(
        vault,
        f"{KB}/Evidence/Harbor/tide-proof.md",
        f"---\ntype: evidence\n---\n\nTable: [[{source.removesuffix('.md')}]].\n",
    )
    before = {rel: (vault / rel).read_bytes() for rel in (source, evidence.relative_to(vault).as_posix())}

    proposal = rc.propose(vault, source, source_kind="dataset", domain="travel")
    with pytest.raises(rc.ReclassifyError) as refused:
        rc.reclassify(
            vault, path=source, source_kind="dataset", domain="travel",
            reason="a table of measurements", today=TODAY,
        )

    assert [(item["code"], item["path"]) for item in proposal.refusals] == [
        ("APPEND_ONLY", f"{KB}/Evidence/Harbor/tide-proof.md")
    ]
    assert refused.value.code == "APPEND_ONLY"
    assert {rel: (vault / rel).read_bytes() for rel in before} == before
    assert not (vault / proposal.destination).exists()


def test_a_legacy_previous_path_is_kept_but_never_reverted_by_guessing(vault: Path) -> None:
    source = _legacy_source(vault)
    page = vault / source
    legacy_path = f"{KB}/Sources/Unclassified/2026-01-02-tide-table.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "ingested_into: []\n", f"ingested_into: []\nreclassified_from: {legacy_path}\n"
        ),
        encoding="utf-8",
    )
    before = page.read_bytes()

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.revert(vault, path=source, reason="undo", today=TODAY)
    assert refused.value.code == "HISTORY_CLASSIFICATION_UNKNOWN"
    assert page.read_bytes() == before

    moved = rc.reclassify(
        vault, path=source, source_kind="dataset", domain="travel",
        reason="a table of measurements", today=TODAY,
    )
    assert _front(vault, moved.new_path)["reclassified_from"] == [
        {"path": legacy_path},
        {"path": source, "kind": "other", "domain": None},
    ]
    reverted = rc.revert(vault, path=moved.new_path, reason="undo", today=TODAY)
    assert reverted.new_path == source
    assert _front(vault, source)["reclassified_from"] == [{"path": legacy_path}]


def test_a_revert_refuses_a_previous_location_another_page_now_occupies(
    vault: Path,
) -> None:
    source = _legacy_source(vault)
    moved = rc.reclassify(
        vault, path=source, source_kind="dataset", domain="travel",
        reason="a table of measurements", today=TODAY,
    )
    _write(vault, source, "---\ntype: source\ntitle: A newer capture\n---\n\nOther.\n")
    before = (vault / moved.new_path).read_bytes()

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.revert(vault, path=moved.new_path, reason="undo", today=TODAY)

    assert refused.value.code == "PREVIOUS_PATH_OCCUPIED"
    assert (vault / moved.new_path).read_bytes() == before


def test_a_reclassified_artifact_page_follows_its_bytes_and_keeps_its_other_fields(
    vault: Path,
) -> None:
    """The companion pointer is patched in place, not by re-serializing the page."""
    binary = f"{KB}/Sources/Other/scan.png"
    (vault / binary).parent.mkdir(parents=True, exist_ok=True)
    (vault / binary).write_bytes(b"\x89PNG\r\n\x1a\nbytes")
    page_rel = f"{binary}.md"
    original = (
        "---\ntype: source\ntitle: 'Harbour scan'\nsource_type: other\n"
        f"evidence_file: {binary}\n"
        "# scanned at the harbour office\n"
        "tags: [ harbor ]\n---\n\n# Harbour scan\n\nA scanned tide board.\n"
    )
    _write(vault, page_rel, original)

    result = rc.reclassify(
        vault, path=page_rel, source_kind="photograph", domain="travel",
        reason="it is a photograph of the tide board", today=TODAY,
    )

    new_binary = result.new_path.removesuffix(".md")
    assert (vault / new_binary).read_bytes() == b"\x89PNG\r\n\x1a\nbytes"
    assert _front(vault, result.new_path)["evidence_file"] == new_binary
    moved_lines = (vault / result.new_path).read_text(encoding="utf-8").splitlines()
    for line in original.splitlines():
        if not line.startswith(("source_type:", "evidence_file:")):
            assert line in moved_lines, line
