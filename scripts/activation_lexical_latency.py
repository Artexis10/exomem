"""Activation's lexical stage on long turns: unbounded vs bounded (roadmap U4).

`working_set_runtime.lexical_evidence` used to hand every content word of a
turn to one FTS5 MATCH. On long live turns rich in model numbers and ordinary
words that stage took 3.2-4.3 s. It now keeps at most
`ACTIVATION_LEXICAL_MAX_TERMS` units, rarest in the knowledge base first,
carrying at most `ACTIVATION_LEXICAL_MAX_STEMS` stems. This harness times both shapes
against the SAME synthetic catalogue, warm, so the difference reported is the
change and not a cold start. "Unbounded" is the shipped function with its
term budget switched off, which is exactly the pre-U4 query.

Synthetic only: it builds its own vault and state under a scratch root and
never reads a real vault. Do not point this at a live cell.

    uv run python scripts/activation_lexical_latency.py
    uv run python scripts/activation_lexical_latency.py --pages 5000 --json
    uv run python scripts/activation_lexical_latency.py --shared 3
    uv run python scripts/activation_lexical_latency.py --script japanese
    uv run python scripts/activation_lexical_latency.py --common-turn --turn-words 12

The corpus is Zipf-distributed invented words (so the head of the vocabulary
is on most pages, as ordinary English is) with rare model-number tokens, and
a turn is about `--turn-words` words drawn from the same distribution plus one
page's own rare words. `--shared N` also writes each rare word on N other
anchors, never both of one anchor's words on the same page, so the named page
is the only one holding both. `--script japanese` writes the corpus and the
turns as unspaced runs: invented kanji words joined by particles into
sentences, and each anchor named by two rare kanji words, each its own
sentence. `--common-turn` makes each turn `--turn-words` distinct words from
the head of the vocabulary and nothing else: at twelve words it is a short
turn the budget keeps whole, whose every word is on most pages. Reported per
shape: p50/p95 of the stage's wall time, for the bounded shape also p95 with
its term-frequency cache emptied before every turn (`cold_p95_ms`), how often
the page the turn names comes first (None for common turns), and the mean
kept/dropped unit counts. Run it more than once; one run is not a measurement.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratch_root  # noqa: E402

DEFAULT_PAGES = 4000
DEFAULT_TURNS = 20
DEFAULT_REPEAT = 3
DEFAULT_TURN_WORDS = 400
#: Invented vocabulary size and page length. The Zipf head is what makes
#: ordinary words ordinary: the top few hundred sit on most pages.
VOCABULARY = 6000
PAGE_WORDS = 220
#: One page in this many is an anchor, the catalogue activation ranks.
ANCHOR_EVERY = 8
#: The head of the vocabulary a `--common-turn` draws from: every word in it
#: sits on most pages.
COMMON_HEAD = 60
_SYLLABLES = ("ka", "lo", "mi", "ren", "tas", "vu", "dor", "pel", "sin", "qua", "bre", "zon")
#: The Japanese corpus: its vocabulary is written in `_KANJI`, joined by the
#: particles in `_PARTICLES` into sentences of `_SENTENCE_WORDS` words, and an
#: anchor is named by two words of `_RARE_KANJI`, which nothing else uses.
#: Four hundred ideographs make every word two characters, so a word's one
#: bigram is about as rare as the word; the question kanji 何 is left out,
#: since a bigram holding it is not content.
_KANJI = "".join(chr(code) for code in range(0x4E00, 0x4E00 + 401) if chr(code) != "何")
_PARTICLES = "のはをにがと"
_RARE_KANJI = ("鑑鑿鍔鎧鑼鐸錨鋸鉞鏑鐙鑓鍬鋏錐鎚", "鷲鷹鶴鵜鷺鶯鴨鵡鸚鴉鵬鶏鴇鳶鴫鵯")
_SENTENCE_WORDS = 12


def _kanji_word(index: int) -> str:
    """A deterministic invented kanji word of at least two characters."""
    parts = []
    value = index + len(_KANJI)
    while value:
        value, digit = divmod(value, len(_KANJI))
        parts.append(_KANJI[digit])
    return "".join(parts)


def _kanji_name(index: int, which: int) -> str:
    """Anchor `index`'s rare name `which` (0 or 1): three rare kanji."""
    rare = _RARE_KANJI[which]
    return "".join(rare[index // len(rare) ** power % len(rare)] for power in range(3))


def _sentences(words: list[str], names: list[str], at: int) -> str:
    """Unspaced Japanese text: `words` joined by particles into sentences,
    with each of `names` its own sentence after the first `at` sentences."""
    sentences = [
        "".join(
            word + _PARTICLES[(start + offset) % len(_PARTICLES)]
            for offset, word in enumerate(words[start : start + _SENTENCE_WORDS])
        )
        for start in range(0, len(words), _SENTENCE_WORDS)
    ]
    sentences[at:at] = names
    return "。".join(sentences) + "。"


def _word(index: int) -> str:
    """A deterministic invented word for vocabulary slot `index`."""
    parts = []
    value = index + 1
    while value:
        value, digit = divmod(value, len(_SYLLABLES))
        parts.append(_SYLLABLES[digit])
    return "".join(parts) + ("x" if len(parts) < 2 else "")


def _zipf_weights(size: int) -> list[float]:
    return [1.0 / (rank + 1) for rank in range(size)]


def percentile(samples: list[float], share: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), math.ceil(share * len(ordered))))
    return ordered[rank - 1]


