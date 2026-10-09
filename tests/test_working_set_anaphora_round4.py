"""Correction cases from the round-4 time-word review (#1463)."""

from __future__ import annotations

import pytest
from anaphor_acceptance_sets import EARLIER
from anaphor_round6_sets import (
    FIFTH_NEGATIVES,
    FIFTH_POSITIVES,
    LICENCE_NEGATIVES,
    STRUCTURAL_NEGATIVES,
    STRUCTURAL_POSITIVES,
    earlier as repeated_earlier,
)
from test_working_set_conversation_carry import _carried, analyze
from anaphor_round7_sets import (
    CODE_TURNS, CONTENT_FREE_FIFTH_IDS,
    STEM_COLLISION_WORDS, TASK_WORD_FIFTH_IDS, TOPIC_SWITCH_VARIANTS,
)

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
# "wait" is content that the subject does not support in the second.
# Round 7 supersedes round 4: neutral time leaves these content-free carries.
# "best" is content since T2e, so "this is the best time of year" no longer carries.
TIME_CONTENT_FREE = {
    "is it in the morning or the evening", "is it after the spring equinox",
}
TIME_MISSES = {"can priya do saturday", "can it wait until monday"}
#: One admitted unit of the earlier subject. Since T2e its words, not a task
#: vocabulary, license a follow-up's content; time words stay neutral.
TIME_SUPPORT = (
    "Step one is done and the copy is ready and on track, due friday; her results compare "
    "well with the last round, and we may drop it from the plan once the vendor replies "
    "that it is finished."
)


@pytest.mark.parametrize("turn", [t for t in TIME_NEGATIVES if t not in TIME_CONTENT_FREE])
def test_a_time_subject_does_not_carry(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER) == []


@pytest.mark.parametrize("turn", [turn for turn in TIME_POSITIVES if turn not in TIME_MISSES])
def test_a_follow_up_with_a_time_modifier_carries(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER, supporting_text=TIME_SUPPORT) == [turn]


@pytest.mark.parametrize(
    "turn",
    [
        "is it on track for the autumn",
        "is it ready in the morning",
        "how did her results compare with the spring round",
    ],
)
def test_a_preposition_can_place_an_article_time_phrase(turn: str) -> None:
    assert _carried([turn], TIME_EARLIER, supporting_text=TIME_SUPPORT) == [turn]


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
    ROUND5_REVIEW_NEGATIVES + DUMMY_TIME_NEGATIVES + CLOSING_NEGATIVES,
)
def test_dummy_time_and_closing_turns_do_not_carry_even_with_shared_words(turn: str) -> None:
    assert _carried([turn], ROUND5_EARLIER) == []


# Round 7 ruling: bare temporal "late" is dummy; disclose this recall loss.
@pytest.mark.parametrize("turn", [t for t in ROUND5_POSITIVES
                                  if t != "is it still late in the evening there for the review"])
def test_a_referential_it_or_comparison_keeps_its_subject(turn: str) -> None:
    assert _carried([turn], ROUND5_EARLIER, supporting_text=TIME_SUPPORT) == [turn]


@pytest.mark.parametrize("weekday", ["wednesday", "saturday", "sunday"])
def test_a_shared_weekday_is_not_a_dummy_its_subject(weekday: str) -> None:
    earlier = ({"role": "user", "text": f"the migration plan is due {weekday}"},)
    assert _carried([f"is it {weekday} yet where you are"], earlier) == []


def test_a_neutral_time_word_can_carry_without_a_shared_stem() -> None:
    earlier = ({"role": "user", "text": "the migration plan is even better now"},)
    # Round 7 ruling: neutral time; this content-free carry is permitted.
    assert _carried(["is that the evening"], earlier) == ["is that the evening"]


# Round 6: verbatim fifth-set disclosures plus 40 variants frozen pre-fix.


@pytest.mark.parametrize("case_id,turn,subject", FIFTH_NEGATIVES)
def test_each_disclosed_fifth_case_follows_the_round7_classification(case_id, turn, subject) -> None:
    # Round 8 local quotations supersede the three quoted-task exceptions; T2e
    # removes the task-word licence the six TASK_WORD_FIFTH_IDS relied on.
    expected = [turn] if case_id in CONTENT_FREE_FIFTH_IDS - {"N50", "N52", "N53"} - TASK_WORD_FIFTH_IDS else []
    assert _carried([turn], repeated_earlier(subject, turn), subject_title=subject) == expected, case_id


@pytest.mark.parametrize("case_id,turn,subject", FIFTH_POSITIVES)
def test_each_disclosed_fifth_positive_carries_except_drop_it(case_id, turn, subject) -> None:
    expected = [] if turn == "drop it" else [turn]
    assert _carried([turn], repeated_earlier(subject, turn), subject_title=subject) == expected, case_id


@pytest.mark.parametrize("turn", LICENCE_NEGATIVES + STRUCTURAL_NEGATIVES)
def test_round6_authored_negative_variants_abstain(turn: str) -> None:
    assert _carried([turn], repeated_earlier("Alder database migration", turn),
                    subject_title="Alder database migration") == []


