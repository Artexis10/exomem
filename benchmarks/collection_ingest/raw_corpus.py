"""Messy, non-Exomem-shaped input for the ``raw`` profiles, and its answer-key rows.

The standard collection looks like an Obsidian vault that Exomem already fits. Real
imports arrive as raw data: notes without front matter, odd encodings, copies, huge files,
deep trees and awkward names. This module builds that input from a seeded stream, so the
same seed gives the same bytes, and describes what the import contract promises for each
file. ``generate.py`` writes the files and the key; ``score.py`` scores against the key.

Pure standard library. Every file holds invented content. Every file name is valid UTF-8,
holds no control character and stays inside the limits that the import path documents
(255 bytes per name, 4096 bytes per path), so one archive of the tree is never refused
as a whole. The names are legal on Linux; generate on a Linux filesystem.

Each file is one item of the key:

- ``outcome``: ``filed`` (a page exists for it) or ``filed-or-refused`` (a page, or a refusal
  that names a code in ``refusal_codes``). An item that has neither is lost.
- ``form``: ``text`` when the contract keeps the text in the page, ``artifact`` when it keeps
  the bytes as a stored file. The contract is in ``openspec/specs/adoption-studio/spec.md``.
- ``probe``: an ASCII phrase inside the file. ``promised`` says whether the contract promises
  that search finds it. Where it does not, the scorer still reports what it observed.
"""

from __future__ import annotations

import codecs
import gzip
import hashlib
import io
import random
import tarfile
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

FILED = "filed"
FILED_OR_REFUSED = "filed-or-refused"
# A text file larger than this is stored as an artifact, not held in a page. It mirrors
# adoption_plan.INLINE_TEXT_MAX_BYTES, which an instrument that reads no product internals
# cannot import; it only sizes the huge text file and labels its contract form.
INLINE_TEXT_BOUND = 4 * 1024 * 1024
DEFAULT_HUGE_BYTES = 16 * 1024 * 1024
# The huge text file takes this share of --huge-bytes, which puts it above the inline bound.
HUGE_TEXT_SHARE = (5, 16)
CHUNK = 1024 * 1024  # the import path streams in blocks of this size
# The documented limits of one import: bytes per name, bytes per relative path.
NAME_MAX_BYTES = 255
PATH_MAX_BYTES = 4096
# The vault refuses a name with a backslash with one of these codes (a closed set that the import path fixes).
UNSAFE_NAME_CODES = ("INVALID_PATH", "UNSAFE_PATH")

CLASS_CONTRACT = {
    "plain-md": "Markdown without front matter. Filed as a Source, and as an own note when an include rule asks for one. Text is searchable.",
    "same-title": "Three notes that share one H1 title. Each is filed under its own page name.",
    "plain-txt": "Plain text without front matter, one near-empty. Filed as a Source. Text is searchable.",
    "broken-frontmatter": "A front-matter block that never closes, one that is not valid YAML, and a rule line that opens one. Filed with the original text verbatim.",
    "line-endings": "UTF-8 with CRLF, mixed or lone-CR line endings. Filed with the original bytes; text is searchable.",
    "utf8-bom": "UTF-8 with a byte-order mark. Filed; text is searchable.",
    "latin-1": "Latin-1 text, not valid UTF-8. Filed as an artifact with its bytes; the contract does not decode it.",
    "utf-16-bom": "UTF-16 with a byte-order mark. Filed as an artifact with its bytes; the contract does not decode it.",
    "shift-jis": "Shift-JIS text, not valid UTF-8. Filed as an artifact with its bytes; the contract does not decode it.",
    "invalid-utf8": "Mostly UTF-8 with stray or truncated bytes. Filed as an artifact with its bytes.",
    "mixed-types": "Files of many types: text suffixes (text Source), other suffixes and no suffix (artifact).",
    "duplicate": "Byte-identical copies under other names and folders, and one near copy. Every copy is filed.",
    "deep-path": "Files 1 to 24 folders deep. Filed.",
    "long-name": "File and folder names near 200 bytes. Filed.",
    "odd-name": "Names with spaces, punctuation, symbols, emoji and other scripts. Filed.",
    "unsafe-name": "A name with a backslash, which the vault refuses. Filed, or refused with a code.",
    "unicode-twin": "Two files whose names differ only in Unicode normalisation. Both are filed.",
    "case-twin": "Two files whose names differ only in letter case. Both are filed.",
    "flat-dump": "Many small snippets in one folder. Every one is filed.",
    "huge-text": "A UTF-8 text file above the inline text bound, probe near its end. Filed with its bytes.",
    "huge-binary": "A binary file of several MiB. Filed with its bytes, streamed.",
}


