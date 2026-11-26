"""Naming the speakers of a recording after the fact, and the names a reprocess keeps.

A recording's page is attached through the door, as an agent does, then renamed. The page's
labelled lines are the stored turns, so the tests read the lines and the front matter back.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from exomem import commands, extraction_parts, preserve, speech_attach, speaker_names
from exomem.cli_ops import OpError
from exomem.semantic_segments import format_ts
from exomem.vault import parse_frontmatter

PAYLOAD = b"fake audio bytes, no container"
SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Welcome back to the show."},
    {"start": 4.0, "end": 9.0, "text": "Thanks for having me."},
    {"start": 9.0, "end": 14.0, "text": "Speaker A said it first."},
]
TURNS = [
    {"start": 0.0, "end": 4.0, "label": "SPEAKER_00"},
    {"start": 4.0, "end": 9.0, "label": "SPEAKER_01"},
    {"start": 9.0, "end": 14.0, "label": "SPEAKER_00"},
]


@pytest.fixture(autouse=True)
def _states_its_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: 120.0)


def _attached(vault: Path, turns=TURNS, segments=SEGMENTS) -> tuple[str, Path]:
    """A recording whose page holds an attached, labelled transcript; returns its path and the page."""
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "interview.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(PAYLOAD)
    relative = binary.relative_to(vault).as_posix()
    commands.op_process_media(vault, path=relative, operation="process")
    commands.op_process_media(
        vault, path=relative, operation="attach-speech", binary_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
        binary_size=len(PAYLOAD), segments=segments, turns=turns, engine="whisper + pyannote",
    )
    return relative, binary.with_name(binary.name + ".md")


def _rename(vault: Path, relative: str, renames: dict[str, str]) -> dict:
    return commands.op_process_media(vault, path=relative, operation="rename-speakers", renames=renames)


def _fields(page: Path) -> dict:
    return parse_frontmatter(page.read_text(encoding="utf-8"))[0]


def test_a_rename_rewrites_each_label_span_and_leaves_every_other_byte(vault: Path) -> None:
    relative, page = _attached(vault)
    before = page.read_text(encoding="utf-8")

    reply = _rename(vault, relative, {"Speaker A": "Hugo"})

    after = page.read_text(encoding="utf-8")
    assert reply["outcome"] == "renamed" and reply["renamed"] == [{"label": "Speaker A", "name": "Hugo"}]
    assert "[0:00] [Hugo]: Welcome back to the show." in after
    assert "[0:09] [Hugo]: Speaker A said it first." in after, "words that mention the old label stay"
    assert "[0:04] [Speaker B]: Thanks for having me." in after
    assert _fields(page)["speakers"] == ["Hugo", "Speaker B"]
    # Only the label spans, `speakers` and the new `speaker_names` field differ from the page before.
    named = '\nspeaker_names: [{"name": "Hugo", "label": "Speaker A", "basis": "person"}]'
    assert after.replace(named, "") == (
        before.replace("[Speaker A]: Welcome", "[Hugo]: Welcome")
        .replace("[Speaker A]: Speaker A said", "[Hugo]: Speaker A said")
        .replace("speakers: [Speaker A, Speaker B]", "speakers: [Hugo, Speaker B]")
    )


def test_naming_every_speaker_through_a_person_verifies_the_page_and_a_partial_naming_does_not(vault: Path) -> None:
    relative, page = _attached(vault)

    _rename(vault, relative, {"Speaker A": "Hugo"})
    assert _fields(page)["speaker_verification"] == "anonymous"
    assert _fields(page)["speaker_names"] == [{"name": "Hugo", "label": "Speaker A", "basis": "person"}]

    reply = _rename(vault, relative, {"Speaker B": "Sam"})
    assert _fields(page)["speaker_verification"] == "human-verified"
    assert reply["speaker_verification"] == "human-verified"


def test_a_second_rename_of_the_same_speaker_keeps_the_label_it_first_replaced(vault: Path) -> None:
    relative, page = _attached(vault)
    _rename(vault, relative, {"Speaker A": "Hugo"})

    _rename(vault, relative, {"Hugo": "Hugo A."})

    assert _fields(page)["speaker_names"] == [{"name": "Hugo A.", "label": "Speaker A", "basis": "person"}]
    assert "[0:00] [Hugo A.]: Welcome back to the show." in page.read_text(encoding="utf-8")


def test_a_rename_that_was_applied_before_writes_nothing(vault: Path) -> None:
    relative, page = _attached(vault)
    _rename(vault, relative, {"Speaker A": "Hugo"})
    written = page.read_bytes()

    reply = _rename(vault, relative, {"Speaker A": "Hugo"})

    assert reply["outcome"] == "unchanged" and page.read_bytes() == written


def test_a_rename_reaches_the_part_pages_of_a_long_transcript(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 600)
    segments = [{"start": float(index * 3), "end": float(index * 3 + 2), "text": f"Line {index} of a long talk."} for index in range(40)]
    turns = [{"start": float(index * 3), "end": float(index * 3 + 2), "label": f"V{index % 2}"} for index in range(40)]
    relative, page = _attached(vault, turns=turns, segments=segments)
    assert extraction_parts.count_of(_fields(page)) > 2

    _rename(vault, relative, {"Speaker A": "Hugo"})

    parts = [extraction_parts.part_path(page, n) for n in range(2, extraction_parts.count_of(_fields(page)) + 1)]
    texts = [page.read_text(encoding="utf-8"), *(part.read_text(encoding="utf-8") for part in parts)]
    assert not any("[Speaker A]" in text for text in texts)
    assert sum(text.count("[Hugo]") for text in texts) == 20


def test_a_rename_of_merged_turns_rewrites_lines_that_carry_no_time(vault: Path) -> None:
    relative, page = _attached(vault)
    merged = page.read_text(encoding="utf-8")
    for stamp in ("[0:00] ", "[0:04] ", "[0:09] "):
        merged = merged.replace(stamp, "")
    page.write_text(merged, encoding="utf-8")

    _rename(vault, relative, {"Speaker B": "Sam"})

    assert "[Sam]: Thanks for having me." in page.read_text(encoding="utf-8")


def test_a_leading_unlabelled_line_and_a_continuation_line_stay_byte_for_byte() -> None:
    text = "An unlabelled opening line.\n[0:00] [Speaker A]: First words\nthat continue here.\r\n[0:05] [Speaker B]: Reply.\n"

    renamed = speaker_names.rename_labels(text, {"Speaker A": "Hugo"})

    assert renamed == "An unlabelled opening line.\n[0:00] [Hugo]: First words\nthat continue here.\r\n[0:05] [Speaker B]: Reply.\n"


@pytest.mark.parametrize(
    ("renames", "code"),
    [
        ({"Speaker A": "Hugo [host]"}, "SPEAKER_NAME_INVALID"),
        ({"Speaker A": " Hugo"}, "SPEAKER_NAME_INVALID"),
        ({"Speaker Z": "Hugo"}, "SPEAKER_LABEL_UNKNOWN"),
        ({"Speaker A": "Speaker B"}, "SPEAKER_NAME_IN_USE"),
    ],
)
def test_a_rename_that_breaks_a_rule_is_refused_and_changes_nothing(vault: Path, renames: dict, code: str) -> None:
    relative, page = _attached(vault)
    written = page.read_bytes()

    with pytest.raises(OpError) as refused:
        _rename(vault, relative, renames)

    assert refused.value.code == code and page.read_bytes() == written


def test_a_rename_of_a_label_that_the_page_lists_and_no_line_carries_is_refused_and_changes_nothing(vault: Path) -> None:
    relative, page = _attached(vault)
    page.write_text(page.read_text(encoding="utf-8").replace("speakers: [Speaker A, Speaker B]", "speakers: [Speaker A, Speaker B, Ghost]"), encoding="utf-8")
    written = page.read_bytes()

    with pytest.raises(OpError) as refused:
        _rename(vault, relative, {"Ghost": "Hugo"})

    assert refused.value.code == "SPEAKER_LABEL_UNKNOWN" and page.read_bytes() == written


def test_a_rename_enrols_no_voice_and_points_the_owner_to_enroll_speaker(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import voice_profiles

    relative, _page = _attached(vault)
    monkeypatch.setattr(voice_profiles, "save_profile", lambda *args, **kwargs: pytest.fail("a rename enrolled a voice"))

    reply = _rename(vault, relative, {"Speaker A": "Hugo"})

    assert "enroll-speaker" in reply["next_action"]


# --- Names carried across a reprocess -----------------------------------------------------------


def test_a_reprocess_carries_a_name_to_the_label_that_overlaps_its_lines() -> None:
    old = "[0:00] [Hugo]: One.\n[0:05] [Speaker B]: Two.\n[0:10] [Hugo]: Three.\n[0:15] [Speaker B]: Four.\n"
    new = "[0:00] [Speaker B]: One.\n[0:05] [Speaker A]: Two.\n[0:10] [Speaker B]: Three.\n[0:15] [Speaker A]: Four.\n"

    carried = speaker_names.carry(old, [{"name": "Hugo", "label": "Speaker A", "basis": "person"}], new)

    assert carried.labels == {"Speaker B": "Hugo"}
    assert carried.entries == ({"name": "Hugo", "label": "Speaker B", "basis": "person"},)
    assert carried.not_carried == ()


def test_a_name_with_no_label_that_mostly_overlaps_it_is_not_carried() -> None:
    old = "[0:00] [Hugo]: One.\n[0:10] [Speaker B]: Two.\n[0:20] [Speaker B]: Three.\n"
    new = "[0:00] [Speaker B]: One.\n[0:08] [Speaker A]: Two.\n[0:20] [Speaker B]: Three.\n[0:30] [Speaker B]: Four.\n"

    carried = speaker_names.carry(old, [{"name": "Hugo", "label": "Speaker A", "basis": "person"}], new)

    assert carried.labels == {} and carried.not_carried == ("Hugo",)


def test_a_reprocess_without_labels_lists_every_name_as_not_carried() -> None:
    old = "[0:00] [Hugo]: One.\n[0:05] [Sam]: Two.\n[0:10] [Hugo]: Three.\n"
    new = "[0:00] One.\n[0:05] Two.\n[0:10] Three.\n"
    names = [{"name": "Hugo", "label": "Speaker A", "basis": "person"}, {"name": "Sam", "label": "Speaker B", "basis": "person"}]

    carried = speaker_names.carry(old, names, new)

    assert carried.labels == {} and carried.not_carried == ("Hugo", "Sam")


def test_names_on_merged_turns_are_not_carried_because_no_line_has_a_time() -> None:
    old = "[Hugo]: One.\n[Sam]: Two.\n"
    new = "[0:00] [Speaker A]: One.\n[0:05] [Speaker B]: Two.\n"

    carried = speaker_names.carry(old, [{"name": "Hugo", "label": "Speaker A", "basis": "person"}], new)

    assert carried.labels == {} and carried.not_carried == ("Hugo",)


def test_a_reprocess_of_a_fifty_thousand_line_recording_carries_names_in_one_sweep() -> None:
    # Two voices, three seconds a line. Comparing every new span with every old span took five seconds at
    # six thousand lines, so fifty thousand would take minutes; one sweep takes a fraction of a second.
    voices = ("Hugo", "Sam")
    old = "".join(f"[{format_ts(n * 3)}] [{voices[n % 2]}]: line {n}\n" for n in range(50_000))
    new = old.replace("[Hugo]", "[Speaker B]").replace("[Sam]", "[Speaker A]")
    names = [{"name": name, "label": f"Speaker {'AB'[index]}", "basis": "person"} for index, name in enumerate(voices)]

    started = time.perf_counter()
    carried = speaker_names.carry(old, names, new)
    elapsed = time.perf_counter() - started

    assert carried.labels == {"Speaker B": "Hugo", "Speaker A": "Sam"}
    assert elapsed < 10, f"carrying names over 50,000 lines took {elapsed:.1f} s"


def test_a_rewrite_of_a_page_with_windows_line_endings_changes_only_the_label_bytes(vault: Path) -> None:
    _relative, page = _attached(vault)
    crlf = page.read_text(encoding="utf-8").replace("\n", "\r\n")

    renamed = preserve.map_extracted_text(crlf, lambda text: speaker_names.rename_labels(text, {"Speaker A": "Hugo"}))

    assert renamed == crlf.replace("[Speaker A]: ", "[Hugo]: ")
    assert preserve.map_extracted_text(crlf, lambda text: text) == crlf
