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
    # A vault that init already laid down has its own schema folder; a bare root gets the minimum.
    if not (root / KB / "_Schema" / "SKILL.md").exists():
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


# Scope ids are invented, in the form a policy file requires.
SCOPE_AREA = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
SCOPE_PROJECT = "01ARZ3NDEKTSV4RRFFQ69G5FA2"
SCOPE_RENAMED = "01ARZ3NDEKTSV4RRFFQ69G5FA3"
SCOPE_PARTITION = "01ARZ3NDEKTSV4RRFFQ69G5FA4"
SCOPE_MEDIA = "01ARZ3NDEKTSV4RRFFQ69G5FA5"
RENAMED_REF = f"{KB}/Notes/Insights/rust-go.md"
RENAMED_GLOB = f"{KB}/Notes/Insights/dup-*"
CLIP_FRAME = f"{VIDEO}.frames/frame-001.md"
TALK = f"{KB}/Sources/Media/talk.mp4"
GHOST = f"{KB}/Sources/Media/ghost.mp4"


def _scope(root: Path, name: str, scope_id: str, selectors: str) -> None:
    text = f"governance_version: 1\nid: {scope_id}\nname: {name}\n{selectors}"
    _write(root, f"{KB}/_Governance/scopes/{name}.yaml", text)


def add_governed_content(root: Path) -> None:
    """Add scopes, an access file and the pages they select to the vault that `build_nested_vault` wrote.

    Five scopes each cover a case: a compiled area by path, a project with an `exclude_paths`
    value, a path ref and a file-name glob that name pages the plan renames, a glob over a
    partition folder (it changes only if the partition folders flatten), and a tag that a video
    page carries and its frames inherit. Three videos cover the inheritance edge: `clip` keeps
    its page name, `talk` has a page that moves to another area, and `ghost` has a frame whose
    pointer names a video file that does not exist.
    """
    _scope(
        root,
        "area",
        SCOPE_AREA,
        f'paths: ["{KB}/Notes/Insights/**"]\nexclude:\n  paths: ["{KB}/Notes/Insights/Why churn rose.md"]\n',
    )
    _scope(
        root,
        "project",
        SCOPE_PROJECT,
        f'projects: [pricing]\nexclude:\n  paths: ["{KB}/Evidence/**"]\n',
    )
    _scope(root, "renamed", SCOPE_RENAMED, f'refs: ["{RENAMED_REF}"]\npaths: ["{RENAMED_GLOB}"]\n')
    _scope(root, "partition", SCOPE_PARTITION, f'paths: ["{KB}/Notes/Research/Pricing/*"]\n')
    _scope(
        root,
        "media",
        SCOPE_MEDIA,
        f'tags: [media-restricted]\npaths: ["{KB}/Sources/Media/*.mp4"]\n',
    )
    _write(
        root,
        f"{KB}/_access.yaml",
        yaml.safe_dump(
            {
                "readonly": ["Notes/Insights/hidden.md", "Notes/Failures"],
                "excluded": ["Notes/Productions"],
            }
        ),
    )

    _page(
        root,
        f"{KB}/Notes/Research/Pricing/pricing-model.md",
        {"type": "research-note", "title": "Pricing model", "project": "pricing"},
    )
    _page(
        root,
        f"{KB}/Evidence/Pricing/c/Receipt.md",
        {"type": "evidence", "title": "Receipt", "project": "pricing"},
    )
    _page(root, f"{VIDEO}.md", {"tags": ["media-restricted"]})
    _write(root, TALK, b"video")
    _page(root, f"{TALK}.md", {"type": "source", "title": "Talk", "tags": ["media-restricted"]})
    _page(
        root,
        f"{TALK}.frames/frame-001.md",
        {"title": "Talk frame", "parent_media": TALK, "frame_ts": 2.5},
    )
    _page(root, f"{GHOST}.md", {"tags": ["media-restricted"]})
    _page(
        root,
        f"{GHOST}.frames/frame-001.md",
        {"title": "Ghost frame", "parent_media": GHOST, "frame_ts": 3.5},
    )


RECORD_PAGE = f"{KB}/Records/Receipts/receipt-001.md"
RETIRED_LINKER = f"{KB}/Notes/Insights/retired-pointer.md"
RETIRED_TARGET = f"{KB}/Records/Receipts/receipt-gone"


def add_links_from_collection_and_part_pages(root: Path) -> None:
    """Add the pages that the collection store and the part writer own, each with a link the plan would edit.

    The record page also links to a collection page that does not exist, and so does an ordinary
    page: neither link can be repointed, and neither is an unresolved link in the usual sense.
    """
    with (root / PART_FOLDER / "part-02.md").open("a", encoding="utf-8") as part:
        part.write("See [[Knowledge Base/Notes/Insights/Why churn rose]].\n")
    _page(
        root,
        RECORD_PAGE,
        {"type": "record", "title": "Receipt 001"},
        f"Filed after [[Knowledge Base/Notes/Insights/Why churn rose]] and [[{RETIRED_TARGET}]].\n",
    )
    _page(
        root,
        RETIRED_LINKER,
        {"type": "insight", "title": "Retired pointer"},
        f"The receipt at [[{RETIRED_TARGET}]] was retired.\n",
    )


