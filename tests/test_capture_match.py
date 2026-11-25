"""When a copy may take the date of an original: only when exactly one date explains it."""

from __future__ import annotations

from pathlib import Path

from exomem import capture_match
from exomem.capture_match import AMBIGUOUS, NO_MATCH, Fingerprint, Location, Original, Pool

BASE = int.from_bytes(bytes(range(0, 256, 8)), "big") ^ int.from_bytes(b"\xa5" * 32, "big")
COPY = Fingerprint(BASE, 640, 480)


def _original(distance: int, time: str, origin: str, *, width: int = 640, height: int = 480) -> Original:
    """An original whose fingerprint differs from the copy's in exactly `distance` bits."""
    return Original(
        Fingerprint(BASE ^ ((1 << distance) - 1), width, height), time, time[:10], origin, Location(Path(origin), ".jpg")
    )


def _pool(*originals: Original) -> Pool:
    pool = Pool()
    for original in originals:
        pool.add(original)
    return pool


def test_the_one_original_near_a_copy_gives_its_date():
    found = _pool(_original(6, "2021-05-06T07:08:09", "a"), _original(120, "2020-01-01T00:00:00", "b")).find(COPY)

    assert isinstance(found, Original) and found.origin == "a"


def test_a_second_original_with_another_time_just_behind_the_best_makes_the_match_ambiguous():
    burst = _pool(_original(2, "2022-01-01T10:00:00", "a"), _original(2 + capture_match.MIN_MARGIN - 1, "2022-01-01T10:00:07", "b"))

    assert burst.find(COPY) == AMBIGUOUS


def test_a_second_original_with_another_time_beyond_the_margin_leaves_the_best_unique():
    far = _pool(_original(2, "2022-01-01T10:00:00", "a"), _original(2 + capture_match.MIN_MARGIN, "2022-01-01T10:00:07", "b"))

    found = far.find(COPY)

    assert isinstance(found, Original) and found.origin == "a"


def test_two_copies_of_the_same_original_never_make_the_match_ambiguous():
    twice = _pool(_original(3, "2021-05-06T07:08:09", "vault-page"), _original(3, "2021-05-06T07:08:09", "archive/IMG_1.jpg"))

    found = twice.find(COPY)

    assert isinstance(found, Original) and found.origin == "archive/IMG_1.jpg"


def test_an_original_beyond_the_match_distance_is_no_match():
    assert _pool(_original(capture_match.MAX_DISTANCE + 1, "2021-05-06T07:08:09", "a")).find(COPY) == NO_MATCH


def test_an_original_of_another_shape_is_neither_a_match_nor_a_rival():
    pool = _pool(_original(2, "2022-01-01T10:00:00", "square", width=500, height=500), _original(8, "2021-05-06T07:08:09", "a"))

    found = pool.find(COPY)

    assert isinstance(found, Original) and found.origin == "a"


def test_an_empty_pool_matches_nothing():
    assert Pool().find(COPY) == NO_MATCH


def test_a_candidate_that_cannot_be_read_again_is_not_confirmed(tmp_path):
    gone = Location(tmp_path / "moved-away.jpg", ".jpg")

    assert not capture_match.confirm(gone, _original(0, "2021-05-06T07:08:09", "a"))