@dataclass
class RawItem:
    path: str
    raw_class: str
    form: str
    data: bytes | None = None  # None for a huge file that the writer streams
    size: int = 0  # the size of a streamed file
    probe: str | None = None
    promised: bool = False
    outcome: str = FILED
    refusal_codes: tuple[str, ...] = ()
    dup_group: str = ""

    @property
    def streamed(self) -> bool:
        return self.data is None


@dataclass
class SeedPage:
    """A page that the existing vault holds before the import starts."""

    id: str
    path: str  # may hold {today}: the day of the drive, which names a Source page
    content: str  # may hold {today}
    role: str  # "collider" holds a name that an incoming item plans; "bystander" holds nothing of it
    collides_with: str | None  # the key path of the incoming item, for a collider
    stem: str | None  # the page name that the item plans, without the suffix, for a collider
    probe: str


@dataclass
class RawCorpus:
    items: list[RawItem]
    pages: list[SeedPage]


def vault_pages_path(key_path: Path) -> Path:
    """Where the existing-vault page spec sits beside a key."""
    return key_path.with_name(key_path.stem + ".vault-pages.json")


def class_summary(items: Sequence[RawItem]) -> dict[str, dict]:
    counts = Counter(item.raw_class for item in items)
    return {
        name: {"items": counts[name], "contract": CLASS_CONTRACT[name]}
        for name in sorted(counts)
    }


def key_row(item: RawItem, written: tuple[str, int] | None) -> dict:
    """The answer-key row of one item. ``written`` is the sha256 and size of a streamed file."""
    sha256, size = written if written else (hashlib.sha256(item.data).hexdigest(), len(item.data))
    return {
        "path": item.path, "kind": "raw", "raw_class": item.raw_class, "form": item.form,
        "bytes": size, "sha256": sha256, "streamed": item.streamed,
        "probe": ({"text": item.probe, "lang": "en", "promised": item.promised}
                  if item.probe else None),
        "outcome": item.outcome, "refusal_codes": list(item.refusal_codes),
        "dup_group": item.dup_group or None,
    }


def summary(corpus: RawCorpus) -> dict:
    return {
        "raw_items": len(corpus.items),
        "raw_classes": len({item.raw_class for item in corpus.items}),
        "raw_bytes": sum(item.size for item in corpus.items),
    }


def key_block(corpus: RawCorpus, huge_bytes: int) -> dict:
    groups: dict[str, list[str]] = {}
    for item in corpus.items:
        if item.dup_group:
            groups.setdefault(item.dup_group, []).append(item.path)
    return {
        "huge_bytes": huge_bytes, "inline_text_bound": INLINE_TEXT_BOUND,
        "limits": {"name_max_bytes": NAME_MAX_BYTES, "path_max_bytes": PATH_MAX_BYTES},
        "classes": class_summary(corpus.items),
        "duplicate_groups": dict(sorted(groups.items())),
    }


def existing_vault_block(corpus: RawCorpus) -> dict:
    """What the finished vault must show when the pages of ``vault_pages_spec`` were there first."""
    return {
        "contract": (
            "An import never replaces a page that the vault already holds. An item whose planned "
            "page name is taken gets the next free name, and the page that was there stays as written."
        ),
        "pages": [
            {"id": page.id, "path": page.path, "role": page.role, "collides_with": page.collides_with,
             "stem": page.stem, "probe": page.probe}
            for page in corpus.pages
        ],
    }


def vault_pages_spec(corpus: RawCorpus) -> dict:
    """The files that the driver writes into the vault before the import. ``{today}`` is the drive day."""
    return {
        "format": 1,
        "pages": [{"id": page.id, "path": page.path, "content": page.content} for page in corpus.pages],
    }


