"""9.2: the fairness matrix — the eight published fields, and the refusal.

Every field name here is read out of ``docs/benchmark-fairness-contract.md`` at
assert time rather than restated, so the row model cannot drift away from the
published contract without this file going red.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from epistemic.projectors.exomem_vault import VaultProjector
from epistemic.snapshot import FieldDeclaration
from protocol.models import LaneReadiness
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "docs" / "benchmark-fairness-contract.md"
VAULT = REPO_ROOT / "benchmarks" / "epistemic" / "fixtures" / "vault"

MATRIX_HEADING = "## Fairness-matrix row (rendered per lane × provider × variant)"


def _doc_field_labels() -> tuple[str, ...]:
    """The row labels of the contract's fairness-matrix table, in file order."""

    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    start = lines.index(MATRIX_HEADING)
    labels: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = re.match(r"^\|\s*([^|]+?)\s*\|", line)
        if match is None:
            continue
        label = match.group(1)
        if label == "Field" or set(label) <= {"-", ":"}:
            continue
        labels.append(label)
    return tuple(labels)


def _glue():
    from benchmarks.reports.fairness import GlueAccounting

    return GlueAccounting.for_projector(
        VaultProjector(VAULT), repo_root=REPO_ROOT, reviewer_disposition="unreviewed"
    )


def _readiness() -> tuple[LaneReadiness, ...]:
    return (
        LaneReadiness(
            lane="exomem",
            requested=True,
            verified=True,
            method="config-state",
            evidence="evidence/readiness/exomem.json",
        ),
    )


def _payload(**changes):
    from benchmarks.reports.fairness import (
        Asymmetry,
        BlockedMeasurement,
        CapabilityDeclarationCounts,
    )

    payload = {
        "lane": "epistemic",
        "provider": "exomem",
        "variant": "exomem-native",
        "config_source": {"vault_root": "benchmarks/epistemic/PREREGISTRATION.md:16"},
        "config_authored_by": {"vault_root": "exomem"},
        "exomem_authored_glue": _glue(),
        "asymmetries": (
            Asymmetry(
                description="the vault projector reads the filesystem directly",
                favours="exomem",
                evidence="benchmarks/epistemic/projectors/exomem_vault.py:405",
            ),
        ),
        "capability_declarations": CapabilityDeclarationCounts.from_declarations(
            VaultProjector(VAULT).declarations()
        ),
        "readiness": _readiness(),
        "pins": {"exomem": "0f239f0317df83ac77b65f734812bf41f4967251"},
        "blocked_measurements": (
            BlockedMeasurement(
                measurement="cross-provider latency",
                reason="host is known-unvalidated for latency",
                unblock_command="uv run python benchmarks/run.py latency --host-validated",
            ),
        ),
    }
    payload.update(changes)
    return payload


def _row(**changes):
    from benchmarks.reports.fairness import FairnessRow

    return FairnessRow(**_payload(**changes))


def test_the_eight_published_fields_are_the_contract_document_s_own() -> None:
    """I1: the field names are copied from the doc, never paraphrased."""

    from benchmarks.reports.fairness import (
        EMPTY_DISCLOSURE_REASONS,
        FAIRNESS_MATRIX_FIELDS,
        FairnessRow,
    )

    labels = _doc_field_labels()
    assert len(labels) == 8
    assert tuple(label for label, _ in FAIRNESS_MATRIX_FIELDS) == labels

    attributes = tuple(attribute for _, attribute in FAIRNESS_MATRIX_FIELDS)
    fields = tuple(FairnessRow.model_fields)
    assert fields[:3] == ("lane", "provider", "variant")
    assert fields[3:11] == attributes
    # The disclosure companions come after the eight and never displace them: an
    # empty published field still has to state a claim rather than say nothing.
    assert fields[11:] == tuple(reason for _, reason in EMPTY_DISCLOSURE_REASONS)


def test_a_complete_row_carries_the_glue_accounting_the_projector_published() -> None:
    row = _row()
    meta = VaultProjector(VAULT).meta()
    assert row.key == ("epistemic", "exomem", "exomem-native")
    assert row.exomem_authored_glue.projector.loc == meta.loc
    assert row.exomem_authored_glue.projector.endpoints_used == meta.endpoints_used
    assert meta.loc > 0


def test_glue_accounting_binds_its_loc_to_the_disclosed_files() -> None:
    """F4: the LOC number must be about a file the row actually discloses."""

    from benchmarks.reports.fairness import GlueAccounting

    projector = VaultProjector(VAULT)
    glue = _glue()
    assert glue.projector_module == "benchmarks/epistemic/projectors/exomem_vault.py"
    assert glue.projector_module in glue.files
    assert glue.projector.loc == projector.meta().loc

    with pytest.raises(ValidationError, match="projector module"):
        GlueAccounting(
            files=("benchmarks/reports/fairness.py",),
            projector=projector.meta(),
            projector_module="benchmarks/epistemic/projectors/exomem_vault.py",
            reviewer_disposition="unreviewed",
        )


