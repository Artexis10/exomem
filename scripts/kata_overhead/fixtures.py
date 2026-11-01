"""Synthetic fixtures for the gVisor overhead measurement (diagnostic branch only).

Pure stdlib. Everything here is invented text: a vault of notes for recall and
writes, a folder of mixed files for the import, and a multi-page PDF.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

TOPICS = (
    "granite", "harbour", "lantern", "orchard", "compass", "glacier", "tapestry", "violin",
    "monsoon", "quartz", "saffron", "windmill", "cobalt", "meadow", "obsidian", "pavilion",
    "rhubarb", "satchel", "tundra", "umbrella", "velvet", "willow", "yeast", "zephyr",
)
FILLER = (
    "the team reviewed the plan and agreed on a small first step before the next checkpoint",
    "a quiet afternoon of measurement showed that the slowest part was not the part anyone expected",
    "notes from the session were short, but each one named an owner and a date for follow up",
    "the draft stayed open for a week so that every reader could add a comment or a correction",
    "an earlier attempt failed for a simple reason, and the fix was to write the steps down",
    "the summary lists three options, and the group chose the one with the lowest running cost",
    "a sample was collected, labelled, and stored with the others in the same cabinet",
    "the schedule moved twice, yet the final result matched the first estimate within a few percent",
)
NOTE_FOLDER = "Knowledge Base/Notes/Insights"
SEARCH_QUERIES = (
    "what do the notes say about granite and harbour",
    "lantern orchard compass",
    "how did the glacier measurement go",
    "tapestry violin",
    "summary of the monsoon and quartz sessions",
    "saffron windmill cobalt",
    "which option did the group choose for the meadow",
    "obsidian pavilion",
    "notes about rhubarb and the satchel",
    "tundra umbrella velvet",
    "what happened with the willow plan",
    "yeast zephyr",
)
RECALL_TURNS = (
    "I need to continue the work on the granite harbour plan",
    "remind me what we decided about the lantern orchard",
    "where did we leave the glacier measurement",
    "what is open on the monsoon and quartz notes",
    "pick up the saffron windmill thread",
    "what did the group choose for the meadow",
)


def note_text(index: int, rng: random.Random) -> tuple[str, str]:
    """One invented note: (file stem, markdown). Two topics, three paragraphs, a few links."""
    first, second = rng.sample(TOPICS, 2)
    stem = f"note-{index:05d}-{first}-{second}"
    paragraphs = []
    for _ in range(3):
        sentences = [rng.choice(FILLER) for _ in range(4)]
        paragraphs.append(f"On {first} and {second}: " + ". ".join(sentences) + ".")
    links = "\n".join(f"- related: [[note-{rng.randrange(0, 200):05d}]]" for _ in range(3))
    text = (
        "---\n"
        "type: insight\n"
        f"title: Note {index} on {first} and {second}\n"
        f"tags: [synthetic, {first}, {second}]\n"
        f"updated: 2026-02-{(index % 28) + 1:02d}\n"
        "---\n\n"
        f"# Note {index} on {first} and {second}\n\n"
        + "\n\n".join(paragraphs)
        + "\n\n## Related\n\n"
        + links
        + "\n"
    )
    return stem, text


def write_vault_seed(root: Path, count: int, seed: int = 7) -> list[str]:
    """Write `count` notes under root/Knowledge Base/Notes/Insights; return their stems."""
    rng = random.Random(seed)
    folder = root / NOTE_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    stems = []
    for index in range(count):
        stem, text = note_text(index, rng)
        (folder / f"{stem}.md").write_text(text, encoding="utf-8")
        stems.append(stem)
    return stems


def _pdf_escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small valid PDF 1.4 with Helvetica text, one content stream per page."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [" + " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
         + f"] /Count {len(pages)} >>").encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        body = "BT /F1 11 Tf 72 740 Td 14 TL " + " ".join(
            f"({_pdf_escape(line)}) Tj T*" for line in lines) + " ET"
        stream = body.encode("latin-1", "replace")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def pdf_pages(pages: int, label: str, seed: int = 11) -> list[list[str]]:
    rng = random.Random(seed)
    result = []
    for page in range(pages):
        lines = [f"{label} page {page + 1}"]
        for _ in range(38):
            lines.append(" ".join(rng.choice(FILLER).split()[:11]))
        result.append(lines)
    return result


def write_import_folder(root: Path, name: str = "Project Archive") -> dict:
    """A mixed folder: Markdown in subfolders, plain text, and one PDF. Returns its counts."""
    rng = random.Random(23)
    folder = root / name
    shutil.rmtree(folder, ignore_errors=True)
    for index in range(30):
        stem, text = note_text(1000 + index, rng)
        target = folder / f"area-{index % 3}" / f"{stem}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for index in range(12):
        target = folder / "text" / f"log-{index:03d}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(rng.choice(FILLER) for _ in range(40)) + "\n", encoding="utf-8")
    (folder / "reports").mkdir(parents=True, exist_ok=True)
    (folder / "reports" / "annual-summary.pdf").write_bytes(make_pdf(pdf_pages(8, "Annual summary")))
    files = [p for p in folder.rglob("*") if p.is_file()]
    return {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}


def write_media(root: Path, samples: Path) -> dict:
    """The media inputs: a 24-page PDF and two committed OCR sample images."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "bench-report.pdf").write_bytes(make_pdf(pdf_pages(24, "Bench report", seed=31)))
    for name in ("ocr-eng.png", "ocr-jpn.png"):
        shutil.copyfile(samples / name, root / name)
    return {p.name: p.stat().st_size for p in sorted(root.iterdir())}
