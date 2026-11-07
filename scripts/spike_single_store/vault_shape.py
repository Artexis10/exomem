"""Summarise a vault's page shape so a synthetic vault can match it.

Prints only aggregate numbers (no titles, paths or text), so the output is
safe to publish. Link counting is a shape estimate for the generator, not a
parser: the benchmark itself parses pages with Exomem's own functions.
"""
import json, re, statistics, sys
from pathlib import Path

# Shape estimate only (counts [[...]] spans); see the module docstring.
WIKILINK = re.compile(r"\[\[[^\]\n]+\]\]")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def main(root: Path) -> None:
    kb = root / "Knowledge Base"
    sizes, links, fm_lines, depth, top = [], [], [], [], {}
    for p in kb.rglob("*.md"):
        rel = p.relative_to(kb)
        if rel.parts[0].startswith("_"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        sizes.append(len(text.encode()))
        links.append(len(WIKILINK.findall(text)))
        fm = text.split("\n---", 1)[0].count("\n") if text.startswith("---") else 0
        fm_lines.append(fm)
        depth.append(len(rel.parts))
        top[rel.parts[0]] = top.get(rel.parts[0], 0) + 1
    out = {
        "pages": len(sizes),
        "bytes": {q: pct(sizes, q) for q in (0.1, 0.5, 0.9, 0.99)} | {"mean": int(statistics.mean(sizes))},
        "wikilinks": {q: pct(links, q) for q in (0.1, 0.5, 0.9, 0.99)} | {"mean": round(statistics.mean(links), 2)},
        "frontmatter_lines": {q: pct(fm_lines, q) for q in (0.5, 0.9)},
        "depth": {q: pct(depth, q) for q in (0.5, 0.9)},
        "top_folder_share": {k: round(v / len(sizes), 3) for k, v in sorted(top.items(), key=lambda kv: -kv[1])},
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
