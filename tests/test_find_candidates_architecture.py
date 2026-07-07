"""Architecture checks for the find candidate-lane split."""

from __future__ import annotations

from exomem import find, find_candidates


def test_find_uses_candidate_collapse_helper() -> None:
    assert hasattr(find_candidates, "CandidateBundle")
    assert hasattr(find_candidates, "collect_candidates")
    assert find._collapse_frame_children.__name__ == "_collapse_frame_children"
