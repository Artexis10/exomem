"""Scenario-family registry: classification coverage, enforcement, doc no-drift."""

from __future__ import annotations

from pathlib import Path

import pytest

from membench import families
from membench.generate import GenerationError, generate_corpus
from membench.schema import ExpectedAnswer, ExpectedRecord
from membench.templates import registry
from membench.templates.base import Template

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "memory-proof-benchmark.md"

V01_ACTIVE_FAMILIES = {
    "temporal",
    "epistemics",
    "query_behavior",
    "maintenance",
    "identity",
    "multimodal",
    "governance",
}

V02_PLANNED_FAMILIES = {
    "procedural",
    "quantitative",
    "negation_counterfactual",
    "cross_lingual",
    "preference_attribution",
    "source_reliability",
    "long_horizon_entropy",
    "multimodal_depth",
}


def _probe_template(family: str) -> Template:
    """Minimal buildable template used to probe generation-time enforcement."""

    def build(ctx):  # type: ignore[no-untyped-def]
        entity = ctx.entity("concept", "science")
        source = ctx.source(
            2, f"{entity.canonical_name} note", lines=["A minor recorded fact."]
        )
        ctx.claim(entity, "noted_by", "field team", source)

        def expect(octx, query):  # type: ignore[no-untyped-def]
            return ExpectedRecord(
                query_id=query.query_id, answer=ExpectedAnswer(kind="none")
            )

        ctx.query("direct_recall", "Anything noted?", knowledge_week=4, expect=expect)

    return Template(
        template_id="t98_family_probe",
        family=family,
        summary="family enforcement probe",
        variants=1,
        build=build,
    )


# -- registry completeness -------------------------------------------------


def test_every_template_family_is_an_active_registry_entry() -> None:
    reg = families.registry()
    for template in registry().values():
        entry = reg.get(template.family)
        assert entry is not None, (
            f"template {template.template_id} names unregistered family "
            f"{template.family!r}"
        )
        assert entry.status == "active", (
            f"template {template.template_id} names non-active family "
            f"{template.family!r} ({entry.status})"
        )


def test_v01_families_are_active_deterministic_oracle() -> None:
    reg = families.registry()
    assert {t.family for t in registry().values()} == V01_ACTIVE_FAMILIES
    for family_id in sorted(V01_ACTIVE_FAMILIES):
        entry = reg[family_id]
        assert entry.classification == "deterministic-oracle"
        assert entry.status == "active"


def test_v02_planned_families_are_registered() -> None:
    reg = families.registry()
    for family_id in sorted(V02_PLANNED_FAMILIES):
        entry = reg.get(family_id)
        assert entry is not None, f"planned v0.2 family {family_id!r} not registered"
        assert entry.status == "planned"
        assert entry.classification != "out-of-scope"


def test_out_of_scope_entry_present_with_rationale() -> None:
    out_of_scope = [
        e for e in families.registry().values() if e.classification == "out-of-scope"
    ]
    assert out_of_scope, "registry declares no out-of-scope boundary entry"
    for entry in out_of_scope:
        assert entry.status != "active"
        assert len(entry.rationale.strip()) >= 20, (
            f"out-of-scope family {entry.family_id!r} lacks a stated rationale"
        )


def test_every_entry_has_a_single_line_table_safe_rationale() -> None:
    for entry in families.registry().values():
        assert entry.rationale.strip(), f"{entry.family_id}: empty rationale"
        assert "\n" not in entry.rationale, f"{entry.family_id}: multi-line rationale"
        assert "|" not in entry.rationale, f"{entry.family_id}: pipe breaks the doc table"


# -- generation-time enforcement -------------------------------------------


def test_unregistered_family_refuses_generation(tmp_path: Path) -> None:
    probe = _probe_template("bogus_family")
    with pytest.raises(GenerationError, match=r"t98_family_probe.*bogus_family"):
        generate_corpus(1, tmp_path / "corpus", templates={probe.template_id: probe})


def test_planned_family_refuses_generation(tmp_path: Path) -> None:
    probe = _probe_template("procedural")
    with pytest.raises(GenerationError, match=r"t98_family_probe.*procedural.*planned"):
        generate_corpus(1, tmp_path / "corpus", templates={probe.template_id: probe})


def test_active_family_still_generates(tmp_path: Path) -> None:
    probe = _probe_template("query_behavior")
    manifest = generate_corpus(
        1, tmp_path / "corpus", templates={probe.template_id: probe}
    )
    assert manifest.counts["queries"] == 1


# -- published coverage table (no-drift gate) ------------------------------


def test_doc_coverage_table_matches_registry() -> None:
    doc_text = DOC.read_text(encoding="utf-8")
    table = families.coverage_table_markdown()
    assert table in doc_text, (
        "docs/memory-proof-benchmark.md scenario-family table drifted from "
        "membench.families.coverage_table_markdown(); regenerate the doc section"
    )


# -- guard validation (the registry's own integrity checks) ----------------


def _family(**overrides):  # type: ignore[no-untyped-def]
    base = dict(
        family_id="probe_family",
        classification="deterministic-oracle",
        status="planned",
        rationale="probe rationale",
    )
    base.update(overrides)
    return families.Family(**base)


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ((_family(), _family()), "duplicate family id"),
        ((_family(rationale="  "),), "rationale is required"),
        ((_family(family_id=" "),), "family_id is required"),
        ((_family(classification="out-of-scope", status="active"),), "must coincide"),
        ((_family(status="excluded"),), "must coincide"),
        ((_family(classification="determinstic-oracle"),), "not in the taxonomy"),
        ((_family(status="activ"),), "not in the taxonomy"),
    ],
)
def test_validate_rejects_malformed_registry_entries(entries, match) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match=match):
        families._validate(entries)
