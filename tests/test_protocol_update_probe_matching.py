"""The update probe must survive a provider that renders what it stored.

Exact equality assumed a provider echoes stored content verbatim. Exomem
returns a rendered note — YAML frontmatter and all — so the marker is present
but never equal, and the probe scored `unresolvable` on a store that had in
fact superseded correctly. That invalidated every direct run before a single
case executed.

Markers are opaque tokens, so containment cannot collide by accident. A
structured label is still matched exactly: there the provider names the record,
and a loose match could call a different record current.
"""

from __future__ import annotations

import pytest

OLD = "revision-old-opaque"
CURRENT = "revision-current-opaque"


def _classify(hits):
    from protocol.probes import classify_update_outcome

    return classify_update_outcome(hits, old_marker=OLD, current_marker=CURRENT)


def test_a_rendered_note_containing_only_the_current_marker_is_superseded() -> None:
    rendered = (
        "---\ntype: source\nexomem_id: feb07ccf\ntitle: LongMemEval case 3 session 1\n"
        f"---\n\n## user\n\n{CURRENT}\n"
    )
    assert _classify([rendered]) == "superseded"


def test_a_bare_marker_still_matches() -> None:
    """The pre-existing exact-equality case must keep working."""

    assert _classify([CURRENT]) == "superseded"
    assert _classify([OLD]) == "stale_only"
    assert _classify([OLD, CURRENT]) == "both_returned"


def test_rendered_notes_carrying_both_markers_are_both_returned() -> None:
    assert _classify([f"# note\n\n{OLD}\n", f"# note\n\n{CURRENT}\n"]) == "both_returned"


def test_text_with_neither_marker_is_still_unresolvable() -> None:
    assert _classify(["---\ntype: source\n---\n\nnothing relevant here\n"]) == "unresolvable"
    assert _classify([]) == "unresolvable"


def test_a_structured_label_is_matched_exactly_not_by_containment() -> None:
    """A provider that names records must not have a different record read as current."""

    assert _classify([{"record_id": CURRENT}]) == "superseded"
    # A longer id merely *containing* the marker is a different record.
    assert _classify([{"record_id": f"{CURRENT}-v2"}]) == "unresolvable"
    assert _classify([{"state": OLD}]) == "stale_only"


@pytest.mark.parametrize("marker_field", ["record_id", "state", "revision", "kind"])
def test_every_structured_label_field_keeps_exact_matching(marker_field: str) -> None:
    assert _classify([{marker_field: CURRENT}]) == "superseded"
    assert _classify([{marker_field: f"prefix-{CURRENT}"}]) == "unresolvable"


def test_the_probe_matches_what_the_exomem_direct_row_actually_returns() -> None:
    """Regression for the run-blocking shape, verbatim from a real probe."""

    observed = (
        "---\ntype: source\nexomem_id: 84ad402c-a976-426e-8c4c-95f7c7ff84b2\n"
        "title: LongMemEval case 3 session 1\nsource_type: session\n"
        "captured: 2026-01-01T00:00:00Z\ntags: []\n---\n\n"
        f"## user\n\n{CURRENT}\n"
    )
    assert _classify([observed]) == "superseded"