def build_vault(
    vault: Path, pages: int, seed: int, *, script: str = "latin", shared: int = 0
) -> tuple[list[str], dict[str, list[str]]]:
    """Write `pages` notes; return `(page paths, {anchor path: its rare words})`.

    With `shared`, anchor k also carries the first rare word of the `shared`
    anchors before it and the second of the `shared` before those, so each
    rare word sits on `shared` other anchors and no other page holds both.
    """
    rng = random.Random(seed)
    japanese = script == "japanese"
    words = [(_kanji_word if japanese else _word)(index) for index in range(VOCABULARY)]
    weights = _zipf_weights(VOCABULARY)
    kb = vault / "Knowledge Base" / "Notes"
    kb.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    named: dict[str, list[str]] = {}
    for index in range(pages):
        body = rng.choices(words, weights=weights, k=PAGE_WORDS)
        rel = f"Knowledge Base/Notes/page-{index:05d}.md"
        rare: list[str] = []
        if index % ANCHOR_EVERY == 0:
            if japanese:
                rare = [_kanji_name(index // ANCHOR_EVERY, which) for which in (0, 1)]
            else:
                rare = [f"qx{index:05d}", f"{rng.choice('abcdefgh')}r{index * 7 % 99991:05d}"]
            earlier = list(named.values())
            borrowed = [earlier[-back][0] for back in range(1, shared + 1) if back <= len(earlier)]
            borrowed += [
                earlier[-back][1]
                for back in range(shared + 1, 2 * shared + 1)
                if back <= len(earlier)
            ]
            named[rel] = rare
            rare = rare + borrowed
        if japanese:
            text = _sentences(body, rare, 1)
        else:
            body[5:5] = rare
            text = " ".join(body)
        (vault / rel).write_text(
            f"---\ntype: note\nstatus: active\n---\n\n# Page {index:05d}\n\n{text}\n",
            encoding="utf-8",
        )
        paths.append(rel)
    return paths, named


def build_turns(
    named: dict[str, list[str]],
    count: int,
    turn_words: int,
    seed: int,
    *,
    script: str = "latin",
    common: bool = False,
) -> list[tuple[str, str | None]]:
    """`(turn text, the anchor it names)`: everyday words and one page's name,
    or with `common` only distinct head words, naming nothing."""
    rng = random.Random(seed + 1)
    japanese = script == "japanese"
    words = [(_kanji_word if japanese else _word)(index) for index in range(VOCABULARY)]
    weights = _zipf_weights(VOCABULARY)
    targets = rng.sample(sorted(named), min(count, len(named)))
    turns: list[tuple[str, str | None]] = []
    for target in targets:
        if common:
            head = rng.sample(words[:COMMON_HEAD], min(turn_words, COMMON_HEAD))
            turns.append(("".join(head) if japanese else " ".join(head), None))
            continue
        filler = rng.choices(words, weights=weights, k=turn_words)
        middle = len(filler) // 2
        if japanese:
            text = _sentences(filler, named[target], middle // _SENTENCE_WORDS)
        else:
            text = " ".join(filler[:middle] + named[target] + filler[middle:])
        turns.append((text, target))
    return turns


def _time_shape(vault, turns, rows, repeat, checkpoint, *, bounded: bool):
    from exomem import lexstore, working_set_runtime

    original = working_set_runtime.lexical_term_budget
    if not bounded:
        working_set_runtime.lexical_term_budget = lambda: None
    samples: list[float] = []
    cold: list[float] = []
    first = 0
    kept: list[int] = []
    dropped: list[int] = []
    try:
        # One extra pass for the bounded shape with the term-frequency cache
        # emptied before every turn: the cost right after a publish.
        for attempt in range(repeat + (1 if bounded else 0)):
            for turn, target in turns:
                selection: dict = {}
                if attempt == repeat:
                    lexstore.get_store(vault)._term_frequency_cache = None
                started = time.perf_counter()
                hits, state = working_set_runtime.lexical_evidence(
                    vault,
                    turn,
                    rows,
                    limit=8,
                    recall_checkpoint=checkpoint,
                    selection=selection,
                )
                elapsed = (time.perf_counter() - started) * 1000.0
                if state != "available":
                    raise RuntimeError(f"lexical stage answered {state!r}")
                if attempt == repeat:
                    cold.append(elapsed)
                    continue
                samples.append(elapsed)
                first += bool(hits) and hits[0].path == target
                if selection:
                    kept.append(selection["terms_kept"])
                    dropped.append(selection["terms_dropped"])
    finally:
        working_set_runtime.lexical_term_budget = original
    return {
        "p50_ms": round(percentile(samples, 0.50), 1),
        "p95_ms": round(percentile(samples, 0.95), 1),
        "max_ms": round(max(samples), 1),
        "cold_p95_ms": round(percentile(cold, 0.95), 1) if cold else None,
        "samples": len(samples),
        "named_page_first": (
            round(first / len(samples), 3) if any(target for _turn, target in turns) else None
        ),
        "mean_units_kept": round(statistics.mean(kept), 1) if kept else None,
        "mean_units_dropped": round(statistics.mean(dropped), 1) if dropped else None,
    }


def run(
    pages: int,
    turns: int,
    repeat: int,
    turn_words: int,
    seed: int,
    *,
    script: str = "latin",
    shared: int = 0,
    common: bool = False,
) -> dict:
    with scratch_root.scratch_root("exomem-activation-lexical-") as base:
        os.environ["EXOMEM_STATE_ROOT"] = str(base / "state")
        os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(base / "state" / "writer-lease")
        os.environ["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
        from exomem import find as find_module
        from exomem import freshness, lexstore
        from exomem.vault import walk_vault_md

        vault = base / "vault"
        started = time.perf_counter()
        paths, named = build_vault(vault, pages, seed, script=script, shared=shared)
        # The production shape: the watcher keeps the recall registry live
        # and activation hands the lexical stage the request's own recall
        # checkpoint, so no stage re-derives it by walking the vault.
        freshness.seed(
            vault,
            "vault",
            ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(vault)),
        )
        freshness.seed(
            vault,
            "kb",
            (
                (str(path), freshness.stat_signature(path))
                for path in find_module._walk_md(vault / "Knowledge Base")
            ),
        )
        lexstore.ensure_fresh(vault)
        checkpoint = freshness.recall_checkpoint(vault, "kb")
        build_seconds = time.perf_counter() - started
        rows = [SimpleNamespace(path=path, title=Path(path).stem) for path in named]
        cases = build_turns(named, turns, turn_words, seed, script=script, common=common)
        # Warm both shapes once so neither pays the first connection.
        _time_shape(vault, cases[:1], rows, 1, checkpoint, bounded=False)
        _time_shape(vault, cases[:1], rows, 1, checkpoint, bounded=True)
        report = {
            "pages": len(paths),
            "anchors": len(rows),
            "turns": len(cases),
            "turn_words": turn_words,
            "script": script,
            "shared": shared,
            "common_turn": common,
            "build_seconds": round(build_seconds, 1),
            "unbounded": _time_shape(vault, cases, rows, repeat, checkpoint, bounded=False),
            "bounded": _time_shape(vault, cases, rows, repeat, checkpoint, bounded=True),
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    parser.add_argument("--turn-words", type=int, default=DEFAULT_TURN_WORDS)
    parser.add_argument("--seed", type=int, default=4)
    parser.add_argument("--script", choices=("latin", "japanese"), default="latin")
    parser.add_argument("--shared", type=int, default=0)
    parser.add_argument("--common-turn", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(
        args.pages,
        args.turns,
        args.repeat,
        args.turn_words,
        args.seed,
        script=args.script,
        shared=args.shared,
        common=args.common_turn,
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(
        f"{report['pages']} pages, {report['anchors']} anchors, {report['turns']} turns "
        f"of ~{report['turn_words']} {report['script']} words, rare words on "
        f"{report['shared']} other anchors{', common words only' if report['common_turn'] else ''}"
        f" (built in {report['build_seconds']}s)\n"
    )
    print(
        f"{'shape':>10}  {'p50 ms':>8}  {'p95 ms':>8}  {'max ms':>8}  {'cold p95':>8}  "
        f"{'named first':>11}  kept/dropped"
    )
    for shape in ("unbounded", "bounded"):
        row = report[shape]
        units = (
            f"{row['mean_units_kept']}/{row['mean_units_dropped']}"
            if row["mean_units_kept"] is not None
            else "-"
        )
        cold = row["cold_p95_ms"] if row["cold_p95_ms"] is not None else "-"
        first = row["named_page_first"] if row["named_page_first"] is not None else "-"
        print(
            f"{shape:>10}  {row['p50_ms']:>8}  {row['p95_ms']:>8}  {row['max_ms']:>8}  "
            f"{cold:>8}  {first:>11}  {units}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
