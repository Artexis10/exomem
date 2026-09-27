"""Activation's lexical stage on long turns: unbounded vs bounded (roadmap U4).

`working_set_runtime.lexical_evidence` used to hand every content word of a
turn to one FTS5 MATCH. On long live turns rich in model numbers and ordinary
words that stage took 3.2-4.3 s. It now keeps at most
`ACTIVATION_LEXICAL_MAX_TERMS` units, rarest in the catalogue first, and reads
a capped candidate window ranked inside SQL. This harness times both shapes
against the SAME synthetic catalogue, warm, so the difference reported is the
change and not a cold start. "Unbounded" is the shipped function with its
term budget switched off, which is exactly the pre-U4 query.

Synthetic only: it builds its own vault and state under a scratch root and
never reads a real vault. Do not point this at a live cell.

    uv run python scripts/activation_lexical_latency.py
    uv run python scripts/activation_lexical_latency.py --pages 5000 --json

The corpus is Zipf-distributed invented words (so the head of the vocabulary
is on most pages, as ordinary English is) with rare model-number tokens, and
a turn is about `--turn-words` words drawn from the same distribution plus one
page's own rare words. Reported per shape: p50/p95 of the stage's wall time,
how often the page the turn names comes first, and the mean kept/dropped unit
counts. Run it more than once; one run is not a measurement.
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
_SYLLABLES = ("ka", "lo", "mi", "ren", "tas", "vu", "dor", "pel", "sin", "qua", "bre", "zon")


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


def build_vault(vault: Path, pages: int, seed: int) -> tuple[list[str], dict[str, list[str]]]:
    """Write `pages` notes; return `(page paths, {anchor path: its rare words})`."""
    rng = random.Random(seed)
    words = [_word(index) for index in range(VOCABULARY)]
    weights = _zipf_weights(VOCABULARY)
    kb = vault / "Knowledge Base" / "Notes"
    kb.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    named: dict[str, list[str]] = {}
    for index in range(pages):
        body = rng.choices(words, weights=weights, k=PAGE_WORDS)
        rel = f"Knowledge Base/Notes/page-{index:05d}.md"
        if index % ANCHOR_EVERY == 0:
            rare = [f"qx{index:05d}", f"{rng.choice('abcdefgh')}r{index * 7 % 99991:05d}"]
            named[rel] = rare
            body[5:5] = rare
        (vault / rel).write_text(
            f"---\ntype: note\nstatus: active\n---\n\n# Page {index:05d}\n\n"
            + " ".join(body)
            + "\n",
            encoding="utf-8",
        )
        paths.append(rel)
    return paths, named


def build_turns(
    named: dict[str, list[str]], count: int, turn_words: int, seed: int
) -> list[tuple[str, str]]:
    """`(turn text, the anchor it names)`: everyday words and one page's name."""
    rng = random.Random(seed + 1)
    words = [_word(index) for index in range(VOCABULARY)]
    weights = _zipf_weights(VOCABULARY)
    targets = rng.sample(sorted(named), min(count, len(named)))
    turns = []
    for target in targets:
        filler = rng.choices(words, weights=weights, k=turn_words)
        middle = len(filler) // 2
        turns.append((" ".join(filler[:middle] + named[target] + filler[middle:]), target))
    return turns


def _time_shape(vault, turns, rows, repeat, checkpoint, *, bounded: bool):
    from exomem import working_set_runtime

    original = working_set_runtime.lexical_term_budget
    if not bounded:
        working_set_runtime.lexical_term_budget = lambda: None
    samples: list[float] = []
    first = 0
    kept: list[int] = []
    dropped: list[int] = []
    try:
        for _ in range(repeat):
            for turn, target in turns:
                selection: dict = {}
                started = time.perf_counter()
                hits, state = working_set_runtime.lexical_evidence(
                    vault,
                    turn,
                    rows,
                    limit=8,
                    recall_checkpoint=checkpoint,
                    selection=selection,
                )
                samples.append((time.perf_counter() - started) * 1000.0)
                if state != "available":
                    raise RuntimeError(f"lexical stage answered {state!r}")
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
        "samples": len(samples),
        "named_page_first": round(first / len(samples), 3),
        "mean_units_kept": round(statistics.mean(kept), 1) if kept else None,
        "mean_units_dropped": round(statistics.mean(dropped), 1) if dropped else None,
    }


def run(pages: int, turns: int, repeat: int, turn_words: int, seed: int) -> dict:
    with scratch_root.scratch_root("exomem-activation-lexical-") as base:
        os.environ["EXOMEM_STATE_ROOT"] = str(base / "state")
        os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(base / "state" / "writer-lease")
        os.environ["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
        from exomem import find as find_module
        from exomem import freshness, lexstore
        from exomem.vault import walk_vault_md

        vault = base / "vault"
        started = time.perf_counter()
        paths, named = build_vault(vault, pages, seed)
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
        cases = build_turns(named, turns, turn_words, seed)
        # Warm both shapes once so neither pays the first connection.
        _time_shape(vault, cases[:1], rows, 1, checkpoint, bounded=False)
        _time_shape(vault, cases[:1], rows, 1, checkpoint, bounded=True)
        report = {
            "pages": len(paths),
            "anchors": len(rows),
            "turns": len(cases),
            "turn_words": turn_words,
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
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(args.pages, args.turns, args.repeat, args.turn_words, args.seed)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(
        f"{report['pages']} pages, {report['anchors']} anchors, {report['turns']} turns "
        f"of ~{report['turn_words']} words (built in {report['build_seconds']}s)\n"
    )
    print(f"{'shape':>10}  {'p50 ms':>8}  {'p95 ms':>8}  {'max ms':>8}  {'named first':>11}  kept/dropped")
    for shape in ("unbounded", "bounded"):
        row = report[shape]
        units = (
            f"{row['mean_units_kept']}/{row['mean_units_dropped']}"
            if row["mean_units_kept"] is not None
            else "-"
        )
        print(
            f"{shape:>10}  {row['p50_ms']:>8}  {row['p95_ms']:>8}  {row['max_ms']:>8}  "
            f"{row['named_page_first']:>11}  {units}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
