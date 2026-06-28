"""Mining the relevance-pairs snapshot is idempotent (torch-free).

`mine_pairs` rewrites `relevance_pairs.jsonl` as an atomic deduped snapshot, so
re-running over the same source logs must be byte-identical and leave no temp
residue — the property that makes it safe to run on a schedule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import derive_relevance_pairs as drp  # noqa: E402


def _seed_logs(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    queries = [
        {
            "ts": "2026-06-01T10:00:00",
            "query": "metabolism basics",
            "top_k": [
                {"path": "Knowledge Base/Notes/Research/Health/metabolic-literacy.md"},
                {"path": "Knowledge Base/Sources/Articles/some-source.md"},
            ],
        },
        {
            "ts": "2026-06-02T09:00:00",
            "query": "binding problem",
            "top_k": [
                {"path": "Knowledge Base/Notes/Insights/binding-mechanism.md"},
            ],
        },
    ]
    writes = [
        # Cites a path from query 1, within window → one pair.
        {
            "ts": "2026-06-01T10:30:00",
            "tool": "note",
            "written_path": "Knowledge Base/Notes/Insights/foo.md",
            "cited_sources": ["Knowledge Base/Notes/Research/Health/metabolic-literacy.md"],
        },
        # A SECOND write citing the SAME (query, path) later → must dedup to one row.
        {
            "ts": "2026-06-01T11:00:00",
            "tool": "note",
            "written_path": "Knowledge Base/Notes/Insights/bar.md",
            "cited_sources": ["Knowledge Base/Notes/Research/Health/metabolic-literacy.md"],
        },
        # Cites a path from query 2 → a second distinct pair.
        {
            "ts": "2026-06-02T09:20:00",
            "tool": "note",
            "written_path": "Knowledge Base/Notes/Insights/baz.md",
            "cited_sources": ["Knowledge Base/Notes/Insights/binding-mechanism.md"],
        },
    ]
    (logs_dir / "queries.jsonl").write_text(
        "\n".join(json.dumps(q) for q in queries) + "\n", encoding="utf-8"
    )
    (logs_dir / "writes.jsonl").write_text(
        "\n".join(json.dumps(w) for w in writes) + "\n", encoding="utf-8"
    )


def test_snapshot_is_idempotent_and_deduped(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _seed_logs(logs)
    window = 2 * 3600

    pairs1 = drp.mine_pairs(window, write=True, logs_dir=logs)
    snapshot = logs / "relevance_pairs.jsonl"
    bytes1 = snapshot.read_bytes()

    pairs2 = drp.mine_pairs(window, write=True, logs_dir=logs)
    bytes2 = snapshot.read_bytes()

    # Idempotent: a second run over the same logs reproduces the file exactly.
    assert bytes1 == bytes2
    assert pairs1 == pairs2

    rows = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines() if line]
    # Two distinct (query, cited_path) pairs, deduped across the two citing writes.
    keys = {(r["query"], r["cited_path"]) for r in rows}
    assert len(rows) == len(keys) == 2

    # No temp residue from the atomic write.
    assert not list(logs.glob(".relevance_pairs.jsonl.*"))
    assert not list(logs.glob("*.tmp"))
