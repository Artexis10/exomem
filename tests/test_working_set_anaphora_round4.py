"""Correction cases from the round-4 time-word review (#1463)."""

from __future__ import annotations

import pytest
from anaphor_acceptance_sets import EARLIER
from test_working_set_conversation_carry import _carried

TIME_NEGATIVES = (
    "is it the weekend yet",
    "is it already the evening there",
    "is it the end of the month",
    "is it a holiday on monday",
    "what day is it",
    "is it monday",
    "is it the summer yet",
    "is it in the morning or the evening",
    "is it after the spring equinox",
    "this is the best time of year",
    "that was a long week",
)
TIME_POSITIVES = (
    "will it be ready tomorrow",
    "is that done today",
    "did it happen yesterday",
    "is it still due friday",
    "can priya do saturday",
    "is it done by friday",
    "was that last week",
    "is that finished yet",
    "can it wait until monday",
    "did they reply this morning",
)
TIME_EARLIER = (
    {"role": "user", "text": "can you pull up the nas migration plan, i want to move the photos off the old synology"},
    {"role": "assistant", "text": "sure. the plan has three steps: copy the photo library, verify checksums, then retire the old box. step one is half done."},
    {"role": "user", "text": "ok. also the volunteer rota for the food bank still has gaps on saturdays"},
    {"role": "assistant", "text": "two saturday slots are open; priya offered to cover one."},
    {"role": "user", "text": "right, and remind me what the electrician quoted for the garage"},
)


# Recall is reported, not gated: no pointing word in the first case, and
# "wait" remains content under the frozen task vocabulary in the second.
TIME_MISSES = {"can priya do saturday", "can it wait until monday"}


@pytest.mark.parametrize("turn", TIME_NEGATIVES)
def test_a_time_subject_does_not_carry(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER) == []


@pytest.mark.parametrize("turn", [turn for turn in TIME_POSITIVES if turn not in TIME_MISSES])
def test_a_follow_up_with_a_time_modifier_carries(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER) == [turn]


@pytest.mark.parametrize("word", ["figure", "figures", "number", "numbers"])
def test_task_word_forms_match_the_same_generic_vocabulary(word: str) -> None:
    turn = f"is that the final {word}"
    assert _carried([turn], EARLIER) == [turn]


@pytest.mark.parametrize(
    "turn",
    [
        "is it on track for the autumn",
        "is it ready in the morning",
        "how did her results compare with the spring round",
    ],
)
def test_a_preposition_can_place_an_article_time_phrase(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER) == [turn]


@pytest.mark.parametrize("turn", sorted(TIME_MISSES))
def test_time_word_cases_with_other_missing_evidence_still_abstain(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER) == []