def huge_chunks(item: RawItem, seed: int | str, prose: Callable[[random.Random], str]) -> Iterator[bytes]:
    """The bytes of a huge file in blocks, so that the file never sits in memory."""
    rng = random.Random(seed)
    if item.raw_class == "huge-binary":
        left = item.size
        while left > 0:
            part = min(CHUNK, left)
            yield rng.randbytes(part)
            left -= part
        return
    tail = f"\nReference code {item.probe} closes this export.\n".encode("ascii")
    left = item.size - len(tail)
    while left > 0:
        block = bytearray()
        while len(block) < min(CHUNK, left):
            block += prose(rng).encode("ascii") + b"\n"  # prose is ASCII, so a cut never splits a character
        part = bytes(block[:left])
        yield part
        left -= len(part)
    yield tail


def _require_invalid_utf8(data: bytes, label: str) -> bytes:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    raise AssertionError(f"{label} must not be valid UTF-8")


def build(
    rng: random.Random,
    *,
    flat_files: int,
    huge_bytes: int,
    prose: Callable[[random.Random], str],
    probe_words: tuple[Sequence[str], Sequence[str]],
) -> RawCorpus:
    """Every raw item and every existing-vault page for one seed."""
    numbers = iter(rng.sample(range(10_000, 100_000), 400 + flat_files))
    items: list[RawItem] = []

    def probe() -> str:
        return f"{rng.choice(probe_words[0])}-{rng.choice(probe_words[1])}-{next(numbers)}"

    def para(count: int = 2) -> str:
        return " ".join(prose(rng) for _ in range(count))

    def reference(phrase: str) -> str:
        return f"Reference code {phrase} applies to this entry."

    def add(path: str, cls: str, form: str, data: bytes, *, phrase: str | None = None,
            promised: bool = False, outcome: str = FILED, codes: tuple[str, ...] = (),
            group: str = "") -> None:
        items.append(RawItem(path, cls, form, data, len(data), phrase, promised and phrase is not None,
                             outcome, codes, group))

    def text(path: str, cls: str, body: str, *, phrase: str | None, promised: bool = True,
             encoding: str = "utf-8", form: str = "text", **extra) -> None:
        add(path, cls, form, body.encode(encoding), phrase=phrase, promised=promised, **extra)

    def markdown(title: str | None, phrase: str, extra: str = "") -> str:
        head = f"# {title}\n\n" if title else ""
        return f"{head}{para()}\n\n{reference(phrase)}\n{extra}"

    # ---- notes without front matter ------------------------------------------------
    collider_text = probe()
    text("dump/Harbor tide ledger.md", "plain-md", markdown("Harbor tide ledger", collider_text),
         phrase=collider_text)
    p = probe()
    text("dump/no heading note.md", "plain-md", markdown(None, p), phrase=p)
    p = probe()
    text("dump/rule at top.md", "broken-frontmatter", f"---\n\n{para()}\n\n---\n\n{reference(p)}\n", phrase=p)
    p = probe()
    fenced = "```python\n# a hash in code is not a tag\nvalue = '#not-a-tag'\n```\n\n~~~\ntilde fence\n~~~\n"
    text("dump/inline tags and code.md", "plain-md",
         markdown("Inline tags and code", p, f"\n#harbor-log #open-question\n\n{fenced}"), phrase=p)
    for folder in ("a", "b", "c"):
        p = probe()
        text(f"dump/{folder}/Untitled.md", "same-title", markdown("Untitled", p), phrase=p)
    p = probe()
    text("dump/field log.txt", "plain-txt", f"{para(3)}\n\n{reference(p)}\n", phrase=p)
    p = probe()
    text("dump/todo.txt", "plain-txt",
         "\n".join(f"- {prose(rng)}" for _ in range(3)) + f"\n- {reference(p)}\n", phrase=p)
    text("dump/blank.txt", "plain-txt", "\n", phrase=None)
    p = probe()
    text("dump/one line.txt", "plain-txt", reference(p), phrase=p)
    p = probe()
    text("dump/unclosed front matter.md", "broken-frontmatter",
         f"---\ntitle: Half open\ntags: [harbor, unclosed\n\n# Body\n\n{para()}\n\n{reference(p)}\n", phrase=p)
    p = probe()
    text("dump/invalid yaml.md", "broken-frontmatter",
         f"---\nkey: : :\nbad: [1, 2\n---\n\n# Body after bad YAML\n\n{para()}\n\n{reference(p)}\n", phrase=p)

    # ---- line endings and encodings ---------------------------------------------------
    p = probe()
    text("legacy/windows note.md", "line-endings", markdown("Windows note", p).replace("\n", "\r\n"), phrase=p)
    p = probe()
    text("legacy/windows log.txt", "line-endings", f"{para(3)}\n{reference(p)}\n".replace("\n", "\r\n"), phrase=p)
    p = probe()
    lines = [*para(3).split(". "), reference(p), prose(rng)]
    mixed = "".join(line + ("\r\n", "\n", "\r")[number % 3] for number, line in enumerate(lines))
    text("legacy/mixed endings.txt", "line-endings", mixed, phrase=p)
    p = probe()
    add("legacy/notepad export.md", "utf8-bom", "text",
        codecs.BOM_UTF8 + markdown("Notepad export", p).encode("utf-8"), phrase=p, promised=True)

    legacy_body = "Brève réunion à la côte; señor Müller prévoit ½ prix, 20°C, façade ouverte.\n"
    collider_artifact = probe()
    add("legacy/latin1 river survey.txt", "latin-1", "artifact",
        _require_invalid_utf8(f"{legacy_body}{para()}\n{reference(collider_artifact)}\n".encode("latin-1"),
                              "latin-1 text"), phrase=collider_artifact)
    p = probe()
    add("legacy/latin1 cafe notes.md", "latin-1", "artifact",
        _require_invalid_utf8(f"# Café notes\n\n{legacy_body}\n{reference(p)}\n".encode("latin-1"),
                              "latin-1 text"), phrase=p)
    p = probe()
    add("legacy/utf16 minutes.txt", "utf-16-bom", "artifact",
        codecs.BOM_UTF16_LE + f"Minutes\n{para()}\n{reference(p)}\n".encode("utf-16-le"), phrase=p)
    p = probe()
    add("legacy/utf16 agenda.md", "utf-16-bom", "artifact",
        codecs.BOM_UTF16_BE + f"# Agenda\n\n{para()}\n{reference(p)}\n".encode("utf-16-be"), phrase=p)
    p = probe()
    sjis = f"港の潮見表を確認した。\n来週の予定を整理する。\n{reference(p)}\n"
    add("legacy/shiftjis memo.txt", "shift-jis", "artifact",
        _require_invalid_utf8(sjis.encode("shift_jis"), "shift-jis text"), phrase=p)
    p = probe()
    stray = f"# Stray bytes\n\n{para()}\n".encode() + b"bad \xc3\x28 and \xff\xfe\xfd here\n" \
        + f"{reference(p)}\n".encode()
    add("legacy/stray bytes.md", "invalid-utf8", "artifact", _require_invalid_utf8(stray, "stray bytes"), phrase=p)
    p = probe()
    cut = f"{para()}\n{reference(p)}\n".encode() + "港".encode()[:2]
    add("legacy/cut off.txt", "invalid-utf8", "artifact", _require_invalid_utf8(cut, "cut-off text"), phrase=p)

    # ---- many file types ----------------------------------------------------------------
    for name, promised in (
        ("data/readings.tsv", True), ("data/settings.yaml", True), ("data/session.log", True),
        ("data/READ.ME.TXT", True), ("data/Notes.MD", True),
        ("src/tool.py", False), ("src/query.sql", False), ("data/report.xml", False),
        ("data/diagram.svg", False), ("data/notebook.ipynb", False), ("data/NOTES", False),
    ):
        p = probe()
        suffix = Path(name).suffix.lower()
        body = {
            ".tsv": f"item\tnote\nentry\t{p}\nentry two\tsteady\n",
            ".yaml": f"entry: {p}\nstate: steady\n",
            ".log": f"2025-01-02 info start\n2025-01-02 info {p}\n",
            ".py": f'"""Helper."""\n\nNOTE = "{p}"\n',
            ".sql": f"-- {p}\nSELECT 1;\n",
            ".xml": f"<report><entry>{p}</entry></report>\n",
            ".svg": f'<svg xmlns="http://www.w3.org/2000/svg"><title>{p}</title></svg>\n',
            ".ipynb": f'{{"cells": [{{"source": ["{p}"]}}], "nbformat": 4}}\n',
        }.get(suffix, f"# {Path(name).stem}\n\n{reference(p)}\n")
        text(f"types/{name}", "mixed-types", body, phrase=p, promised=promised,
             form="text" if promised else "artifact")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        info = zipfile.ZipInfo("inside.txt", date_time=(2025, 1, 1, 0, 0, 0))
        bundle.writestr(info, para())
    add("types/data/archive.zip", "mixed-types", "artifact", archive.getvalue())
    tarball = io.BytesIO()
    with tarfile.open(fileobj=tarball, mode="w") as tar:
        member = tarfile.TarInfo("inside.txt")
        body_bytes = para().encode()
        member.size, member.mtime = len(body_bytes), 1_735_689_600
        tar.addfile(member, io.BytesIO(body_bytes))
    add("types/data/bundle.tar.gz", "mixed-types", "artifact",
        gzip.compress(tarball.getvalue(), mtime=0))
    add("types/data/blob.dat", "mixed-types", "artifact", rng.randbytes(32 * 1024))

    # ---- copies ------------------------------------------------------------------------------
    p = probe()
    original = markdown("Harbor checklist", p).encode()
    for path in ("dump/Harbor checklist.md", "copies/Copy of Harbor checklist.md",
                 "copies/archive/Harbor checklist (1).md"):
        add(path, "duplicate", "text", original, phrase=p, promised=True, group="rd-000")
    add("copies/Harbor checklist - edited.md", "duplicate", "text",
        original + b"\nOne line added on another day.\n", phrase=p, promised=True, group="rd-000")

    # ---- deep trees and long names ---------------------------------------------------------------
    for depth in (1, 6, 12, 14, 24):
        p = probe()
        folders = "/".join(f"level-{level:02d}" for level in range(1, depth + 1))
        text(f"deep/{folders}/bottom note {depth}.md", "deep-path", markdown(f"Bottom note {depth}", p),
             phrase=p)
    long_ascii = (" ".join(rng.choice(probe_words[1]) for _ in range(40)) + " " * 200)[:196].strip()
    p = probe()
    text(f"long names/{long_ascii}.md", "long-name", markdown(None, p), phrase=p)
    long_japanese = "長い名前の資料" * 8 + "終"  # 57 characters: 171 bytes
    p = probe()
    text(f"long names/{long_japanese}.md", "long-name", markdown(None, p), phrase=p)
    long_folder = ("deep folder " * 20)[:196].strip()
    p = probe()
    text(f"long names/{long_folder}/note in a long folder.md", "long-name",
         markdown("Note in a long folder", p), phrase=p)

    for name in (
        "meeting notes (final) [v2].md", "Q&A what's next? #1.md", "100% done; really!.txt",
        "a   b    c.md", "-leading dash.md", "~tilde $dollar @at =eq +plus ,comma.md",
        'say "quoted" <angle> |pipe| *star*.md', "colon: separated.md", "emoji 📝 notes.md",
        "Привет мир.md", "مرحبا بالعالم.md", "spaced folder /note in a spaced folder.md",
        "v1.2.3 release notes.final.md", "fullwidth ＡＢＣ １２３.md",
    ):
        p = probe()
        stem = Path(name).stem
        body = markdown(stem if len(name) % 2 else None, p)
        text(f"odd names/{name}", "odd-name", body, phrase=p)
    p = probe()
    text("odd names/back\\slash note.md", "unsafe-name", markdown("Back slash note", p), phrase=p,
         promised=False, outcome=FILED_OR_REFUSED, codes=UNSAFE_NAME_CODES)

    # ---- names that differ only by normalisation or case ---------------------------------------
    for number, form in enumerate(("NFC", "NFD"), start=1):
        p = probe()
        text(f"twins/{unicodedata.normalize(form, 'Café ledger')}.md", "unicode-twin",
             markdown(f"Cafe twin ledger {number}", p), phrase=p)
    for name in ("Case Twin", "case twin"):
        p = probe()
        text(f"twins/{name}.md", "case-twin", markdown(f"{name} body", p), phrase=p)

    # ---- one folder with many small files ----------------------------------------------------------
    for number in range(1, flat_files + 1):
        p = probe()
        suffix = ".md" if number % 3 == 0 else ".txt"
        text(f"snippets/snippet-{number:04d}{suffix}", "flat-dump", f"{prose(rng)}\n{reference(p)}\n", phrase=p)

    # ---- huge files (streamed by the writer) -----------------------------------------------------------
    text_size = huge_bytes * HUGE_TEXT_SHARE[0] // HUGE_TEXT_SHARE[1]
    p = probe()
    # Above the inline bound the contract keeps the bytes as a file, so search is not promised.
    big_text = text_size > INLINE_TEXT_BOUND
    items.append(RawItem("bulk/export-log.txt", "huge-text", "artifact" if big_text else "text", None,
                         text_size, p, not big_text))
    items.append(RawItem("bulk/capture.dat", "huge-binary", "artifact", None, huge_bytes - text_size))
    check_paths(items)
    return RawCorpus(sorted(items, key=lambda item: item.path), _seed_pages(items, probe))


