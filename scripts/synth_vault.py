"""Reusable synthetic, densely-wikilinked vault generator for perf benchmarks.

Extracted from `tests/test_graph_lane_perf.py` (which used to carry its own copy)
so three callers generate a byte-identical corpus from ONE place:

- `tests/test_graph_lane_perf.py` — the graph-lane event-maintenance regression.
- `tests/test_latency_gate.py`    — the per-lane latency ceiling gate at scale.
- `scripts/latency_curve.py`      — the latency-vs-corpus-size curve harness.

The generator is pure stdlib (`random` + `pathlib`) — it does NOT import exomem
or torch — so importing it is cheap and side-effect-free from both tests and
scripts. `gen_dense_vault` is deterministic for a fixed `(n, links_per_note,
seed)`: same names, same folders, same link topology every run, so a benchmark
number is reproducible and a regression is attributable to code, not corpus jitter.

Why "dense": the graph lane resolves every `[[wikilink]]` on strong candidates
through the whole-vault `WikilinkResolver`. A realistic cross-linked corpus
(~25 links/note, a mix of full-path and bare-stem link forms) is what makes a
resolver-rebuild regression show up as a real per-query cost at scale, instead of
staying hidden the way a 10-file fixture did.
"""

from __future__ import annotations

import random
from pathlib import Path

# KB sub-folders the synthetic notes are spread across — a realistic slice of the
# page-type tree so the walk/parse cost resembles a real vault's shape.
FOLDERS: tuple[str, ...] = (
    "Notes/Insights", "Notes/Patterns", "Notes/Failures",
    "Entities/Concepts", "Entities/People", "Sources", "Experiments",
)


def gen_dense_vault(
    root: Path, n: int, links_per_note: int = 25, seed: int = 7
) -> list[str]:
    """Write `n` densely cross-linked KB notes under `root`; return their rels.

    Each note gets `links_per_note` outbound `[[wikilinks]]` to random other
    notes, alternating between full-path and bare-stem link forms so the resolver
    exercises both resolution paths. Returns the vault-relative paths (with the
    leading ``Knowledge Base/`` and trailing ``.md``) in creation order, so a
    caller can address specific notes (e.g. to edit/rename/delete one).

    Deterministic in `(n, links_per_note, seed)`. Writes only under
    ``root/Knowledge Base/`` — the caller owns `root` (typically a tmp dir).
    """
    rng = random.Random(seed)
    kb = root / "Knowledge Base"
    for f in FOLDERS:
        (kb / f).mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    names: list[str] = []
    for i in range(n):
        folder = FOLDERS[i % len(FOLDERS)]
        name = f"note-{i:05d}-topic-{rng.randint(0, 99999)}"
        names.append(name)
        rels.append(f"Knowledge Base/{folder}/{name}.md")
    for i, rel in enumerate(rels):
        targets = rng.sample(range(n), min(links_per_note, n))
        link_lines = []
        for t in targets:
            if t % 3 == 0:  # mix full-path and bare-stem link forms
                link_lines.append(f"- see [[{rels[t][:-3]}]] for context")
            else:
                link_lines.append(f"- ref [[{names[t]}]] inline")
        (root / rel).write_text(
            "---\n"
            "type: insight\n"
            f"title: Note {i} about topic {names[i]}\n"
            "tags: [synthetic, graph, dense]\n"
            f"updated: 2026-02-{(i % 28) + 1:02d}\n"
            "---\n\n"
            f"# Note {i}\n\n"
            "Prose paragraph so the note is realistically sized and body text "
            "gives BM25 something to rank on. topic topic topic.\n\n"
            "## Related\n\n" + "\n".join(link_lines) + "\n",
            encoding="utf-8",
        )
    return rels
