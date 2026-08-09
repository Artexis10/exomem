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
    names = parse_preregistered_assertions(PREREGISTRATION.read_text(encoding="utf-8"))
    assert len(names) == 18
    assert len(set(names)) == 18
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


def test_catastrophic_set_matches_preregistration_section_three() -> None:
    text = PREREGISTRATION.read_text(encoding="utf-8")
    marker = "## 3. Catastrophic set"
    start = text.index(marker)
    end = text.index("## 4.", start)
    section = text[start:end]
    for name in CATASTROPHIC_ASSERTIONS:
        assert f"`{name}`" in section, name
    assert CATASTROPHIC_ASSERTIONS <= set(ASSERTION_REGISTRY)
    assert len(CATASTROPHIC_ASSERTIONS) == 6