@pytest.mark.parametrize("turn", STRUCTURAL_POSITIVES)
def test_round6_authored_temporal_positive_variants_carry(turn: str) -> None:
    assert _carried([turn], repeated_earlier("Alder database migration", turn),
                    subject_title="Alder database migration") == [turn]


@pytest.mark.parametrize("title,turn", [
    ("Raven Telescope", "is that raven's final version"),
    ("Orchid Telescope", "should we change its telescopes"),
    ("Ember Shipping", "is that shipping ready"),
    ("Raven-telescope Plan", "can you compare its raven-telescope figures"),
])
def test_the_subjects_own_title_forms_license_content(title, turn) -> None:
    # The unit supports every other word; only the title's own forms license the rest.
    support = "The final version is ready; compare the figures before any change."
    assert _carried([turn], repeated_earlier(title, turn), subject_title=title, supporting_text=support) == [turn]


def test_an_older_subjects_title_does_not_license_the_selected_subject() -> None:
    turn = "is its telescope ready"
    entries = ({"role": "user", "text": "Can we review the Raven Telescope?"},
               {"role": "user", "text": "Can we review the Briar workshop launch?"})
    assert _carried([turn], entries, subject_title="Briar workshop launch") == []


@pytest.mark.parametrize("turn", ["is it cold", "is it raining", "is it five miles away"])
def test_a_dummy_complement_does_not_carry_even_when_its_head_is_in_the_title(turn) -> None:
    title = "Cold Rain Miles"
    assert _carried([turn], repeated_earlier(title, turn), subject_title=title) == []


@pytest.mark.parametrize("turn", ["is it the first", "is it the second option", "is it the one"])
def test_a_bare_pointer_is_not_a_bare_date(turn: str) -> None:
    assert _carried([turn], EARLIER, subject_title="Marlow Quay Survey") == [turn]


def test_snowy_is_a_dummy_weather_head_even_when_in_the_subject_title() -> None:
    assert _carried(["is it snowy outside"], EARLIER, subject_title="Snowy Launch") == []


@pytest.mark.parametrize("title,turn", [
    ("Cold Launch", "how cold is it outside"),
    ("Far Station", "how far is it to the station"),
])
def test_a_fronted_weather_or_distance_complement_is_dummy(title, turn) -> None:
    assert _carried([turn], repeated_earlier(title, turn), subject_title=title) == []


@pytest.mark.parametrize("turn", ["it has been late", "it had been midnight"])
def test_a_perfect_copula_still_has_a_dummy_time_complement(turn: str) -> None:
    assert _carried([turn], EARLIER) == []


@pytest.mark.parametrize("turn", sorted(TIME_CONTENT_FREE) + list(TIME_LINK_NEGATIVES))
def test_round7_neutral_time_content_free_labels_carry(turn: str) -> None:
    # Round 7 ruling supersedes the older negative neutral-time labels.
    assert _carried([turn], ROUND5_EARLIER) == [turn]


def test_round7_discloses_the_bare_temporal_head_recall_loss() -> None:
    turn = "is it still late in the evening there for the review"
    assert _carried([turn], ROUND5_EARLIER) == []


@pytest.mark.parametrize("word", STEM_COLLISION_WORDS)
def test_round7_a_function_word_stem_cannot_hide_new_content(word: str) -> None:
    from exomem import working_set_anaphora

    assert working_set_anaphora.forms(word) & working_set_anaphora._FUNCTION_WORDS
    turn = f"i got a new {word}; can you review it"
    assert _carried([turn], repeated_earlier("Alder database migration", turn),
                    subject_title="Alder database migration") == []
    assert word in analyze(turn).content_words


@pytest.mark.parametrize("word", ["offer", "often", "butter", "inner"])
def test_round7_surface_words_remain_content(word: str) -> None:
    from exomem.working_set_anaphora import content_words

    assert content_words((word,)) == (word,)


@pytest.mark.parametrize("turn", CODE_TURNS + TOPIC_SWITCH_VARIANTS)
def test_round7_code_and_new_content_never_carry(turn: str) -> None:
    assert _carried([turn], repeated_earlier("Alder database migration", turn),
                    subject_title="Alder database migration") == []


# CODE_TURNS[:4]'s code forms, with no content word outside the code.
@pytest.mark.parametrize("turn", (
    "what is `it = 1`", "is `they` so", "can you do ``that = 2``", "what is ```\nit = 1\n```",
))
def test_round7_pointers_inside_code_never_point(turn: str) -> None:
    assert not analyze(turn).points_back
    assert len(analyze(turn).content_words) == 1


def test_round7_a_function_word_in_vault_vocabulary_cannot_license_its_stem() -> None:
    from exomem.working_set_anaphora import content_words

    assert content_words(("offer",), vocabulary=frozenset({"off"})) == ("offer",)
    assert content_words(("off",), vocabulary=frozenset({"off"})) == ()


@pytest.mark.parametrize("turn,title", [
    ("the second option sounds better", "Marlow Quay Survey"),
    ("what happens if we can't close it", "Harbour Lantern Budget"),
])
def test_round7_surface_function_inflections_disclose_recall_losses(turn: str, title: str) -> None:
    # Round 7 ruling: sounds/happens are surface content, outside the licence.
    assert _carried([turn], EARLIER, subject_title=title) == []
