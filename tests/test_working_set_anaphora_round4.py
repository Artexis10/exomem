"""Correction cases from the round-4 time-word review (#1463)."""

from __future__ import annotations

import pytest
from anaphor_acceptance_sets import EARLIER, POSITIVES as ACCEPTANCE_POSITIVES
from anaphor_heldout_sets import EARLIER as HELDOUT_EARLIER, POSITIVES as HELDOUT_POSITIVES
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


# Round 5: authored before the grammar fix. The five reviewer examples are
# tuning cases now; the further variants exercise the same grammar classes.
ROUND5_REVIEW_NEGATIVES = (
    "is it the first of next month already",
    "is it tuesday yet where you are",
    "is it the end of next week now",
    "is it late in the evening there",
    "ok, that's enough, let's leave it there",
)
DUMMY_TIME_NEGATIVES = (
    "is it dark there yet",
    "it's nearly midnight",
    "it is the start of next year already",
    "isn't it half past ten there",
    "will it be the second of december tomorrow",
)
TIME_LINK_NEGATIVES = (
    "what about wednesday",
    "and how about sunday",
    "is that january",
    "is that midnight",
)
CLOSING_NEGATIVES = (
    "let's call it a day",
    "fine, drop it",
    "thanks, forget it",
    "ok, that's it",
    "never mind, that'll do it",
    "let's leave it",
    "that's enough, leave it there",
)
ROUND5_POSITIVES = (
    "will it be ready tomorrow",
    "is it still due friday",
    "how did her results compare with the spring round",
    "is it still late in the evening there for the review",
    "ok, let's drop it from the plan",
)
ROUND5_EARLIER = TIME_EARLIER + (
    {"role": "user", "text": "the migration review is tuesday, wednesday, sunday, january and midnight; the plan has a start in december at half past ten and ends after dark tomorrow"},
    {"role": "assistant", "text": "fine, thanks; it is nearly ready. never forget to call before you drop the plan for the day"},
)


@pytest.mark.parametrize(
    "turn",
    ROUND5_REVIEW_NEGATIVES + DUMMY_TIME_NEGATIVES + TIME_LINK_NEGATIVES + CLOSING_NEGATIVES,
)
def test_dummy_time_and_closing_turns_do_not_carry_even_with_shared_words(turn: str) -> None:
    assert _carried([turn], ROUND5_EARLIER) == []


@pytest.mark.parametrize("turn", ROUND5_POSITIVES)
def test_a_referential_it_or_comparison_keeps_its_subject(turn: str) -> None:
    assert _carried([turn], ROUND5_EARLIER) == [turn]


@pytest.mark.parametrize("weekday", ["wednesday", "saturday", "sunday"])
def test_a_shared_weekday_is_not_a_dummy_its_subject(weekday: str) -> None:
    earlier = ({"role": "user", "text": f"the migration plan is due {weekday}"},)
    assert _carried([f"is it {weekday} yet where you are"], earlier) == []


def test_a_time_word_cannot_link_through_a_shared_function_word_stem() -> None:
    earlier = ({"role": "user", "text": "the migration plan is even better now"},)
    assert _carried(["is that the evening"], earlier) == []


@pytest.mark.parametrize(
    "turn,earlier",
    [(turn, EARLIER) for turn in ACCEPTANCE_POSITIVES
     if turn not in {"does this affect the rota", "has anything changed there since"}]
    + [(turn, HELDOUT_EARLIER) for turn in HELDOUT_POSITIVES if turn != "how bad is it now"],
)
def test_every_previously_carried_acceptance_positive_keeps_its_subject(turn, earlier) -> None:
    assert _carried([turn], earlier) == [turn]
