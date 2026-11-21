from exomem import transcript_passages


def test_passage_addresses_the_timed_occurrence_that_matched():
    text = (
        "[0:03] [Speaker A]: Wait by the gate.\n"
        "[0:19] [Speaker B]: Wait by the gate.\n"
        "[0:24] [Speaker A]: The gate is open.\n"
    )

    match = transcript_passages.match(
        text, chunk="Gate meeting\n\n[0:19] [Speaker B]: Wait by the gate.",
        title="Gate meeting", query="who asked us to stay outside",
    )

    assert match is not None
    assert match.text == "[0:19] [Speaker B]: Wait by the gate."
    assert text[match.start:match.end] == match.text
    assert match.timestamp == 19


def test_literal_query_uses_the_timed_line_even_when_another_chunk_ranked():
    text = (
        "[0:03] [Speaker A]: We recorded the morning temperature.\n"
        "[0:19] [Speaker B]: Put a bucket of warm water under each sapling.\n"
    )

    match = transcript_passages.match(
        text, chunk="Gate meeting\n\n[0:03] [Speaker A]: We recorded the morning temperature.",
        title="Gate meeting", query="Put a bucket of warm water under each sapling.",
    )

    assert match is not None
    assert match.text == "[0:19] [Speaker B]: Put a bucket of warm water under each sapling."
    assert match.timestamp == 19


def test_passage_does_not_join_text_across_a_withheld_range():
    text = (
        "[0:03] [Speaker A]: We recorded the morning temperature.\n"
        "[0:19] [Speaker B]: Private record.\n"
    )
    start = text.index("[0:19]")

    assert transcript_passages.match(
        text, chunk=None, title="Gate meeting", query="Private record.", withheld=((start, len(text)),),
    ) is None


def test_ingestion_keeps_wrapped_speaker_turns_in_one_chunk():
    text = "[0:03] [Speaker A]: First line.\nContinued.\n[0:18] [Speaker B]: New turn."

    assert transcript_passages.chunks(text) == [
        "[0:03] [Speaker A]: First line.\nContinued.",
        "[0:18] [Speaker B]: New turn.",
    ]



def test_literal_query_can_match_the_continuation_of_a_crlf_capture():
    body = "## Capture\r\n[0:03] [Speaker A]: First line.\r\nContinued.\r\n[0:18] [Speaker B]: Next."
    text = transcript_passages.source_text(body)

    match = transcript_passages.match(text, chunk=None, title="Meeting", query="Continued.")

    assert match is not None
    assert match.timestamp == 3
    assert text[match.start:match.end] == "[0:03] [Speaker A]: First line.\nContinued."



def test_legacy_block_vector_cannot_choose_one_of_multiple_turns():
    text = "[0:03] First report.\n[0:18] Second report."

    assert transcript_passages.match(
        text, chunk=f"Meeting\n\n{text}", title="Meeting", query="what was discussed",
    ) is None



def test_legacy_vector_cannot_place_repeated_continuation_text():
    text = "[0:03] We should wait  by the gate.\n[0:19] Final reminder.\n\nwait by the gate."

    assert transcript_passages.match(
        text, chunk="Meeting\n\nwait by the gate.", title="Meeting", query="where should we assemble",
    ) is None


def test_out_of_range_timestamps_stay_ordinary_text():
    invalid = f"[{'9' * 400}:00:00] A malformed timestamp."
    text = f"{invalid}\n[0:03] First report.\n[0:18] Second report."

    assert transcript_passages.chunks(text) == [invalid, "[0:03] First report.", "[0:18] Second report."]


def test_repeated_literal_keeps_the_turn_proved_by_a_continuation():
    quote = "Wait by the gate."
    text = f"[0:03] {quote}\n[0:19] {quote}\nBring the blue clipboard."

    match = transcript_passages.match(
        text, chunk="Meeting\n\nBring the blue clipboard.", title="Meeting", query=quote,
    )

    assert match is not None and match.timestamp == 19
    assert match.text == "[0:19] Wait by the gate.\nBring the blue clipboard."


def test_exact_phrase_near_the_end_of_a_long_turn_keeps_its_timestamp():
    quote = "Inspect the hinge on the west door."
    text = f"[0:03] {'We discussed the building plan. ' * 150}{quote}\n[0:19] The meeting ended."

    match = transcript_passages.match(text, chunk=None, title="Meeting", query=quote)

    assert match is not None and match.timestamp == 3
    assert len(match.text) <= 4000 and match.text.endswith(quote)
    assert text[match.start:match.end] == match.text


def test_long_turn_prefers_the_repeat_in_the_winning_chunk():
    quote = "Wait by the gate."
    winning = f"Bring the blue clipboard. {quote}"
    text = f"[0:03] {quote} {'We reviewed the schedule. ' * 185}{winning}\n[0:19] The truck arrived."

    match = transcript_passages.match(text, chunk=f"Meeting\n\n{winning}", title="Meeting", query=quote)

    assert match is not None and match.timestamp == 3
    assert match.text.endswith(winning)
    assert text[match.start:match.end] == match.text