def check_paths(items: Sequence[RawItem]) -> None:
    """Fail the generator when a name would break the import as a whole, not just one file."""
    paths = [item.path for item in items]
    assert len(paths) == len(set(paths)), "a raw path repeats"
    for item in items:
        parts = item.path.split("/")
        assert all(0 < len(part.encode("utf-8")) <= NAME_MAX_BYTES for part in parts), item.path
        assert len(item.path.encode("utf-8")) <= PATH_MAX_BYTES, item.path
        assert not any(ord(char) < 32 or char == "\x7f" for char in item.path), item.path
        assert ("\\" in item.path) == (item.raw_class == "unsafe-name"), item.path


def _seed_pages(items: Sequence[RawItem], probe: Callable[[], str]) -> list[SeedPage]:
    """Pages that exist before the import: two take a name that an incoming item plans, two do not.

    A Source page is named ``<day>-<title slug>.md``, and an artifact's page ``<day>-<slug><suffix>.md``.
    The contract gives an item whose planned name is taken the next free name; it never replaces
    the existing page. The titles here are plain lowercase words, so the slug is the words joined
    by hyphens.
    """
    by_path = {item.path: item for item in items}
    day_dir = "Knowledge Base/Sources/Imported"

    def page(page_id: str, path: str, title: str, *, role: str, collides_with: str | None,
             stem: str | None, extra: str = "") -> SeedPage:
        phrase = probe()
        body = (
            f"---\ntype: source\ntitle: {title}\nsource_type: unclassified\ncaptured: {{today}}\n"
            f"{extra}tags: [existing]\ningested_into: []\n---\n\n# {title}\n\n"
            f"This page was in the vault before the import. Reference code {phrase} belongs to it.\n"
        )
        return SeedPage(page_id, path, body, role, collides_with, stem, phrase)

    text_item = "dump/Harbor tide ledger.md"
    artifact_item = "legacy/latin1 river survey.txt"
    assert text_item in by_path and artifact_item in by_path
    return [
        page("collide-text", f"{day_dir}/{{today}}-harbor-tide-ledger.md", "Earlier Harbor tide ledger",
             role="collider", collides_with=text_item, stem="{today}-harbor-tide-ledger"),
        page("collide-artifact", f"{day_dir}/{{today}}-latin1-river-survey.txt.md", "Earlier river survey",
             role="collider", collides_with=artifact_item, stem="{today}-latin1-river-survey"),
        page("bystander-source", f"{day_dir}/{{today}}-bystander-source.md", "Earlier archive note",
             role="bystander", collides_with=None, stem=None,
             extra="imported_from: Earlier Archive/notes/archive note.md\nimport_collection: Earlier Archive\n"
                   "collection_path: notes/archive note.md\n"
                   f"original_sha256: {hashlib.sha256(b'earlier archive note').hexdigest()}\n"
                   "original_bytes: 20\n"),
        page("bystander-note", "Knowledge Base/Notes/Insights/existing-hand-written-note.md",
             "Existing hand-written note", role="bystander", collides_with=None, stem=None),
    ]
