"""Pool both arms' top results into a blinded judge packet and a separate arm key.

A judge sees one line per distinct (query, document) pair: the query, the
document's path, its title and a leading slice of its body. Title and body come
from one read-only source vault, never from either arm's answer, so both arms'
copies of a document look the same and a pair says nothing about which arm
returned it. A path the vault does not hold is shown as missing and counted.
Pair ids are opaque hashes and the lines are shuffled with the seed. The arm
key, which maps each pair id back to its case, document and the rank each arm
gave it, lives in a separate directory that no judge reads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from membench.judge.blinding import shuffled

from exomem.vault import parse_frontmatter

from . import private
from .capture import capture_path

PACKET_SCHEMA = "recall-parity/packet/2"
KEY_SCHEMA = "recall-parity/arm-key/1"
DEFAULT_K = 10
#: How much of a document's body, after its frontmatter, the judge sees.
BODY_CHARS = 1500
#: A read stops here, so a huge file named by a result cannot fill memory.
MAX_READ_BYTES = 1 << 20


def pair_id(seed: int, case: str, document: str) -> str:
    material = f"{seed}\0pair\0{case}\0{document}"
    return "p-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def read_document(vault: Path, path: str) -> str | None:
    """The text of the vault file at ``path``; ``None`` when the vault has no such file.

    ``vault`` is a resolved directory. The path comes from an arm's answer, so it
    is not trusted: a path that resolves outside the vault (absolute, ``..`` or a
    symlink out) is refused. The file is opened for reading only.
    """

    target = (vault / path).resolve()
    if not target.is_relative_to(vault):
        raise ValueError("a captured result path leaves the source vault")
    if not target.is_file():
        return None
    with target.open("rb") as handle:
        return handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")


def document_view(vault: Path, path: str) -> dict[str, Any]:
    """The one view of a document every arm's copy of it gets.

    Title is the frontmatter ``title``, else the file name without its suffix. Body
    is the text after the frontmatter, cut to ``BODY_CHARS``.
    """

    text = read_document(vault, path)
    if text is None:
        return {"title": None, "body": None, "body_truncated": False, "missing": True}
    frontmatter, body, _ = parse_frontmatter(text)
    title = frontmatter.get("title")
    body = body.strip()
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else Path(path).stem,
        "body": body[:BODY_CHARS].rstrip(),
        "body_truncated": len(body) > BODY_CHARS,
        "missing": False,
    }


def build_packet(root: Path, run_name: str, *, k: int, vault: Path) -> dict[str, Any]:
    """Write ``judge/packet.jsonl`` and ``key/``; return counts only.

    ``vault`` is the source vault the judge's view of each document is read from.
    It is only read. Nothing is written until every pair has its view.
    """

    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError("--vault must be an existing directory")
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

    ranks: dict[tuple[str, str], dict[str, int]] = {}
    for case in manifest["case_ids"]:
        for arm in arms:
            row = captured[arm].get(case)
            if row is None or not row["ok"]:
                continue
            for result in row["results"][:k]:
                ranks.setdefault((case, result["id"]), {})[arm] = result["rank"]

    views: dict[str, dict[str, Any]] = {}
    lines: list[dict[str, Any]] = []
    key: dict[str, dict[str, Any]] = {}
    for (case, document), pair_ranks in sorted(ranks.items()):
        identifier = pair_id(seed, case, document)
        if identifier in key:
            raise ValueError("pair id collision; change the seed")
        if document not in views:
            try:
                views[document] = document_view(vault, document)
            except ValueError as error:
                raise ValueError(f"{error} (case {case})") from None
        key[identifier] = {"case_id": case, "id": document, "ranks": pair_ranks}
        lines.append(
            {"pair_id": identifier, "query": queries[case], "path": document, **views[document]}
        )
    private.write_jsonl(run_dir / "judge" / "packet.jsonl", shuffled(lines, f"{seed}:packet"))
    private.write_json(
        run_dir / "key" / "arm-key.json",
        {"schema": KEY_SCHEMA, "k": k, "seed": seed, "arms": arms, "pairs": key},
    )
    absent = [item for item in key.values() if views[item["id"]]["missing"]]
    counts = {
        "schema": PACKET_SCHEMA,
        "cases": len(manifest["case_ids"]),
        "pairs": len(lines),
        "pairs_returned_by_both_arms": sum(
            len(item["ranks"]) == len(arms) for item in key.values()
        ),
        "pairs_returned_by_one_arm": {
            arm: sum(set(item["ranks"]) == {arm} for item in key.values()) for arm in arms
        },
        "view": {
            "body_chars": BODY_CHARS,
            "pairs_missing_from_vault": len(absent),
            "pairs_missing_from_vault_returned_by": {
                arm: sum(arm in item["ranks"] for item in absent) for arm in arms
            },
        },
    }
    private.write_json(run_dir / "key" / "packet-manifest.json", counts)
    return counts
