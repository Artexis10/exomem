"""Build a deterministic write stream from a vault copy.

The mix follows the personal service's observed mutations (mostly new pages,
then edits to existing notes). Bodies are sampled from real pages so sizes and
link density match the vault; each op carries a unique token and one wikilink
to an existing entity page, which is what the visibility checks look for.

Usage: make_stream.py <vault> <out.jsonl> <count> [--seed N] [--create-share F]
The output holds vault text, so it stays in the session scratch directory.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def body_of(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            return parts[1].lstrip("-\n")
    return text


def last_unique_line(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if text.count(last) != 1 or len(last) < 12 or last.startswith("---"):
        return None
    return last


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("vault", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("count", type=int)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--create-share", type=float, default=0.65)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    kb = args.vault / "Knowledge Base"

    sources = sorted(p for p in (kb / "Sources").rglob("*.md")) + sorted(
        p for p in (kb / "Notes").rglob("*.md"))
    entities = sorted(p.stem for p in (kb / "Entities").rglob("*.md")
                      if p.stem not in ("index", "log") and not p.stem.startswith("_"))
    editable = []
    for p in sorted((kb / "Notes").rglob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if last_unique_line(text):
            editable.append(p.relative_to(args.vault).as_posix())

    created = 0
    with args.out.open("w", encoding="utf-8") as out:
        for n in range(args.count):
            token = f"zqspike{n:04d}{rng.getrandbits(24):06x}"
            link = rng.choice(entities)
            if created == 0 or rng.random() < args.create_share:
                src = rng.choice(sources)
                body = body_of(src.read_text(encoding="utf-8", errors="replace")).strip()
                # A compiled note needs one semantic unit; the stream marker is it.
                body = body.replace("## Observations", "## Earlier observations")
                op = {
                    "n": n, "op": "create", "token": token, "link": link,
                    "title": f"Spike stream note {n:04d} {token}",
                    "content": (f"{body}\n\n## Observations\n\n- [operating constraint] Stream "
                                f"marker {token} relates to [[{link}]] #spike\n"),
                    "note_type": "insight",
                }
                created += 1
            elif rng.random() < 0.5:
                op = {"n": n, "op": "edit", "token": token, "link": link,
                      "target": {"stream_create": rng.randrange(created)}}
            else:
                op = {"n": n, "op": "edit", "token": token, "link": link,
                      "target": {"vault_path": rng.choice(editable)}}
            out.write(json.dumps(op, ensure_ascii=False) + "\n")
    print(json.dumps({"ops": args.count, "creates": created, "entities": len(entities),
                      "editable_notes": len(editable)}))


if __name__ == "__main__":
    main()