CHART = f"{KB}/Notes/Insights/chart.png"
CHART_LINKER = f"{KB}/Notes/Insights/chart-notes.md"


def add_attachment_links(root: Path) -> None:
    """Add an image below an area and a page that embeds it by its full path and by its bare name.

    Obsidian resolves both embeds today. The relayout moves the image and the page, so the
    full-path embed breaks unless the plan rewrites it. The page also links to a Markdown page
    whose title holds a dot, which reads like a file name and must still resolve as a page.
    """
    _write(root, CHART, b"png")
    _page(
        root,
        f"{KB}/Notes/Insights/release-notes.md",
        {"type": "insight", "title": "Release 1.2 notes"},
    )
    _page(
        root,
        CHART_LINKER,
        {"type": "insight", "title": "Chart notes"},
        "![[Notes/Insights/chart.png]] and ![[chart.png]] and [[Release 1.2 notes]].\n",
    )


BUNDLE = f"{KB}/Evidence/Pricing/bundle/pack.asice"
MEMBER_ARTIFACT = f"{BUNDLE}.documents/{'ab' * 32}.pdf"
MEMO = f"{KB}/Evidence/Pricing/b/memo.pdf"


def add_container_member_and_companion(root: Path) -> None:
    """Add a document container with a single member, and a binary that a companion page gives scopes.

    The member page holds the binding that names its parent, the parent's page and its own
    artifact by vault path. The memo's page holds a companion descriptor that names the memo.
    """
    _write(root, BUNDLE, b"zip")
    _page(root, f"{BUNDLE}.md", {"type": "evidence", "title": "Pack"})
    _write(root, MEMBER_ARTIFACT, b"pdf")
    _page(
        root,
        f"{MEMBER_ARTIFACT}.md",
        {
            "container_member": {
                "version": 1,
                "parent_path": BUNDLE,
                "parent_page": f"{BUNDLE}.md",
                "parent_sha256": "cd" * 32,
                "parent_size": 3,
                "artifact_path": MEMBER_ARTIFACT,
                "artifact_sha256": "ef" * 32,
                "artifact_size": 3,
                "member_path": "docs/terms.pdf",
            }
        },
    )
    _write(root, MEMO, b"pdf")
    _page(
        root,
        f"{MEMO}.md",
        {"type": "evidence", "title": "Memo", "governance_companion": {"artifact_path": MEMO}},
    )


REEL = f"{KB}/Sources/Media/reel.mp4"
REEL_FRAMES = tuple(f"scene-{index:03d}-t{ms}ms.jpg" for index, ms in enumerate((0, 4500)))


def add_renamed_video(root: Path) -> None:
    """Add a video whose page a title renames, and two scene frames in the folder that its file name owns.

    A frame sits in `<video file name>.frames/` and names the video file in `parent_media`; the
    access kernel reads the frame's video page as that value plus `.md`. The frame pages are typed
    like the pages that the sidecar writer produces, so a title rule would rename them if the plan let it.
    """
    _write(root, REEL, b"video")
    _page(
        root,
        f"{REEL}.md",
        {"type": "source", "title": "Reel highlights", "tags": ["media-restricted"]},
    )
    for index, name in enumerate(REEL_FRAMES):
        _write(root, f"{REEL}.frames/{name}", b"jpg")
        _page(
            root,
            f"{REEL}.frames/{name}.md",
            {
                "type": "source",
                "title": f"Evidence: {name}",
                "parent_media": REEL,
                "frame_ts": index * 4.5,
            },
        )


RATE_ORIGINAL = f"{KB}/Evidence/Pricing/a/rate-notes.md"
RATE_PAGE = f"{RATE_ORIGINAL}.md"
RATE_LINKER = f"{KB}/Notes/Insights/rate-linker.md"


def add_markdown_original_with_links(root: Path) -> None:
    """Add a Markdown original kept as evidence, the page that describes it, and a page that links to both.

    The page's file name is the original's name plus `.md`, so it ends in `.md.md`. The plan
    leaves it untitled, so it keeps that name and moves with its folder. Obsidian reads `[[rate-notes.md]]`
    as the original and needs `[[rate-notes.md.md]]` for the page.
    """
    _write(root, RATE_ORIGINAL, "Rates agreed in the call.\n")
    _page(root, RATE_PAGE, {"type": "evidence"})
    _page(
        root,
        RATE_LINKER,
        {"type": "insight", "title": "Rate linker"},
        f"[[{RATE_ORIGINAL}]] and [[{RATE_PAGE}]] and [[rate-notes.md.md]] and [[Evidence/Pricing/a/rate-notes]].\n",
    )
