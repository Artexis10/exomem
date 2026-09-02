"""9.2: the compat matrix — declared capability versus observed behaviour.

The manifest under test is written by the real runner and read back by the real
``load_manifest``; only the *declared* side varies between cases, because the
declared-versus-observed variant identity is the axis the spec makes a validity
question (benchmark-fairness-contract spec :61-71).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from unittest import mock

import pytest
from epistemic.projectors.exomem_vault import VaultProjector
from epistemic.schema import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "benchmarks" / "epistemic" / "fixtures"
VAULT = FIXTURES / "vault"
DATASET = Path("benchmarks/lme/fixtures/mini.json")


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory) -> Path:
    """One real protocol run: manifest, readiness and lifecycle evidence."""

    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    out = tmp_path_factory.mktemp("compat-run")
    with mock.patch.dict(
        os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}
    ):
        execute_run(
            RunConfig(
                dataset=DATASET,
                out=out,
                reader_name="stub",
                run_id="compat",
                provider="hybrid-rag-control",
            ),
            reader=StubReader(),
        )
    return out / "compat"


@pytest.fixture(scope="module")
def manifest(run_dir: Path):
    from protocol.manifest import load_manifest

    return load_manifest(run_dir)


@pytest.fixture(scope="module")
def packet():
    return load_scenario(FIXTURES / "scenario-minimal.yaml").fairness


def _redeclare_variant(fairness_packet, variant: str):
    """The same loader-produced packet, re-keyed onto one variant identity."""

    return fairness_packet.model_copy(
        update={
            "privileged_endpoint_matrix": tuple(
                entry.model_copy(update={"variant": variant})
                for entry in fairness_packet.privileged_endpoint_matrix
            )
        }
    )


def _build(fairness_packet, run_manifest):
    from benchmarks.reports.compat import build_compat_matrix

    return build_compat_matrix(
        packet=fairness_packet,
        manifest=run_manifest,
        declarations=VaultProjector(VAULT).declarations(),
    )


def test_declared_variant_equal_to_the_manifest_yields_no_finding(manifest, packet) -> None:
    """R3, the negative half."""

    matrix = _build(_redeclare_variant(packet, manifest.provider_variant), manifest)
    assert manifest.provider_variant
    assert matrix.cells
    assert matrix.divergences == ()
    assert matrix.run_status == "VALID"
    assert matrix.invalidates() is False


def test_declared_variant_differing_from_the_manifest_is_a_divergence(manifest, packet) -> None:
    """R3 / spec :68-71 — variant conflation is INVALID until corrected."""

    matrix = _build(packet, manifest)
    variant_findings = [item for item in matrix.divergences if item.kind == "variant"]
    assert matrix.invalidates() is True
    assert [item.key for item in variant_findings] == [
        ("fixture", "native", "corpus.ingest"),
        ("fixture", "native", "state.read"),
    ]
    for finding in variant_findings:
        assert finding.declared == "native"
        assert finding.observed == manifest.provider_variant
        assert finding.evidence_path == "manifest.json"


def test_every_declared_surface_becomes_a_cell(manifest, packet) -> None:
    matrix = _build(packet, manifest)
    assert tuple(cell.driver_surface_id for cell in matrix.cells) == (
        "corpus.ingest",
        "state.read",
    )
    for cell in matrix.cells:
        assert cell.observed_variant == manifest.provider_variant
        assert cell.declared_disposition == "equivalent"
        assert cell.observed_readiness_verified is True
    assert matrix.declared_field_statuses == tuple(
        sorted({item.status for item in VaultProjector(VAULT).declarations()})
    )


def test_an_equivalent_surface_without_verified_readiness_is_a_capability_finding(
    run_dir: Path, manifest, packet, tmp_path: Path
) -> None:
    from protocol.manifest import finalize_manifest, load_manifest
    from protocol.models import LaneReadiness

    unverified_dir = tmp_path / "unverified"
    shutil.copytree(run_dir, unverified_dir)
    finalize_manifest(
        unverified_dir,
        status="VALID",
        finalized_at="2026-01-02T00:00:00Z",
        readiness=[
            LaneReadiness(
                lane=manifest.provider_variant,
                requested=True,
                verified=False,
                method="config-state",
                evidence="readiness-unverifiable: dynamic extraction never terminalizes",
            )
        ],
    )
    unverified = load_manifest(unverified_dir)

    matrix = _build(_redeclare_variant(packet, unverified.provider_variant), unverified)
    capability_findings = [item for item in matrix.divergences if item.kind == "capability"]
    assert capability_findings
    assert "readiness-unverifiable" in capability_findings[0].observed
    # A capability finding is a finding, not a validity failure: only a variant
    # or provider identity mismatch makes the run INVALID (spec :61-71).
    assert matrix.invalidates() is False


def test_a_non_valid_run_carries_its_status_and_can_never_read_as_publishable(
    run_dir: Path, packet, tmp_path: Path
) -> None:
    """H2 / spec :26-30, :37-40 — a harness fault is not a comparable result."""

    from protocol.manifest import finalize_manifest, load_manifest

    from benchmarks.reports.compat import render_compat_matrix

    invalid_dir = tmp_path / "invalid"
    shutil.copytree(run_dir, invalid_dir)
    finalize_manifest(
        invalid_dir,
        status="INVALID",
        finalized_at="2026-01-03T00:00:00Z",
        invalid_reason="near-zero retrieval across the run's cases",
    )
    invalid = load_manifest(invalid_dir)
    assert invalid.status == "INVALID"

    matrix = _build(_redeclare_variant(packet, invalid.provider_variant), invalid)
    # The declared identity matches, so no divergence at all — and the matrix is
    # still not publishable, because the run itself is not VALID.
    assert matrix.divergences == ()
    assert matrix.run_status == "INVALID"
    assert matrix.invalid_reason == "near-zero retrieval across the run's cases"
    assert matrix.invalidates() is True

    rendered = render_compat_matrix(matrix)
    assert "INVALID" in rendered
    assert "near-zero retrieval across the run's cases" in rendered


def test_a_manifest_without_a_bound_observed_variant_refuses(manifest, packet) -> None:
    from benchmarks.reports.compat import CompatMatrixError

    with pytest.raises(CompatMatrixError, match="observed variant"):
        _build(packet, manifest.model_copy(update={"provider_variant": None}))


def test_an_empty_privileged_endpoint_matrix_refuses_by_name(manifest, packet) -> None:
    """F7: a named refusal, not a raw pydantic tuple-length error."""

    from benchmarks.reports.compat import CompatMatrixError

    empty = packet.model_copy(update={"privileged_endpoint_matrix": ()})
    with pytest.raises(CompatMatrixError, match="privileged endpoint matrix"):
        _build(empty, manifest)


def _design_variant_vocabulary() -> tuple[str, ...]:
    """Expand the registry list of design.md:43-52 out of the document itself."""

    design = (
        REPO_ROOT
        / "openspec"
        / "changes"
        / "add-competitive-benchmark-programme"
        / "design.md"
    ).read_text(encoding="utf-8")
    block = design.split("**Variants never collapse**", 1)[1]
    block = block.split("`no-memory`", 1)[0] + "`no-memory`"
    names: list[str] = []
    for token in re.findall(r"`([^`]+)`", block):
        braced = re.fullmatch(r"([a-z0-9-]+)-\{([^}]+)\}", token)
        if braced is not None:
            prefix, leaves = braced.groups()
            names.extend(f"{prefix}-{leaf.strip()}" for leaf in leaves.split(","))
        elif re.fullmatch(r"[a-z0-9-]+", token):
            names.append(token)
    return tuple(names)


def test_the_registered_variant_vocabulary_is_the_design_document_s_own() -> None:
    """I2: no new variant names are minted here."""

    from benchmarks.reports.compat import VARIANT_VOCABULARY, VARIANTS_BY_PROVIDER

    expected = _design_variant_vocabulary()
    assert len(expected) == 14
    assert VARIANT_VOCABULARY == frozenset(expected)
    # Every registered variant belongs to exactly one provider.
    flattened = [name for names in VARIANTS_BY_PROVIDER.values() for name in names]
    assert sorted(flattened) == sorted(expected)
    assert len(flattened) == len(set(flattened))


def test_both_variant_identities_are_disclosed_on_the_cell(manifest, packet) -> None:
    """F1 / spec :61-66 — 'in every artifact' covers the observed identity too."""

    from benchmarks.reports.compat import VARIANT_VOCABULARY

    # The real runner binds `hybrid-rag-fixture`, which is not registered.
    assert manifest.provider_variant not in VARIANT_VOCABULARY

    unregistered = _build(packet, manifest)
    assert all(cell.declared_variant_registered is False for cell in unregistered.cells)
    assert all(cell.observed_variant_registered is False for cell in unregistered.cells)

    registered = _build(_redeclare_variant(packet, "exomem-native"), manifest)
    assert all(cell.declared_variant_registered is True for cell in registered.cells)
    assert all(cell.observed_variant_registered is False for cell in registered.cells)

    observed_registered = manifest.model_copy(update={"provider_variant": "exomem-native"})
    both = _build(_redeclare_variant(packet, "exomem-native"), observed_registered)
    assert all(cell.observed_variant_registered is True for cell in both.cells)


def test_a_provider_differing_from_the_observed_variant_s_provider_is_a_divergence(
    manifest, packet
) -> None:
    """F2: the vocabulary names are provider-prefixed, so the provider compares."""

    observed = manifest.model_copy(update={"provider_variant": "exomem-native"})
    matrix = _build(_redeclare_variant(packet, "exomem-native"), observed)
    provider_findings = [item for item in matrix.divergences if item.kind == "provider"]
    assert provider_findings
    for finding in provider_findings:
        assert finding.declared == "fixture"
        assert finding.observed == "exomem"
    assert all(cell.observed_provider == "exomem" for cell in matrix.cells)
    assert matrix.invalidates() is True


def test_an_unregistered_observed_variant_discloses_that_provider_comparison_failed(
    manifest, packet
) -> None:
    """F2, the other half: say so rather than silently skipping the check."""

    from benchmarks.reports.compat import render_compat_matrix

    matrix = _build(packet, manifest)
    assert all(cell.observed_provider is None for cell in matrix.cells)
    assert [item for item in matrix.divergences if item.kind == "provider"] == []
    assert "provider comparison not possible" in render_compat_matrix(matrix)


def test_rendering_refuses_free_text_that_carries_an_aggregate(
    run_dir: Path, packet, tmp_path: Path
) -> None:
    """Integration fold: an invalid_reason is free text on its way to the page."""

    from protocol.manifest import finalize_manifest, load_manifest

    from benchmarks.reports.compat import render_compat_matrix
    from benchmarks.reports.guards import ReportRefused

    aggregate_dir = tmp_path / "aggregate"
    shutil.copytree(run_dir, aggregate_dir)
    finalize_manifest(
        aggregate_dir,
        status="INVALID",
        finalized_at="2026-01-04T00:00:00Z",
        invalid_reason="the aggregate MemScore was consumed as a result",
    )
    invalid = load_manifest(aggregate_dir)

    matrix = _build(_redeclare_variant(packet, invalid.provider_variant), invalid)
    with pytest.raises(ReportRefused, match="never publish an aggregate"):
        render_compat_matrix(matrix)