@pytest.mark.parametrize(
    "field",
    [
        "config_source",
        "config_authored_by",
        "exomem_authored_glue",
        "asymmetries",
        "capability_declarations",
        "readiness",
        "pins",
        "blocked_measurements",
    ],
)
def test_a_row_missing_any_published_field_is_a_validation_error(field: str) -> None:
    """R2: all eight are required; none of them has a silent default."""

    from benchmarks.reports.fairness import FairnessRow

    payload = _payload()
    payload.pop(field)
    with pytest.raises(ValidationError) as excinfo:
        FairnessRow(**payload)
    assert field in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "reason", "empty"),
    [
        ("config_source", "no_config_source_reason", {}),
        ("asymmetries", "no_asymmetries_reason", ()),
        ("pins", "no_pins_reason", {}),
        ("blocked_measurements", "no_blocked_measurements_reason", ()),
    ],
)
def test_an_empty_published_field_must_state_why(field: str, reason: str, empty) -> None:
    """F3: 'none declared' is a claim someone makes, never a silent default."""

    from benchmarks.reports.fairness import FairnessRow

    emptied = {field: empty}
    if field == "config_source":
        emptied["config_authored_by"] = {}

    with pytest.raises(ValidationError, match=reason):
        FairnessRow(**_payload(**emptied))

    stated = _row(**{**emptied, reason: "this control row applies no competitor-side setting"})
    assert getattr(stated, reason)

    with pytest.raises(ValidationError, match="cannot both"):
        FairnessRow(**_payload(**{reason: "this control row applies no setting"}))


def test_capability_declaration_counts_come_from_real_declarations() -> None:
    from benchmarks.reports.fairness import CapabilityDeclarationCounts

    declarations = (
        FieldDeclaration(field="a", status="declared", evidence="https://example.invalid/a"),
        FieldDeclaration(
            field="b", status="absent_by_design", evidence="https://example.invalid/b"
        ),
        FieldDeclaration(
            field="c",
            status="absent_by_design",
            evidence="https://example.invalid/c",
            marketing_claim="the product markets this",
        ),
        FieldDeclaration(field="d", status="unavailable", evidence="https://example.invalid/d"),
    )
    counts = CapabilityDeclarationCounts.from_declarations(declarations)
    # `not_applicable` comes only from an unclaimed `absent_by_design`
    # declaration — a claimed one scores `fail` (benchmarks/epistemic/
    # assertions.py:427-441), so the two counts are deliberately not equal.
    assert counts.absent_by_design == 2
    assert counts.not_applicable == 1
    assert counts.unavailable == 1


def test_a_config_source_that_is_neither_a_citation_nor_a_url_refuses() -> None:
    from benchmarks.reports.fairness import FairnessRow

    with pytest.raises(ValidationError, match="path:line"):
        FairnessRow(**_payload(config_source={"vault_root": "we asked them"}))


def test_config_authorship_must_cover_exactly_the_sourced_settings() -> None:
    from benchmarks.reports.fairness import FairnessRow

    with pytest.raises(ValidationError, match="authorship"):
        FairnessRow(**_payload(config_authored_by={"other_knob": "competitor"}))


def test_a_row_may_not_carry_an_absolute_local_path(tmp_path: Path) -> None:
    """D4: rendered artifacts carry paths relative to the run root.

    The absolute path is produced at runtime rather than written as a literal:
    an absolute local path spelled out in a committed test is itself what
    ``scripts/validate-public-artifacts.py`` refuses.
    """

    from benchmarks.reports.fairness import GlueAccounting

    absolute = str(tmp_path / "benchmarks" / "epistemic" / "projectors" / "exomem_vault.py")
    assert Path(absolute).is_absolute()
    with pytest.raises(ValidationError, match="relative"):
        GlueAccounting(
            files=(absolute,),
            projector=VaultProjector(VAULT).meta(),
            projector_module=absolute,
            reviewer_disposition="unreviewed",
        )


def test_a_row_may_not_escape_the_run_root_by_traversal() -> None:
    """F6: the `..` half of the path guard, which no earlier mutant reached."""

    from benchmarks.reports.fairness import GlueAccounting

    escape = "../escape.md"
    with pytest.raises(ValidationError, match="traversal"):
        GlueAccounting(
            files=(escape,),
            projector=VaultProjector(VAULT).meta(),
            projector_module=escape,
            reviewer_disposition="unreviewed",
        )


def test_missing_fairness_entry_blocks_publication() -> None:
    """R1 / spec :22-24 — the named key is in the refusal."""

    from benchmarks.reports.fairness import FairnessMatrixError, require_complete

    rows = (_row(),)
    expected = (
        ("epistemic", "exomem", "exomem-native"),
        ("epistemic", "basic-memory", "basic-memory-native-git"),
    )
    with pytest.raises(FairnessMatrixError) as excinfo:
        require_complete(rows, expected)
    message = str(excinfo.value)
    assert "missing fairness entry blocks publication" in message
    assert "basic-memory-native-git" in message


def test_require_complete_passes_when_every_expected_key_has_a_row() -> None:
    from benchmarks.reports.fairness import require_complete

    rows = (_row(),)
    require_complete(rows, (("epistemic", "exomem", "exomem-native"),))


def test_rendering_itself_refuses_an_incomplete_matrix() -> None:
    """F5 / spec :22-24 — it is *rendering* that refuses, not only a helper."""

    from benchmarks.reports.fairness import FairnessMatrixError, render_fairness_matrix

    with pytest.raises(FairnessMatrixError, match="missing fairness entry blocks publication"):
        render_fairness_matrix(
            (_row(),),
            expected_keys=(
                ("epistemic", "exomem", "exomem-native"),
                ("epistemic", "basic-memory", "basic-memory-native-git"),
            ),
        )


def test_rendered_matrix_publishes_every_contract_field_and_one_row_per_key() -> None:
    from benchmarks.reports.fairness import render_fairness_matrix

    rows = (_row(), _row(provider="basic-memory", variant="basic-memory-native-git"))
    rendered = render_fairness_matrix(rows, expected_keys=tuple(row.key for row in rows))
    header = rendered.splitlines()[0]
    for label in _doc_field_labels():
        assert f"| {label} |" in header
    body = [line for line in rendered.splitlines() if line.startswith("| epistemic |")]
    assert len(body) == 2
    assert "exomem-native" in rendered
    assert "basic-memory-native-git" in rendered
