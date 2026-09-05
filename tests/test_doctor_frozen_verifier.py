"""Packaging and diagnostics for the frozen stance verifier tier.

The tier is optional and default-off, so its ABSENCE is the normal state:
doctor must be able to say what it is doing without ever failing warm, and the
retired `EXOMEM_CLAIM_NLI_MODEL` knob must be visible as ignored rather than
silently inert.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from exomem import claims, doctor

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture(autouse=True)
def _reset_verifier_state():
    claims.reset_verifier_cache()
    yield
    claims.reset_verifier_cache()


def _extras() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


def test_nli_is_a_default_off_optional_extra() -> None:
    extras = _extras()
    assert "nli" in extras
    joined = " ".join(extras["nli"])
    assert "sentence-transformers" in joined
    sentence_transformers = next(
        Requirement(requirement)
        for requirement in extras["nli"]
        if Requirement(requirement).name == "sentence-transformers"
    )
    assert Version("2.7.0") not in sentence_transformers.specifier
    assert Version("3.0.0") in sentence_transformers.specifier
    # Default-off: it is in no profile's extra set, so no install path pulls it in.
    for profile in doctor.VALID_PROFILES:
        assert "nli" not in doctor._profile_extras(profile)


def test_uninstalled_extra_is_invisible_apart_from_the_diagnostic_surface(
    tmp_path, monkeypatch
) -> None:
    """Byte-identical degradation: with the extra absent and the gate off, the
    verifier tier reaches nothing but doctor."""
    from exomem import audit as audit_module

    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    import importlib.util

    assert importlib.util.find_spec("sentence_transformers") is None, (
        "this test documents the extra-absent lane; it is meaningless with `nli` installed"
    )
    assert claims.verifier_polarity("a claim", "another claim") is None

    finding = audit_module.AuditFinding(
        category="corpus_contradictions",
        severity="info",
        path="Knowledge Base/Notes/Insights/a.md",
        detail="Active conclusion overlaps active conclusion.",
        paths=["Knowledge Base/Notes/Insights/a.md", "Knowledge Base/Notes/Insights/b.md"],
        meta={"signal_version": "abc", "provenance": "proximity"},
    )
    before = dict(finding.meta), finding.detail
    audit_module._enrich_contradiction_polarity(tmp_path, [finding])
    assert (dict(finding.meta), finding.detail) == before

    assert doctor._check_frozen_verifier().id == "verifier.frozen_stance"


def test_doctor_reports_the_absent_tier_without_failing_warm(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    monkeypatch.delenv("EXOMEM_CLAIM_NLI_MODEL", raising=False)

    check = doctor._check_frozen_verifier()
    assert check.id == "verifier.frozen_stance"
    assert check.status == "pass"
    assert "EXOMEM_CLAIM_POLARITY_NLI" in check.message
    assert check.details["reason"] == "gate-off"


def test_doctor_names_an_ignored_retired_model_knob(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    monkeypatch.setenv("EXOMEM_CLAIM_NLI_MODEL", "somebody/some-cross-encoder")

    check = doctor._check_frozen_verifier()
    assert check.status == "warn"
    assert "EXOMEM_CLAIM_NLI_MODEL" in check.message
    assert "somebody/some-cross-encoder" in check.message
    assert check.details["ignored_model_env"] == "somebody/some-cross-encoder"
    assert check.remediation


def test_doctor_reports_a_refusal_with_its_reason_and_still_does_not_fail(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setattr(claims, "VERIFIER_PINS", ())

    check = doctor._check_frozen_verifier()
    assert check.status == "warn"
    assert "no-pin" in check.message
    assert check.details["reason"] == "no-pin"


def test_the_verifier_tier_never_reports_fail(monkeypatch) -> None:
    for gate, pins in (("", ()), ("1", ()), ("1", claims.VERIFIER_PINS)):
        if gate:
            monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", gate)
        else:
            monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
        monkeypatch.setattr(claims, "VERIFIER_PINS", pins)
        claims.reset_verifier_cache()
        assert doctor._check_frozen_verifier().status != "fail"


def test_the_check_is_wired_into_the_doctor_report(vault: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    report = doctor.doctor(vault=str(vault), profile="lean")
    assert "verifier.frozen_stance" in {check.id for check in report.checks}
