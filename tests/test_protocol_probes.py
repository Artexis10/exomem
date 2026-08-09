from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        (["current"], "superseded"),
        (["current", "stale"], "both_returned"),
        (["stale"], "stale_only"),
        ([], "unresolvable"),
    ],
)
def test_update_outcomes(hits: list[str], expected: str) -> None:
    from protocol.probes import classify_update_outcome

    assert classify_update_outcome(hits) == expected


def test_probe_specs_include_inconclusive_by_design() -> None:
    from protocol.probes import known_answer_probe_specs

    specs = known_answer_probe_specs()
    assert {spec.kind for spec in specs} == {"lexical-rare-token", "semantic-zero-overlap", "update-current-state"}
    assert "inconclusive-by-design" in specs[0].allowed_outcomes
