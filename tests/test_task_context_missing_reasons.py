"""A task context that reports one gap keeps it when a later stage reports another."""

from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_context_catalogue import _built, _page
from test_governance_egress import _external
from test_working_set_withheld_twins import _withhold

from exomem import lexstore


def test_a_discovery_gap_survives_a_later_truncation(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client told that one source could not be released must not lose that after another is cut for room."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # The excerpt-only page cannot be released whole, so discovery reports it as a failed lane.
    excerpt = _page(vault, "measurement-reference", body="- [technique] Inspect optical alignment ^excerpt\n\n" + "Neutral observation. " * 60)
    for name in ("alignment-method-a", "alignment-method-b", "alignment-method-c"):
        _page(vault, name, body=f"- [technique] Inspect optical alignment for {name} ^alignment\n\nExclude handheld field checks.\n")
    _withhold(vault, excerpt, ceiling=5)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    roomy = _activate(vault, _external(), turn="Inspect the optical alignment", max_chars=8000)
    assert [gap["reason"] for gap in roomy["task_context"]["missing"]] == ["lane_failed"]
    crowded = _activate(vault, _external(), turn="Inspect the optical alignment", max_chars=1300)
    reasons = [gap["reason"] for gap in crowded["task_context"]["missing"]]
    assert sorted(reasons) == ["lane_failed", "lane_truncated"]
