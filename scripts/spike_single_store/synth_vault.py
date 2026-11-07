"""Generate a synthetic vault with the same shape as a real one.

The shape file holds only aggregate numbers (page count, size and link
quantiles, folder shares; see vault_shape.py). Text comes from a seeded
pseudo-word vocabulary with a Zipf distribution, so the vault carries no real
content and can run on a public CI runner.

Usage: synth_vault.py <shape.json> <vault_root> [--pages N] [--seed S]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import uuid
from pathlib import Path


def lognormal_from(p50: float, p90: float, rng: random.Random) -> float:
    mu = math.log(p50)
    sigma = (math.log(p90) - mu) / 1.2816
    return math.exp(rng.gauss(mu, sigma))


def make_words(rng: random.Random, n: int = 4000) -> list[str]:
    letters = "abcdefghijklmnoprstuvwy"
    return ["".join(rng.choice(letters) for _ in range(rng.randint(3, 10))) for _ in range(n)]


def text_of(rng: random.Random, words: list[str], weights: list[float], size: int) -> str:
    out, length = [], 0
    while length < size:
        sentence = rng.choices(words, weights=weights, k=rng.randint(6, 18))
        line = " ".join(sentence).capitalize() + "."
        out.append(line)
        length += len(line) + 1
    paras, i = [], 0
    while i < len(out):
        k = rng.randint(2, 5)
        paras.append(" ".join(out[i:i + k]))
        i += k
    return "\n\n".join(paras)


def links_for(rng: random.Random, shape: dict) -> int:
    q = shape["wikilinks"]
    r = rng.random()
    if r < 0.1:
        return 0
    if r < 0.5:
        return rng.randint(0, max(1, int(q["0.5"])))
    if r < 0.9:
        return rng.randint(int(q["0.5"]), max(int(q["0.5"]), int(q["0.9"])))
    return rng.randint(int(q["0.9"]), max(int(q["0.9"]), int(q["0.99"])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("shape", type=Path)
    ap.add_argument("vault", type=Path)
    ap.add_argument("--pages", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    shape = json.loads(args.shape.read_text())
    rng = random.Random(args.seed)
    total = args.pages or shape["pages"]

    from exomem import init

    init.init_vault(args.vault, initialize_state=False)
    kb = args.vault / "Knowledge Base"
    words = make_words(rng)
    weights = [1.0 / (i + 1) for i in range(len(words))]
    n_entities = max(20, int(total * 0.015))
    entities = [" ".join(w.capitalize() for w in rng.sample(words[200:], 2)) for _ in range(n_entities)]
    entities = sorted(set(entities))
    q = shape["bytes"]
    day = "2026-10-01"

    def body(size: int, title: str) -> str:
        text = text_of(rng, words, weights, max(200, size - 300))
        links = [f"[[{rng.choice(entities)}]]" for _ in range(links_for(rng, shape))]
        unit_link = f" relates to {links[0]}" if links else ""
        rest = (" See " + ", ".join(links[1:]) + ".") if len(links) > 1 else ""
        return (f"# {title}\n\n{text}{rest}\n\n## Observations\n\n"
                f"- [operating constraint] {title} holds{unit_link} #synthetic\n")

    def write(rel: str, front: dict, text: str) -> None:
        path = kb / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["---"]
        for key, value in front.items():
            lines.append(f"{key}: {json.dumps(value) if isinstance(value, list) else value}")
        lines.append("---")
        path.write_text("\n".join(lines) + "\n\n" + text, encoding="utf-8")

    for title in entities:
        write(f"Entities/Concepts/{title}.md",
              {"type": "entity", "entity_type": "concept", "status": "active", "created": day,
               "updated": day, "exomem_id": uuid.UUID(int=rng.getrandbits(128))},
              body(int(lognormal_from(q["0.5"], q["0.9"], rng)), title))
    remaining = total - len(entities)
    n_notes = int(remaining * 0.30)
    for i in range(remaining):
        size = int(min(lognormal_from(q["0.5"], q["0.9"], rng), q["0.99"] * 2))
        ident = uuid.UUID(int=rng.getrandbits(128))
        if i < n_notes:
            title = f"Synthetic insight {i:05d} {rng.choice(words)}"
            write(f"Notes/Insights/synthetic-insight-{i:05d}.md",
                  {"type": "insight", "exomem_id": ident, "title": title, "status": "active",
                   "created": day, "updated": day, "tags": []}, body(size, title))
        else:
            title = f"Synthetic article {i:05d} {rng.choice(words)}"
            write(f"Sources/Articles/synthetic-article-{i:05d}.md",
                  {"type": "source", "source_type": "article", "captured": day, "tags": [],
                   "ingested_into": [], "exomem_id": ident}, body(size, title))
    print(json.dumps({"pages": total, "entities": len(entities), "notes": n_notes}))


if __name__ == "__main__":
    main()
