from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import snowballstemmer

from exomem import bm25

_SUFFIXES = (
    "ational",
    "ization",
    "fulness",
    "ousness",
    "iveness",
    "biliti",
    "icate",
    "alize",
)


def _letters(value: int) -> str:
    chars: list[str] = []
    for _ in range(4):
        value, remainder = divmod(value, 26)
        chars.append(chr(ord("a") + remainder))
    return "".join(reversed(chars))


def test_stem_word_is_thread_safe_and_matches_fresh_serial_stemmer() -> None:
    worker_count = 8
    tokens_per_worker = 2_500
    batches = [
        [
            f"regul{_letters(worker * tokens_per_worker + index)}"
            f"{_SUFFIXES[index % len(_SUFFIXES)]}"
            for index in range(tokens_per_worker)
        ]
        for worker in range(worker_count)
    ]
    all_tokens = [token for batch in batches for token in batch]
    fresh = snowballstemmer.stemmer("english")
    expected = {token: fresh.stemWord(token) for token in all_tokens}
    barrier = Barrier(worker_count)

    def stem_batch(batch: list[str]) -> list[str]:
        barrier.wait()
        return [bm25.stem_word(token) for token in batch]

    previous_interval = sys.getswitchinterval()
    bm25.stem_word.cache_clear()
    try:
        sys.setswitchinterval(1e-6)
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            actual_batches = list(pool.map(stem_batch, batches))
    finally:
        sys.setswitchinterval(previous_interval)
        bm25.stem_word.cache_clear()

    actual = {
        token: stem
        for batch, stems in zip(batches, actual_batches, strict=True)
        for token, stem in zip(batch, stems, strict=True)
    }
    assert actual == expected
