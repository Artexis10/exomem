"""The assertion registry is frozen by the pre-registration, not by the code.

Drift here is a pre-registration violation, so the test parses
``benchmarks/epistemic/PREREGISTRATION.md`` rather than trusting a mirrored
list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from epistemic.catastrophic import CATASTROPHIC_ASSERTIONS
from epistemic.registry import (
    ASSERTION_REGISTRY,
    PREREGISTERED_ASSERTIONS,
    RegistryError,
    parse_preregistered_assertions,
    resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION = REPO_ROOT / "benchmarks" / "epistemic" / "PREREGISTRATION.md"


def test_registry_keys_equal_the_preregistered_section_two_list() -> None:
    """18 ratified names plus the 6 the 2026-08 §7 amendment added."""

    names = parse_preregistered_assertions(PREREGISTRATION.read_text(encoding="utf-8"))
    assert len(names) == 24
    assert len(set(names)) == 24
    assert set(ASSERTION_REGISTRY) == set(names)
    assert set(PREREGISTERED_ASSERTIONS) == set(names)


def test_registry_has_no_additions_beyond_the_frozen_list() -> None:
    assert tuple(sorted(ASSERTION_REGISTRY)) == tuple(sorted(PREREGISTERED_ASSERTIONS))


def test_every_registered_name_resolves_to_a_callable() -> None:
    for name in PREREGISTERED_ASSERTIONS:
        assert callable(resolve(name))


def test_unknown_assertion_name_raises_registry_error() -> None:
    with pytest.raises(RegistryError) as excinfo:
        resolve("no_such_assertion")
    assert "no_such_assertion" in str(excinfo.value)


#: The six §3 names the ratified document froze.  §3 is byte-pinned by the
#: receipt chain and can never be edited, so a member added by an amendment is
#: authorised by §7 plus an acknowledged receipt instead — never by rewriting §3.
RATIFIED_CATASTROPHIC = frozenset(
    {
        "no_retired_state_served_as_current",
        "prior_revision_retained",
        "evidence_path_resolves",
        "contradiction_not_flattened",
        "no_cross_case_residue",
        "external_edit_authoritative_within",
    }
)
#: Added by the 2026-08 loop-closure amendment, whose §7 entry states the
#: candidacy and defers the decision to the founder's acknowledgment.
AMENDED_CATASTROPHIC = frozenset({"refuted_retrievable_at_full_standing"})


def test_catastrophic_set_matches_preregistration_section_three() -> None:
    """Every ratified member is named in §3, and §3 gained nothing."""

    text = PREREGISTRATION.read_text(encoding="utf-8")
    marker = "## 3. Catastrophic set"
    start = text.index(marker)
    end = text.index("## 4.", start)
    section = text[start:end]
    for name in RATIFIED_CATASTROPHIC:
        assert f"`{name}`" in section, name
    # §3's prose is frozen at six.  The document cannot be edited to record the
    # seventh without breaking the sha256 the amendment receipt pins, which is
    # exactly why the amendment mechanism exists.
    assert len(RATIFIED_CATASTROPHIC) == 6
    for name in AMENDED_CATASTROPHIC:
        assert f"`{name}`" not in section, name
    assert CATASTROPHIC_ASSERTIONS <= set(ASSERTION_REGISTRY)
    assert CATASTROPHIC_ASSERTIONS == RATIFIED_CATASTROPHIC | AMENDED_CATASTROPHIC
    assert len(CATASTROPHIC_ASSERTIONS) == 7


def test_amended_catastrophic_member_is_authorised_by_section_seven_and_a_receipt() -> None:
    """The seventh member's authority, traced to the two artifacts that grant it.

    §3 cannot name it — the document is byte-pinned.  So the chain of authority
    is: §7 states the candidacy and reserves the decision to the founder, and the
    acknowledged receipt records ``accept``.  If either half were missing, the
    code would be widening the frozen catastrophic set on its own.
    """

    import json

    text = PREREGISTRATION.read_text(encoding="utf-8")
    section_seven = text[text.index("## 7. Amendments") :]
    for name in AMENDED_CATASTROPHIC:
        assert f"`{name}`" in section_seven, name
    assert "Catastrophic-set proposal:" in section_seven
    assert "the founder must explicitly record `accept` or `strike`" in section_seven

    receipt = json.loads(
        (
            REPO_ROOT
            / "benchmarks/epistemic/contracts/amendment-2026-08-loop-closure.v1.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["catastrophic_set_decision"] == "accept"
    assert receipt["ratifier"] is not None
    assert receipt["acknowledged_on"] == "2026-08-15"


# --------------------------------------------------------------------------
# Correction round.
# --------------------------------------------------------------------------


def test_m3_preregistration_records_the_evidence_path_amendment() -> None:
    """M3: the sharpened vacuity rule lives in §7, dated and reasoned."""

    text = PREREGISTRATION.read_text(encoding="utf-8")
    start = text.index("## 7. Amendments")
    section = text[start:]
    assert "(none)" not in section
    assert "2026-08-09" in section
    assert "`evidence_path_resolves` semantics sharpened pre-ratification" in section
    assert "vacuity fails" in section
    assert "every aggregate suppressed" in section
    assert "`evidence_path_exists` remains the non-catastrophic co-assertion" in section


def test_family_registry_matches_preregistration_section_one() -> None:
    """§1 drift test, mirroring the §2 assertion drift test.

    14 ratified families plus f15-f19 from the 2026-08 §7 amendment. Being in
    this table is registration, not release: the two are separate, and the gate
    that keeps them separate lives in ``test_epistemic_amendment_governance.py``.
    It withheld f15-f19 until the founder acknowledged the receipt on
    2026-08-15, and would withhold the next amendment's families the same way.
    """

    from epistemic.registry import PREREGISTERED_FAMILIES, parse_preregistered_families

    parsed = parse_preregistered_families(PREREGISTRATION.read_text(encoding="utf-8"))
    assert len(parsed) == 19
    assert [family_id for family_id, _name in parsed] == [f"f{n:02d}" for n in range(1, 20)]
    assert PREREGISTERED_FAMILIES == parsed


def test_family_ids_are_exposed_for_load_time_validation() -> None:
    from epistemic.registry import PREREGISTERED_FAMILY_IDS

    assert PREREGISTERED_FAMILY_IDS == frozenset(f"f{n:02d}" for n in range(1, 20))


def test_amendment_introduced_families_are_a_subset_of_the_registered_table() -> None:
    """The withheld set can only ever name families the document registers."""

    from epistemic.registry import (
        AMENDMENT_INTRODUCED_FAMILIES,
        PREREGISTERED_FAMILY_IDS,
    )

    assert set(AMENDMENT_INTRODUCED_FAMILIES) <= PREREGISTERED_FAMILY_IDS
    assert set(AMENDMENT_INTRODUCED_FAMILIES) == {f"f{n:02d}" for n in range(15, 20)}
