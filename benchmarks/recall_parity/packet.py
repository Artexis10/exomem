"""Pool both arms' top results into a blinded judge packet and a separate arm key.

A judge sees one line per distinct (query, document) pair: the query, the
document's title and path, and its snippet. A document both arms returned is
one pair, so no line says which arm produced it. Pair ids are opaque hashes
and the lines are shuffled with the seed. The arm key, which maps each pair id
back to its case, document and the rank each arm gave it, lives in a separate
directory that no judge reads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from membench.judge.blinding import shuffled

from . import private
from .capture import capture_path

PACKET_SCHEMA = "recall-parity/packet/1"
KEY_SCHEMA = "recall-parity/arm-key/1"
DEFAULT_K = 10


def pair_id(seed: int, case: str, document: str) -> str:
    material = f"{seed}\0pair\0{case}\0{document}"
    return "p-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_packet(root: Path, run_name: str, *, k: int) -> dict[str, Any]:
    """Write ``judge/packet.jsonl`` and ``key/arm-key.json``; return counts only."""

    run_dir = root / "runs" / run_name
    manifest = private.read_json(run_dir / "manifest.json")
    seed = manifest["sample"]["seed"]
    arms = [arm["name"] for arm in manifest["arms"]]
    cases_path = root / "sample" / "cases.jsonl"
    if hashlib.sha256(cases_path.read_bytes()).hexdigest() != manifest["cases_sha256"]:
        raise ValueError("the sample's cases file changed after this run was captured")
    queries = {case["case_id"]: case["query"] for case in private.read_jsonl(cases_path)}
    captured = {
        arm: {row["case_id"]: row for row in private.read_jsonl(capture_path(run_dir, arm))}
        for arm in arms
    }

    pool: dict[tuple[str, str], dict[str, Any]] = {}
    for case in manifest["case_ids"]:
        for arm in arms:
            row = captured[arm].get(case)
            if row is None or not row["ok"]:
                continue
            for result in row["results"][:k]:
                entry = pool.setdefault(
                    (case, result["id"]), {"titles": set(), "snippets": set(), "ranks": {}}
                )
                entry["ranks"][arm] = result["rank"]
                if result["title"]:
                    entry["titles"].add(result["title"])
                if result["snippet"]:
                    entry["snippets"].add(result["snippet"])

    lines: list[dict[str, Any]] = []
    key: dict[str, dict[str, Any]] = {}
    for (case, document), entry in sorted(pool.items()):
        identifier = pair_id(seed, case, document)
        if identifier in key:
            raise ValueError("pair id collision; change the seed")
        key[identifier] = {"case_id": case, "id": document, "ranks": entry["ranks"]}
        lines.append(
            {
                "pair_id": identifier,
                "query": queries[case],
                "title": min(entry["titles"]) if entry["titles"] else None,
                "path": document,
                # Ordered by content hash, so the order cannot reveal an arm.
                "snippets": sorted(
                    entry["snippets"], key=lambda text: hashlib.sha256(text.encode()).hexdigest()
                ),
            }
        )
    private.write_jsonl(run_dir / "judge" / "packet.jsonl", shuffled(lines, f"{seed}:packet"))
    private.write_json(
        run_dir / "key" / "arm-key.json",
        {"schema": KEY_SCHEMA, "k": k, "seed": seed, "arms": arms, "pairs": key},
    )
    return {
        "schema": PACKET_SCHEMA,
        "cases": len(manifest["case_ids"]),
        "pairs": len(lines),
        "pairs_returned_by_both_arms": sum(
            len(item["ranks"]) == len(arms) for item in key.values()
        ),
        "pairs_returned_by_one_arm": {
            arm: sum(set(item["ranks"]) == {arm} for item in key.values()) for arm in arms
        },
    }
