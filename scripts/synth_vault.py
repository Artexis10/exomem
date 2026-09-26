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
import uuid
from pathlib import Path

# KB sub-folders the synthetic notes are spread across — a realistic slice of the
# page-type tree so the walk/parse cost resembles a real vault's shape.
FOLDERS: tuple[str, ...] = (
    "Notes/Insights", "Notes/Patterns", "Notes/Failures",
    "Entities/Concepts", "Entities/People", "Sources", "Experiments",
)


#: Namespace for the deterministic note ids `gen_dense_vault(ids=True)` writes.
_NOTE_IDS = uuid.UUID("2d0b6c1e-8f4a-4c3e-9b1d-7a5e3f2c1b0d")


def gen_dense_vault(
    root: Path,
    n: int,
    links_per_note: int = 25,
    seed: int = 7,
    *,
    sources: int = 0,
    relations: bool = False,
    ids: bool = False,
) -> list[str]:
    """Write `n` densely cross-linked KB notes under `root`; return their rels.

    Each note gets `links_per_note` outbound `[[wikilinks]]` to random other
    notes, alternating between full-path and bare-stem link forms so the resolver
    exercises both resolution paths. Returns the vault-relative paths (with the
    leading ``Knowledge Base/`` and trailing ``.md``) in creation order, so a
    caller can address specific notes (e.g. to edit/rename/delete one).

    Deterministic in `(n, links_per_note, seed)`. Writes only under
    ``root/Knowledge Base/`` — the caller owns `root` (typically a tmp dir).

    Three opt-in shapes real vaults have, off by default so every existing
    caller writes byte-identical notes: `sources=k` writes k Source pages and
    has note i cite ``Sources/report-(i % k)`` (one-Source clusters of n/k
    notes); `relations` adds a ``## Relations`` line to the next note; `ids`
    gives every note a deterministic ``exomem_id``.
    """
    rng = random.Random(seed)
    kb = root / "Knowledge Base"
    for f in FOLDERS:
        (kb / f).mkdir(parents=True, exist_ok=True)
    if sources:
        (kb / "Sources").mkdir(parents=True, exist_ok=True)
        for s in range(sources):
            (kb / "Sources" / f"report-{s:03d}.md").write_text(
                "---\n"
                "type: source\n"
                f"title: Report {s:03d}\n"
                "captured: 2026-01-15\n"
                "---\n\n"
                f"# Report {s:03d}\n\nRaw notes from the field.\n",
                encoding="utf-8",
            )
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
        identity = f"exomem_id: {uuid.uuid5(_NOTE_IDS, rel)}\n" if ids else ""
        cited = f'sources:\n  - "[[Sources/report-{i % sources:03d}]]"\n' if sources else ""
        authored = (
            f"\n## Relations\n\n- relates_to [[{rels[(i + 1) % n][:-3]}]]\n" if relations else ""
        )
        (root / rel).write_text(
            "---\n"
            f"{identity}"
            "type: insight\n"
            f"title: Note {i} about topic {names[i]}\n"
            "tags: [synthetic, graph, dense]\n"
            f"updated: 2026-02-{(i % 28) + 1:02d}\n"
            f"{cited}"
            "---\n\n"
            f"# Note {i}\n\n"
            "Prose paragraph so the note is realistically sized and body text "
            "gives BM25 something to rank on. topic topic topic.\n\n"
            "## Related\n\n" + "\n".join(link_lines) + "\n" + authored,
            encoding="utf-8",
        )
    return rels


def gen_entity_overlay(root: Path, n_entities: int, seed: int = 17) -> list[str]:
    """Add deterministic synthetic person entities without changing dense fixtures."""
    rng = random.Random(seed)
    folder = root / "Knowledge Base" / "Entities" / "People"
    folder.mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    for index in range(n_entities):
        token = rng.randint(0, 999999)
        rel = f"Knowledge Base/Entities/People/synthetic-person-{index:05d}-{token:06d}.md"
        (root / rel).write_text(
            "---\n"
            "type: entity\n"
            f"title: Synthetic Person {index:05d}\n"
            "entity_type: person\n"
            "status: active\n"
            "relationship: friend\n"
            "tags: [synthetic, scale]\n"
            "updated: 2026-02-01\n"
            "---\n\n"
            f"# Synthetic Person {index:05d}\n\n"
            "A deterministic synthetic identity used only for scale measurement.\n",
            encoding="utf-8",
        )
        rels.append(rel)
    return rels
