"""Architecture checks for the find result-helper split."""

from __future__ import annotations

from exomem import find, find_results


def test_find_reexports_result_helpers() -> None:
    assert find.EXCERPT_MAX_LEN == find_results.EXCERPT_MAX_LEN
    assert find._make_excerpt is find_results.make_excerpt
    assert find._semantic_excerpt is find_results.semantic_excerpt
    assert find._stem_tokens_present is find_results.stem_tokens_present
    assert find._transcript_ts_for_hit is find_results.transcript_ts_for_hit
