"""Exact name mentions in page prose, and the edits that link them. Fixtures are invented."""

from __future__ import annotations

from exomem import entity_recurrence, name_mentions


def _entry(path: str, title: str, *aliases: str) -> entity_recurrence.RegistryEntry:
    names = (title, *aliases)
    return entity_recurrence.RegistryEntry(
        path=path, title=title,
        identities=frozenset(entity_recurrence.identity_key(name) for name in names),
        tokens=frozenset(), entity_type="concept", entity_family="concept",
    )


INDEX = name_mentions.build_index([
    _entry("E/Marin Holt.md", "Marin Holt", "Marin"),
    _entry("E/Marin Holt Partners.md", "Marin Holt Partners"),
    _entry("E/Harbour Supply Co.md", "Harbour Supply Co"),
    _entry("E/Kestrel.md", "Kestrel (sailing boat)", "the Kestrel"),
    _entry("E/Dana Reyes.md", "Dana Reyes", "Dana"),
    _entry("E/Dana Freight.md", "Dana Freight", "Dana"),
    _entry("E/Zoë Åberg.md", "Zoë Åberg"),
])


def _found(text: str) -> list[tuple[str, str]]:
    return [(m.path.removeprefix("E/").removesuffix(".md"), m.written) for m in
            name_mentions.find_mentions(text, INDEX, name_mentions.prose_mask(text))]


def test_a_title_and_an_alias_match_whatever_their_case_and_punctuation() -> None:
    assert _found("MARIN HOLT, the kestrel and (Harbour Supply Co).") == [
        ("Marin Holt", "MARIN HOLT"), ("Kestrel", "the kestrel"), ("Harbour Supply Co", "Harbour Supply Co"),
    ]


def test_the_longest_name_wins_over_a_shorter_name_inside_it() -> None:
    assert _found("Marin Holt Partners arrived, and Marin Holt Senior did not.") == [
        ("Marin Holt Partners", "Marin Holt Partners"), ("Marin Holt", "Marin Holt"),
    ]


def test_a_name_that_two_entities_answer_to_is_never_offered_and_takes_no_shorter_match() -> None:
    assert _found("Dana signed. Dana Reyes witnessed.") == [("Dana Reyes", "Dana Reyes")]


def test_a_name_is_matched_on_word_boundaries_and_unicode() -> None:
    assert _found("Marinas and Marinade, but Zoë Åberg came.") == [("Zoë Åberg", "Zoë Åberg")]


def test_a_name_never_spans_a_line_break() -> None:
    assert _found("Harbour Supply\nCo opened.") == []


def test_text_that_is_not_prose_is_never_read() -> None:
    text = "\n".join([
        "---", "note: Marin Holt", "---", "# Marin Holt", "",
        "- `Marin Holt` in code, [Marin Holt](a.md), ![Marin Holt](b.png), <b>Marin Holt</b>,",
        "- [[Marin Holt]] and [[Marin Holt|Marin]] and https://example.org/Marin_Holt and https://Marin.example/x",
        "", "```", "Marin Holt", "```", "", "<!-- Marin Holt -->", "", "Plain Marin Holt.",
    ])
    assert _found(text) == [("Marin Holt", "Marin Holt")]
    assert name_mentions.find_mentions(text, INDEX, name_mentions.prose_mask(text))[0].start == text.rindex("Marin Holt")


def _edits(text: str, wanted: list[int]) -> list[tuple[str, str]]:
    mask = name_mentions.prose_mask(text)
    mentions = name_mentions.find_mentions(text, INDEX, mask)
    chosen = [mentions[at] for at in wanted]
    links = [f"[[{m.path}|{m.written}]]" for m in chosen]
    return [(e.old, e.new) for _at, e in name_mentions.edits_for(text, text, chosen, links, mask, max_chars=400)]


def test_a_unique_name_is_its_own_anchor() -> None:
    assert _edits("Harbour Supply Co opened.", [0]) == [("Harbour Supply Co", "[[E/Harbour Supply Co.md|Harbour Supply Co]]")]


def test_a_repeated_name_grows_just_enough_context_to_occur_once() -> None:
    text = "Marin Holt asked. Later Marin Holt paid."
    edits = _edits(text, [0])
    assert edits == [("Marin Holt asked.", "[[E/Marin Holt.md|Marin Holt]] asked.")]
    assert text.count(edits[0][0]) == 1


def test_edits_run_from_the_last_mention_so_each_anchor_stays_unique_after_the_one_before() -> None:
    text = "Marin Holt came. Harbour Supply Co paid. Marin Holt left. Harbour Supply Co closed."
    mask = name_mentions.prose_mask(text)
    mentions = name_mentions.find_mentions(text, INDEX, mask)
    chosen = [mentions[0], mentions[1]]
    links = [f"[[{m.path}|{m.written}]]" for m in chosen]

    edits = name_mentions.edits_for(text, text, chosen, links, mask, max_chars=400)

    assert [at for at, _edit in edits] == [1, 0], "the last mention is edited first"
    simulated = text
    for _at, edit in edits:
        assert simulated.count(edit.old) == 1, "each anchor is unique in the page the earlier edits left"
        simulated = simulated.replace(edit.old, edit.new, 1)
    assert simulated.startswith("[[E/Marin Holt.md|Marin Holt]] came. [[E/Harbour Supply Co.md|Harbour Supply Co]] paid.")


def test_a_mention_with_no_anchor_inside_the_limit_is_left_out() -> None:
    text = "Marin Holt " * 3
    mask = name_mentions.prose_mask(text)
    mentions = name_mentions.find_mentions(text, INDEX, mask)
    assert name_mentions.edits_for(text, text, mentions[:1], ["[[x]]"], mask, max_chars=12) == []
