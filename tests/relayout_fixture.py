"""A generated nested vault that holds every case a relayout plan must list."""

from __future__ import annotations

from pathlib import Path

import yaml

KB = "Knowledge Base"
LONG_TITLE = "é" * 110  # 220 bytes, over the 200-byte file name cap
PART_SOURCE = f"{KB}/Sources/Articles/2026-05-02-long-article.md"
PART_FOLDER = f"{KB}/Sources/Articles/.2026-05-02-long-article.parts"
VIDEO = f"{KB}/Sources/Media/clip.mp4"


def _write(root: Path, rel: str, text: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def _page(root: Path, rel: str, frontmatter: dict | None, body: str = "") -> None:
    head = ""
    if frontmatter is not None:
        head = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n"
    _write(root, rel, head + body)


def build_nested_vault(root: Path) -> None:
    """Write the fixture below `root`. Names are slugs that the relayout turns into titles."""
    _write(root, f"{KB}/_Schema/SKILL.md", "")
    _write(root, f"{KB}/.obsidian/app.json", "{}")

    # Compiled pages in partition folders, homonyms and trouble titles.
    _page(
        root,
        f"{KB}/Notes/Insights/Why churn rose.md",
        {"type": "insight", "title": "Why churn rose"},
        "# Why churn rose\n\n## Findings\n",
    )
    _page(
        root,
        f"{KB}/_trash/Notes/Insights/Why churn rose.md",
        {"type": "insight", "title": "Why churn rose"},
    )
    _page(
        root,
        f"{KB}/Notes/Research/Pricing/pricing-experiments.md",
        {"type": "research-note", "title": "Pricing experiments"},
    )
    _page(
        root,
        f"{KB}/Notes/Productions/Video/launch-clip.md",
        {"type": "production-log", "title": "Launch clip"},
    )
    _page(root, f"{KB}/Notes/Insights/rust-go.md", {"type": "insight", "title": "Rust # Go notes"})
    _page(root, f"{KB}/Notes/Insights/hidden.md", {"type": "insight", "title": ".Hidden idea"})
    _page(root, f"{KB}/Notes/Insights/long-a.md", {"type": "insight", "title": LONG_TITLE})
    _page(root, f"{KB}/Notes/Insights/long-b.md", {"type": "insight", "title": LONG_TITLE})
    _page(root, f"{KB}/Notes/Insights/dup-1.md", {"type": "insight", "title": "Duplicate title"})
    _page(root, f"{KB}/Notes/Failures/dup-2.md", {"type": "failure", "title": "Duplicate title"})
    _page(root, f"{KB}/Notes/Misc stuff/Idea.md", None, "An idea in a folder that no type owns.\n")

    # Untyped pages keep their names; the first sits at the root of the managed folder.
    _page(root, f"{KB}/Scratch.md", None, "Root-level page.\n")
    _page(root, f"{KB}/Misc/Scratch.md", None, "Same name, a folder down.\n")

    # A page whose links cover every outcome. The path mention is plain text that a link edit cannot reach.
    _page(
        root,
        f"{KB}/Notes/Procedures/link-lab.md",
        {"type": "procedure", "title": "Link lab"},
        "\n".join(
            [
                "[[Knowledge Base/Notes/Insights/Why churn rose]]",
                "[[Why churn rose#Findings|churn]]",
                "[[Scan]] [[Missing page]] [[Knowledge Base/Notes/Procedures/]] [[Pricing/a/Scan]]",
                "`[[Knowledge Base/Notes/Insights/Why churn rose]]` is code.",
                "[[Pricing benchmarks]] [[Misc/Scratch]] [[Knowledge Base/Scratch]]",
                "Plain mention: Knowledge Base/Notes/Insights/Why churn rose.md",
                "",
            ]
        ),
    )

    # Evidence keeps the name of its original, so two scans share a name.
    _page(
        root,
        f"{KB}/Evidence/Pricing/a/Scan.md",
        {
            "type": "evidence",
            "title": "Scan A",
            "evidence_file": f"{KB}/Evidence/Pricing/a/scan.pdf",
            "ingested_into": [f"[[{KB}/Notes/Research/Pricing/pricing-experiments]]"],
        },
        "Cited in [[Knowledge Base/Notes/Insights/Why churn rose]].\n",
    )
    _write(root, f"{KB}/Evidence/Pricing/a/scan.pdf", b"%PDF-1.4 a")
    _page(root, f"{KB}/Evidence/Pricing/b/Scan.md", {"type": "evidence", "title": "Scan B"})

    # A source that has a further part, and a video with a frame.
    _page(
        root,
        PART_SOURCE,
        {
            "type": "source",
            "title": "Long article",
            "extraction_parts": 2,
            "next_part": ".2026-05-02-long-article.parts/part-02.md",
        },
        "First part.\n",
    )
    _page(
        root,
        f"{PART_FOLDER}/part-02.md",
        {
            "type": "source",
            "title": "Part 2 of 2: Long article",
            "part_of": "../2026-05-02-long-article.md",
            "part": 2,
            "parts": 2,
        },
        "Second part.\n",
    )
    _write(root, VIDEO, b"video")
    _page(
        root,
        f"{VIDEO}.frames/frame-001.md",
        {"title": "Frame 1", "parent_media": VIDEO, "frame_ts": 1.5},
    )
    _write(root, f"{VIDEO}.frames/frame-001.jpg", b"jpeg")

    # Content outside the managed folder: a folder that links in, and a folder that takes a new top-level name.
    _page(
        root,
        "Reference/Pricing benchmarks.md",
        None,
        "[[Knowledge Base/Notes/Insights/Why churn rose]] and [[Why churn rose]] and [[Pricing experiments]]\n",
    )
    _write(root, "Reference/data.csv", "a,b\n")
    _page(
        root, "Insights/Idea.md", None, "A folder that the relayout's Insights area would join.\n"
    )
