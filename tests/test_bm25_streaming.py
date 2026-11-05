"""Streaming preserves the maintained tokenizer and admits Unicode work before allocation."""

import unicodedata

import pytest

from exomem import bm25, lexstore, runtime_resources


@pytest.mark.parametrize("text", [
    "Running HTTP/2 ledger: regulator alignment.",
    "東京タワー" * 2000,
    "กิ้น" * 2000,
    "e\u0315\u0300" * 2000,
    "\ufb03\u00df\u0130\u1e9e" * 1000,
    "\u1100\u1161\u11a8\uac01" * 2000,
    "e\u0301" + "\u0300" * 10000,
])
def test_streaming_and_materialized_units_preserve_both_token_modes(text) -> None:
    for query in (False, True):
        admitted = []
        streamed = list(bm25.iter_tokens(text, query=query, reserve=admitted.append))
        assert streamed == bm25.tokenize(text, query=query)
        assert streamed == [stem for unit in bm25.token_units(text, query=query) for stem in unit.stems]
        assert admitted and max(admitted) >= 65536


def test_normalization_is_not_started_after_its_workspace_is_refused(monkeypatch) -> None:
    actual = unicodedata.normalize
    normalized = []
    def observe(form, text):
        if form == "NFKC":
            normalized.append(len(text))
        return actual(form, text)
    monkeypatch.setattr(unicodedata, "normalize", observe)
    with runtime_resources.parser_preparation(0, limit_bytes=128 * 1024) as lease:
        with pytest.raises(runtime_resources.PreparationBudgetExceeded):
            list(bm25.iter_tokens("\ufb03" * 10000, reserve=lease.reserve))
        assert not normalized
    assert lease.reserved == 0


def test_casefold_and_latin_join_are_admitted_before_their_expansion(monkeypatch) -> None:
    actual = unicodedata.normalize
    completed = []
    def observe(form, text):
        result = actual(form, text)
        if form == "NFKC":
            # Casefold expands this normalized scalar threefold; its next bound grows.
            completed.append(result)
        return result
    monkeypatch.setattr(unicodedata, "normalize", observe)
    # Reject the first request after normalization, before full casefold starts.
    def refuse_after_normalization(amount):
        if completed:
            raise runtime_resources.PreparationBudgetExceeded("casefold workspace")
    with pytest.raises(runtime_resources.PreparationBudgetExceeded), runtime_resources.parser_preparation(0) as lease:
        list(bm25.iter_tokens("\u0390" * 10000, reserve=lambda amount: (refuse_after_normalization(amount), lease.reserve(amount))))
    assert lease.reserved == 0
    assert len(completed) == 1

    joined = []
    original = bm25._latin_fold
    monkeypatch.setattr(bm25, "_latin_fold", lambda word: (joined.append(word), original(word))[1])
    with runtime_resources.parser_preparation(0) as lease:
        with pytest.raises(runtime_resources.PreparationBudgetExceeded):
            list(bm25.iter_tokens("e\u0301" + "\u0300" * 40000, reserve=lease.reserve))
    assert not joined
    assert lease.reserved == 0


def test_cancellation_closes_the_stream_without_leaking_a_preparation_lease() -> None:
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        if checks == 100:
            raise TimeoutError("request ended")
    with runtime_resources.parser_preparation(0) as lease:
        with pytest.raises(TimeoutError):
            list(bm25.iter_tokens("日本語" * 10000, reserve=lease.reserve, check=cancel))
    assert lease.reserved == 0


def test_distinct_term_growth_stops_consumption_before_retaining_the_whole_stream(monkeypatch) -> None:
    """Dense short words can exhaust the counter after JSON serialization still fits."""
    words = [left + right for left in "abcdefghijklmnopqrstuvwxyz" for right in "abcdefghijklmnopqrstuvwxyz"]
    public = {"body": " ".join(words)}
    original = bm25.iter_tokens
    consumed = 0
    def observe(*args, **kwargs):
        nonlocal consumed
        for token in original(*args, **kwargs):
            consumed += 1
            yield token
    monkeypatch.setattr(bm25, "iter_tokens", observe)
    with runtime_resources.parser_preparation(2048, limit_bytes=160 * 1024) as lease:
        with pytest.raises(runtime_resources.PreparationBudgetExceeded):
            lexstore._released_public_terms(public, None, lease, None)
    assert 0 < consumed < len(words)
    assert lease.reserved == 0


def test_packing_refusal_precedes_the_packed_count_allocation(monkeypatch) -> None:
    original = lexstore.array
    packed = []
    def observe(width, values=None):
        if values is None:
            return original(width)
        packed.append(width)
        return original(width, values)
    monkeypatch.setattr(lexstore, "array", observe)
    def refuse(amount):
        raise runtime_resources.PreparationBudgetExceeded("packed terms do not fit")
    with pytest.raises(runtime_resources.PreparationBudgetExceeded):
        lexstore._pack_page_counts({"first": 255, "second": 256}, 511, reserve=refuse)
    assert not packed
