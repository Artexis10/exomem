#!/usr/bin/env python
"""Generate an invented, Obsidian-style note collection plus its answer key.

Seeded and deterministic: the same ``--seed`` and ``--profile`` write byte-identical
files and a byte-identical key. The key is written from this generator's own random
draws, never from product output, and it is written OUTSIDE the collection folder so
the product under test can never read it.

Pure standard library. Fixture bytes come from ``tests/fixtures`` (media samples and
the base64 graph-value media); unique-phrase variants of the same formats are
rebuilt here so each carries its own recall phrase.

    python generate.py --seed 7 --profile standard --out /scratch/collection --key /scratch/key.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

GENERATOR_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
# Fixed timestamp base so inventory stat data (size, mtime) is reproducible.
MTIME_BASE = 1_735_689_600  # 2025-01-01T00:00:00Z


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Profile:
    notes: int
    attachments: int
    conflict_copies: int
    zero_byte: int
    trash_notes: int
    duplicate_basename_pairs: int
    folders: int | None  # None = every folder in FOLDERS
    heavy_bytes: int = 0


PROFILES = {
    "small": Profile(60, 15, 3, 2, 2, 2, 9),
    "standard": Profile(1000, 100, 24, 10, 8, 8, None),
    "heavy": Profile(1000, 100, 24, 10, 8, 8, None, heavy_bytes=2 * 1024**3),
}

JA_RATIO = 0.10
NFD_SHARE_OF_JA = 0.4

# (path, weight, kind). Order matters: ``small`` keeps the first ``folders`` entries
# that are not Japanese-only, plus the first Japanese-only one.
FOLDERS: tuple[tuple[str, int, str], ...] = (
    ("projects/harbor-survey", 5, "project"),
    ("journal/2024", 11, "daily"),
    ("people", 6, "person"),
    ("reading/books", 5, "reading"),
    ("meetings/2025", 6, "meeting"),
    ("recipes", 4, "recipe"),
    ("ideas", 5, "idea"),
    ("reference/glossary", 3, "reference"),
    ("areas/home", 3, "area"),
    ("projects/harbor-survey/field-notes", 5, "project"),
    ("projects/garden-planner", 4, "project"),
    ("projects/kite-workshop", 4, "project"),
    ("projects/tide-tables", 3, "project"),
    ("journal/2025", 11, "daily"),
    ("journal/2025/weekly", 2, "weekly"),
    ("reading/articles", 6, "reading"),
    ("reading/papers", 3, "reading"),
    ("meetings/2024", 5, "meeting"),
    ("reference/checklists", 3, "reference"),
    ("areas/travel", 3, "area"),
    ("inbox", 2, "idea"),
    ("日本語/メモ", 3, "ja-only"),
    ("日本語/読書", 2, "ja-only"),
)

ATTACHMENT_FOLDERS = {
    "image": ("attachments", "journal/images", "projects/harbor-survey/assets", "areas/travel/photos"),
    "document": ("attachments", "reading/pdfs", "projects/harbor-survey/assets"),
    "audio": ("meetings/recordings", "attachments"),
    "video": ("meetings/recordings", "attachments"),
    "text": ("attachments", "reference/data"),
}

# --------------------------------------------------------------------------- #
# invented vocabulary (fixture data, not product vocabulary)
# --------------------------------------------------------------------------- #
ADJ = (
    "amber brisk calm copper dusky early faded gentle hollow idle jaunty keen lunar mellow "
    "narrow open pale quiet rustic salted tidy umber vivid windy young zesty bright coarse "
    "distant ember frosted granite humble ivory jade kindled linen mossy nimble oaken pearl"
).split()
NOUN = (
    "anchor beacon compass dune ember ferry gable harbor island jetty kettle lantern meadow "
    "orchard pier quarry reef shingle thicket trellis upland vessel wharf yard almanac basin "
    "cairn delta estuary fjord grove hedge inlet knoll ledger marsh outpost paddock ridge"
).split()
VERB = (
    "review sketch measure compare draft revise tidy catalogue outline test summarise "
    "plan schedule survey label sort polish rehearse inspect archive"
).split()
PERSON_A = "Veltrin Orsa Marlow Tavish Korin Ilsa Brevo Quenby Davor Lysa Hesk Nerys".split()
PERSON_B = "Okonar Pellick Strand Ravel Thorne Winsley Yarrow Zephren Aldous Birchley Corwin".split()
TAGS = (
    "harbor garden kite tides recipe idea reading meeting travel home weekly review draft "
    "followup question reference checklist plan log personal craft weather data"
).split()
NESTED_TAGS = ("project/harbor", "project/garden", "project/kite", "area/home", "area/travel",
               "status/active", "status/waiting", "reading/book", "reading/paper")
STATUSES = ("draft", "active", "waiting", "done")

JA_TOPIC = "港 庭 凧 潮 灯台 地図 茶 雲 森 橋 畑 風 星 湖 駅 市場 帆 岬 窓 道".split()
JA_SUFFIX = ("ガイド", "ログ", "ブック", "プラン", "ボード", "デザイン", "バージョン", "ガーデン",
             "ドキュメント", "プロジェクト", "メモ", "ノート", "記録", "計画")
JA_VERB = "整理 確認 見直し 準備 比較 記録 点検 検討".split()
JA_NOUN = "予定 手順 資料 写真 地図 日程 一覧 感想 課題 候補".split()
JA_PROBE = "霧笛 朝凪 夕凪 星屑 潮騒 灯火 風車 小舟 白帆 青葉".split()
URL_HOST = "example.invalid"

PHRASE_ADJ = "orchid cobalt saffron tundra velvet quartz maple harbor lilac basalt".split()
PHRASE_NOUN = "heron ledger compass lantern kettle atlas ferry orchard glacier marble".split()
PHRASE_TAIL = "inventory schedule briefing journal tally register summary roster".split()


def sentence(rng: random.Random) -> str:
    templates = (
        "The {a} {n} needs another pass before we {v} the {n2}.",
        "We agreed to {v} the {a} {n} and keep the {n2} for later.",
        "A {a} {n} sits next to the {n2}, which makes it easy to {v}.",
        "Next step: {v} the {n} and note how the {a} {n2} changes.",
        "Nothing urgent here, but the {a} {n} deserves a closer look.",
        "The {n} and the {n2} do not match yet; {v} both again.",
    )
    return rng.choice(templates).format(
        a=rng.choice(ADJ), n=rng.choice(NOUN), n2=rng.choice(NOUN), v=rng.choice(VERB)
    )


def ja_sentence(rng: random.Random) -> str:
    templates = (
        "今日は{t1}の{n1}を{v}しました。",
        "{t1}と{t2}の違いについて考えた。",
        "次の{n1}は{t1}に合わせて{v}する予定です。",
        "{t1}の{n1}がまだ足りないので、{t2}の{n2}も{v}したい。",
        "特に急ぎではないが、{t1}の{n1}は早めに{v}しておく。",
    )
    return rng.choice(templates).format(
        t1=rng.choice(JA_TOPIC), t2=rng.choice(JA_TOPIC), n1=rng.choice(JA_NOUN),
        n2=rng.choice(JA_NOUN), v=rng.choice(JA_VERB),
    )


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class Note:
    index: int
    folder: str
    folder_kind: str
    lang: str
    stem: str  # NFC stem used when writing links
    disk_stem: str  # stem as it exists on disk (NFD for some Japanese names)
    date: str
    title: str
    probe: str = ""
    frontmatter_tags: list[str] = field(default_factory=list)
    inline_tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    has_frontmatter: bool = False
    has_overview: bool = False
    links: list[dict] = field(default_factory=list)
    embeds: list[dict] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.disk_stem}.md" if self.folder else f"{self.disk_stem}.md"

    @property
    def link_path(self) -> str:
        """Vault-relative path without extension, as a human writes it in a link."""
        return f"{self.folder}/{self.stem}" if self.folder else self.stem


@dataclass
class Attachment:
    path: str
    data: bytes
    ext: str
    media_type: str  # image | document | audio | video | text
    phrase: str | None
    phrase_lang: str | None
    origin: str
    group: str = ""
    filler: bool = False
    embedded_by: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


# --------------------------------------------------------------------------- #
# attachment payloads
# --------------------------------------------------------------------------- #
MEDIA_TYPE = {
    ".png": "image", ".heic": "image", ".pdf": "document", ".docx": "document",
    ".xlsx": "document", ".pptx": "document", ".odt": "document", ".ods": "document",
    ".odp": "document", ".epub": "document", ".rtf": "document", ".html": "document",
    ".eml": "text", ".ics": "text", ".txt": "text", ".csv": "text", ".json": "text",
    ".ogg": "audio", ".mp4": "video",
}


def _phrase_lang(name: str, phrase: str) -> str:
    if any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in phrase):
        return "ja"
    if "est" in name:
        return "et"
    return "en"


def load_fixture_payloads() -> list[tuple[str, bytes, str | None]]:
    """(name, bytes, phrase) for every reusable fixture, in a fixed order."""
    media = FIXTURES / "media-samples"
    expected = json.loads((media / "expected.json").read_text(encoding="utf-8"))
    out: list[tuple[str, bytes, str | None]] = []
    for name in sorted(expected):
        out.append((name, (media / name).read_bytes(), expected[name]))
    graph = FIXTURES / "graph_value" / "media"
    for name in sorted(p.name for p in graph.glob("*.b64")):
        decoded = base64.b64decode((graph / name).read_text(encoding="ascii"))
        out.append((name[: -len(".b64")], decoded, None))
    return out


def _rezip_with_phrase(raw: bytes, old: str, new: str, tmp: Path) -> bytes | None:
    """Rewrite a zip-based document with ``old`` replaced by ``new`` in every entry."""
    src = tmp / "in.zip"
    dst = tmp / "out.zip"
    src.write_bytes(raw)
    hit = False
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            blob = zin.read(info)
            if old.encode() in blob:
                blob = blob.replace(old.encode(), new.encode())
                hit = True
            fresh = zipfile.ZipInfo(info.filename, date_time=(2025, 1, 1, 0, 0, 0))
            fresh.compress_type = info.compress_type
            fresh.external_attr = info.external_attr
            zout.writestr(fresh, blob)
    return dst.read_bytes() if hit else None


def minimal_pdf(phrase: str) -> bytes:
    stream = f"BT /F1 14 Tf 72 720 Td ({phrase}) Tj ET".encode("latin-1")
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Count 1/Kids[3 0 R]>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def variant_payload(ext: str, phrase: str, rng: random.Random, fixtures: dict[str, bytes],
                    tmp: Path) -> bytes | None:
    if ext == ".txt":
        return f"Reminder: {phrase}.\n".encode()
    if ext == ".html":
        return (f"<html><body><h1>Notice</h1><p>The {phrase} changed.</p></body></html>").encode()
    if ext == ".ics":
        return (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
            f"UID:gen-{rng.randrange(10**8)}\nDTSTART:20260112T090000Z\nSUMMARY:{phrase}\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        ).encode()
    if ext == ".eml":
        return (
            "From: keeper@example.org\nTo: crew@example.org\nSubject: Plan\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            f"Please read the {phrase}.\n"
        ).encode()
    if ext == ".rtf":
        # Raw string with single backslashes: a doubled backslash reads as a UNC path to the privacy gate.
        rtf = r"{\rtf1\ansi\deff0{\fonttbl{\f0 Times New Roman;}}\f0\fs24 The \b PHRASE\b0  begins here.\par}"
        return rtf.replace("PHRASE", phrase).encode()
    if ext == ".pdf":
        return minimal_pdf(phrase)
    if ext == ".csv":
        return f"item,note\nentry,{phrase}\nentry two,steady\n".encode()
    if ext == ".json":
        return json.dumps({"entry": phrase, "state": "steady"}, indent=1).encode()
    fixture_phrase = {
        ".docx": "velvet compass expedition ledger", ".xlsx": "lantern oil inventory",
        ".pptx": "glacier orchard roadmap", ".odt": "heron census at the northern marsh",
        ".ods": "barley harvest tally", ".odp": "tidal observatory briefing",
        ".epub": "the cartographer of salt marshes",
    }.get(ext)
    if fixture_phrase:
        return _rezip_with_phrase(fixtures[f"sample{ext}"], fixture_phrase, phrase, tmp)
    return None


VARIANT_EXTS = (".pdf", ".docx", ".txt", ".html", ".xlsx", ".pptx", ".ics", ".eml", ".rtf",
                ".odt", ".ods", ".odp", ".epub", ".csv", ".json")


def invented_phrase(rng: random.Random, used: set[str]) -> str:
    while True:
        phrase = f"{rng.choice(PHRASE_ADJ)} {rng.choice(PHRASE_NOUN)} {rng.choice(PHRASE_TAIL)}"
        phrase = f"{phrase} {rng.randrange(100, 999)}"
        if phrase not in used:
            used.add(phrase)
            return phrase


def attachment_name(rng: random.Random, ext: str, counter: int, ja: bool) -> str:
    if ext in (".png", ".heic"):
        style = rng.randrange(3)
        if style == 0:
            return f"IMG_{counter:04d}{ext}"
        if style == 1:
            return f"Screenshot 2025-0{rng.randrange(1, 9)}-{rng.randrange(10, 28)} at 14.0{rng.randrange(10)}{ext}"
        return f"diagram-{rng.choice(NOUN)}-{counter}{ext}"
    if ext in (".ogg", ".mp4"):
        return f"Recording {counter:03d} {rng.choice(NOUN)}{ext}"
    if ja:
        return f"{rng.choice(JA_TOPIC)}{rng.choice(JA_SUFFIX)}{counter}{ext}"
    return f"{rng.choice(ADJ)}-{rng.choice(NOUN)}-{counter}{ext}"


def build_attachments(rng: random.Random, profile: Profile, tmp: Path) -> list[Attachment]:
    payloads = load_fixture_payloads()
    fixtures = {name: data for name, data, _ in payloads}
    used_phrases = {p for _, _, p in payloads if p}
    pool: list[tuple[str, bytes, str | None, str]] = [
        (Path(name).suffix, data, phrase, f"fixture:{name}") for name, data, phrase in payloads
    ]
    small = profile.attachments < 30
    base_count = 8 if small else len(pool)
    if small:
        # Keep the small profile representative: one payload per distinct extension.
        seen: set[str] = set()
        chosen = []
        for item in pool:
            if item[0] not in seen:
                seen.add(item[0])
                chosen.append(item)
        pool = chosen[:base_count]
    wanted_unique = max(profile.attachments * 65 // 100, len(pool) + 3)
    variants_needed = max(wanted_unique - len(pool), 0)
    exts = list(VARIANT_EXTS)
    produced = 0
    while produced < variants_needed:
        ext = exts[produced % len(exts)]
        phrase = invented_phrase(rng, used_phrases)
        data = variant_payload(ext, phrase, rng, fixtures, tmp)
        if data is None:
            exts.remove(ext)
            continue
        pool.append((ext, data, phrase, f"generated:{ext.lstrip('.')}"))
        produced += 1

    # Names: every payload once, then re-used bytes under extra names up to the target.
    slots: list[int] = list(range(len(pool)))
    extra = max(profile.attachments - len(pool), 0)
    dup_sources = rng.sample(range(len(pool)), min(len(pool), max(extra // 2, 1)))
    cursor = 0
    while extra > 0:
        slots.append(dup_sources[cursor % len(dup_sources)])
        cursor += 1
        extra -= 1
    rng.shuffle(slots)

    attachments: list[Attachment] = []
    used_paths: set[str] = set()
    counts: dict[int, int] = {}
    for counter, slot in enumerate(slots, start=1):
        ext, data, phrase, origin = pool[slot]
        media_type = MEDIA_TYPE.get(ext, "document")
        folder_choices = ATTACHMENT_FOLDERS.get(media_type, ("attachments",))
        if small:
            folder_choices = ("attachments",)
        folder = rng.choice(folder_choices)
        ja_name = rng.random() < 0.08
        while True:
            name = attachment_name(rng, ext, counter + rng.randrange(1000), ja_name)
            if ja_name and rng.random() < 0.5:
                name = unicodedata.normalize("NFD", name)
            rel = f"{folder}/{name}"
            if rel not in used_paths and name.lower() not in {p.rsplit("/", 1)[-1].lower() for p in used_paths}:
                used_paths.add(rel)
                break
        counts[slot] = counts.get(slot, 0) + 1
        lang = _phrase_lang(origin, phrase) if phrase else None
        attachments.append(Attachment(rel, data, ext, media_type, phrase, lang, origin))
    # Duplicate-bytes groups, ordered by first path for stable ids.
    by_hash: dict[str, list[Attachment]] = {}
    for att in attachments:
        by_hash.setdefault(att.sha256, []).append(att)
    for number, digest in enumerate(sorted(by_hash, key=lambda h: min(a.path for a in by_hash[h]))):
        for att in by_hash[digest]:
            att.group = f"dg-{number:03d}"
    attachments.sort(key=lambda a: a.path)
    return attachments


def heavy_attachments(rng: random.Random, total_bytes: int) -> list[tuple[Attachment, int]]:
    """Large filler binaries for byte-volume pressure; not decodable media.

    Returns each placeholder with the size it will be streamed at, so the bytes
    never sit in memory.
    """
    count = 12
    each = total_bytes // count
    out = []
    for index in range(count):
        ext = (".mp4", ".mov", ".wav")[index % 3]
        path = f"meetings/recordings/bulk-{index:02d}-{rng.choice(NOUN)}{ext}"
        media_type = "audio" if ext == ".wav" else "video"
        placeholder = Attachment(path, b"", ext, media_type, None, None, f"filler:{each}",
                                 group=f"heavy-{index:02d}", filler=True)
        out.append((placeholder, each))
    return out


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #
def apportion(total: int, weights: list[int]) -> list[int]:
    """Largest-remainder split of ``total`` across ``weights``."""
    scale = sum(weights)
    raw = [total * w / scale for w in weights]
    counts = [int(x) for x in raw]
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - counts[i], -i), reverse=True)
    for i in order[: total - sum(counts)]:
        counts[i] += 1
    return counts


def pick_folders(profile: Profile) -> list[tuple[str, int, str]]:
    if profile.folders is None:
        return list(FOLDERS)
    normal = [f for f in FOLDERS if f[2] != "ja-only"][: profile.folders - 1]
    japanese = [f for f in FOLDERS if f[2] == "ja-only"][:1]
    return normal + japanese


def make_title(rng: random.Random, kind: str, lang: str, index: int, dates: Iterator[str]) -> tuple[str, str]:
    """Return (title, stem)."""
    if kind in ("daily", "weekly"):
        date = next(dates)
        if lang == "ja":
            return f"{date} ログ", f"{date}-ログ"
        return (f"Week of {date}", f"{date}-week") if kind == "weekly" else (date, date)
    if lang == "ja":
        title = f"{rng.choice(JA_TOPIC)}{rng.choice(JA_SUFFIX)}"
        return title, title
    if kind == "person":
        title = f"{rng.choice(PERSON_A)} {rng.choice(PERSON_B)}"
    elif kind == "recipe":
        title = f"{rng.choice(ADJ).title()} {rng.choice(NOUN)} bake"
    elif kind == "meeting":
        title = f"{rng.choice(NOUN).title()} sync {rng.choice(ADJ)}"
    elif kind == "reading":
        title = f"{rng.choice(ADJ).title()} {rng.choice(NOUN)}: notes"
    else:
        title = f"{rng.choice(ADJ).title()} {rng.choice(NOUN)} {rng.choice(VERB)}"
    return title, title.replace(":", "")


def date_stream(year_hint: str, rng: random.Random) -> Iterator[str]:
    year = 2024 if "2024" in year_hint else 2025
    day = 0
    while True:
        day += rng.randrange(1, 3)
        month = 1 + (day // 28) % 12
        yield f"{year}-{month:02d}-{1 + day % 28:02d}"


def build_notes(rng: random.Random, profile: Profile) -> list[Note]:
    folders = pick_folders(profile)
    counts = apportion(profile.notes, [w for _, w, _ in folders])
    ja_total = round(profile.notes * JA_RATIO)
    ja_only_total = sum(c for c, (_, _, k) in zip(counts, folders, strict=True) if k == "ja-only")
    extra_ja = max(ja_total - ja_only_total, 0)

    plan: list[tuple[str, str]] = []  # (folder, kind) per note
    for count, (folder, _, kind) in zip(counts, folders, strict=True):
        plan += [(folder, kind)] * count
    ja_candidates = [i for i, (_, kind) in enumerate(plan) if kind != "ja-only"]
    ja_pick = set(rng.sample(ja_candidates, min(extra_ja, len(ja_candidates))))

    notes: list[Note] = []
    seen: set[tuple[str, str]] = set()
    date_iters = {folder: date_stream(folder, rng) for folder, _, _ in folders}
    used_numbers = rng.sample(range(1000, 10000), profile.notes + 50)
    for index, (folder, kind) in enumerate(plan):
        lang = "ja" if (kind == "ja-only" or index in ja_pick) else "en"
        for attempt in range(50):
            title, stem = make_title(rng, kind, lang, index, date_iters[folder])
            if attempt >= 3 and kind not in ("daily", "weekly"):
                suffix = f" {rng.randrange(2, 99)}"
                title, stem = title + suffix, stem + suffix
            if (folder, unicodedata.normalize("NFC", stem).lower()) not in seen:
                break
        seen.add((folder, unicodedata.normalize("NFC", stem).lower()))
        disk = unicodedata.normalize("NFD", stem) if (
            lang == "ja" and unicodedata.normalize("NFD", stem) != stem and rng.random() < NFD_SHARE_OF_JA
        ) else stem
        date = f"202{rng.choice((4, 5))}-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}"
        number = used_numbers[index]
        if lang == "ja":
            probe = f"{rng.choice(JA_PROBE)}{rng.choice(JA_PROBE)}{number}"
        else:
            probe = f"{rng.choice(PHRASE_ADJ)}-{rng.choice(PHRASE_NOUN)}-{number}"
        notes.append(Note(index, folder, kind, lang, stem, disk, date, title, probe))

    # Same basename in two folders: rename the second of each pair (never daily notes).
    candidates = [n for n in notes if n.folder_kind not in ("daily", "weekly")]
    rng.shuffle(candidates)
    pairs = 0
    for first in candidates:
        if pairs >= profile.duplicate_basename_pairs:
            break
        for second in candidates:
            if second.folder != first.folder and second.stem != first.stem and second.lang == first.lang:
                clash = any(n.folder == second.folder and n.stem == first.stem for n in notes)
                if clash:
                    continue
                second.stem, second.disk_stem, second.title = first.stem, first.disk_stem, first.title
                pairs += 1
                break
    # Front matter, tags, aliases.
    for note in notes:
        decorate(rng, note)
    return notes


def decorate(rng: random.Random, note: Note) -> None:
    note.has_frontmatter = rng.random() < 0.7
    pool = [*TAGS, *NESTED_TAGS]
    if note.has_frontmatter:
        note.frontmatter_tags = sorted(rng.sample(pool, rng.randrange(1, 4)))
        if rng.random() < 0.3:
            alias = f"{rng.choice(ADJ)} {note.stem[:12]}".strip()
            if note.lang == "ja":
                alias = f"{rng.choice(JA_TOPIC)}別名"
            note.aliases = [alias]
        note.properties = {
            "status": rng.choice(STATUSES),
            "priority": str(rng.randrange(1, 4)),
            "created": note.date,
        }
        if rng.random() < 0.3:
            note.properties["source"] = f"https://{URL_HOST}/{rng.choice(NOUN)}/{rng.randrange(1000)}"
    note.inline_tags = sorted(rng.sample(pool, rng.randrange(0, 3)))
    note.has_overview = note.folder_kind not in ("daily", "weekly") and rng.random() < 0.8


# --------------------------------------------------------------------------- #
# links and embeds
# --------------------------------------------------------------------------- #
def link_all(rng: random.Random, notes: list[Note], attachments: list[Attachment]) -> None:
    nfc_stems: dict[str, list[Note]] = {}
    for note in notes:
        nfc_stems.setdefault(unicodedata.normalize("NFC", note.stem).lower(), []).append(note)
    alias_owner = {a.lower(): n for n in notes for a in n.aliases}
    by_folder: dict[str, list[Note]] = {}
    for note in notes:
        by_folder.setdefault(note.folder, []).append(note)
    unique = [n for n in notes if len(nfc_stems[unicodedata.normalize("NFC", n.stem).lower()]) == 1]
    unique_ids = {id(n) for n in unique}
    ambiguous = [n for n in notes if len(nfc_stems[unicodedata.normalize("NFC", n.stem).lower()]) > 1]
    for note in notes:
        for _ in range(rng.choice((0, 1, 1, 2, 2, 3, 3, 4, 5))):
            local = by_folder[note.folder]
            pool_unique = [n for n in (local if rng.random() < 0.6 else unique)
                           if id(n) in unique_ids and n is not note]
            roll = rng.random()
            if roll < 0.07 and ambiguous:
                target = rng.choice(ambiguous)
                cands = sorted(n.path for n in nfc_stems[unicodedata.normalize("NFC", target.stem).lower()])
                note.links.append({"raw": f"[[{target.stem}]]", "form": "ambiguous-basename",
                                   "resolution": "ambiguous", "target": None, "candidates": cands})
                continue
            if roll < 0.15:
                label = f"{rng.choice(ADJ)} {rng.choice(NOUN)} {rng.randrange(10, 99)}"
                note.links.append({"raw": f"[[{label}]]", "form": "unresolvable",
                                   "resolution": "unresolvable", "target": None, "candidates": []})
                continue
            if not pool_unique:
                continue
            target = rng.choice(pool_unique)
            if roll < 0.20 and target.aliases:
                alias = target.aliases[0]
                if alias_owner.get(alias.lower()) is target:
                    note.links.append({"raw": f"[[{alias}]]", "form": "frontmatter-alias",
                                       "resolution": "resolvable", "target": target.path, "candidates": []})
                    continue
            forms = ["path", "basename", "basename-alias"]
            if target.has_overview:
                forms += ["basename-heading", "path-heading-alias"]
            form = rng.choice(forms)
            label = rng.choice(ADJ)
            raw = {
                "path": f"[[{target.link_path}]]",
                "basename": f"[[{target.stem}]]",
                "basename-alias": f"[[{target.stem}|{label} {rng.choice(NOUN)}]]",
                "basename-heading": f"[[{target.stem}#Overview]]",
                "path-heading-alias": f"[[{target.link_path}#Overview|{label}]]",
            }[form]
            note.links.append({"raw": raw, "form": form, "resolution": "resolvable",
                               "target": target.path, "candidates": []})

    # Embeds: most attachments are embedded by one or two notes; ~10% stay orphaned.
    for att in attachments:
        if att.filler or rng.random() < 0.10:
            continue
        for _ in range(1 if rng.random() < 0.7 else 2):
            host = rng.choice(notes)
            name = att.path.rsplit("/", 1)[-1]
            by_path = rng.random() < 0.4
            raw = f"![[{att.path if by_path else name}]]"
            host.embeds.append({"raw": raw, "form": "path" if by_path else "basename",
                                "attachment": att.path})
            att.embedded_by.append(host.path)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_note(rng: random.Random, note: Note) -> str:
    gen = ja_sentence if note.lang == "ja" else sentence
    lines: list[str] = []
    if note.has_frontmatter:
        lines.append("---")
        style = rng.randrange(3)
        if note.frontmatter_tags:
            if style == 0:
                lines.append("tags: [" + ", ".join(note.frontmatter_tags) + "]")
            elif style == 1:
                lines.append("tags:")
                lines += [f"  - {t}" for t in note.frontmatter_tags]
            else:
                lines.append("tags: " + ", ".join(note.frontmatter_tags))
        if note.aliases:
            lines.append("aliases: [" + ", ".join(note.aliases) + "]")
        for key, value in note.properties.items():
            lines.append(f"{key}: {value}")
        lines.append("---")
        lines.append("")
    if rng.random() < 0.85:
        lines += [f"# {note.title}", ""]
    if note.has_overview:
        lines += ["## Overview", ""]
    body: list[str] = [" ".join(gen(rng) for _ in range(rng.randrange(2, 5)))]
    probe_line = (f"整理番号 {note.probe} を参照。" if note.lang == "ja"
                  else f"Reference code {note.probe} applies to this entry.")
    body.append(probe_line)
    if rng.random() < 0.5:
        body.append("\n".join(f"- [ ] {gen(rng)}" for _ in range(rng.randrange(1, 4))))
    if rng.random() < 0.15:
        body.append("```\nsample = 'inline code'\n```")
    if rng.random() < 0.2:
        body.append("| item | state |\n| --- | --- |\n| first | open |\n| second | done |")
    if note.inline_tags:
        body.append(" ".join(f"#{t}" for t in note.inline_tags))
    for link in note.links:
        body.append(f"See also {link['raw']}.")
    for embed in note.embeds:
        body.append(embed["raw"])
    if rng.random() < 0.5:
        body.append(gen(rng))
    lines.append("\n\n".join(body))
    return "\n".join(lines) + "\n"


def write_bytes(root: Path, rel: str, data: bytes, order: int) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    stamp = MTIME_BASE + order * 61
    os.utime(target, (stamp, stamp))


def write_filler(root: Path, rel: str, seed: int, size: int, order: int) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    chunk = 8 * 1024 * 1024
    with target.open("wb") as handle:
        left = size
        while left > 0:
            part = min(chunk, left)
            handle.write(rng.randbytes(part))
            left -= part
    stamp = MTIME_BASE + order * 61
    os.utime(target, (stamp, stamp))


def build_junk(rng: random.Random, profile: Profile, notes: list[Note],
               contents: dict[str, bytes]) -> list[dict]:
    """Add junk files to ``contents`` and return their key rows."""
    rows: list[dict] = []

    def add(rel: str, data: bytes, junk_kind: str, of: str | None = None) -> None:
        contents[rel] = data
        rows.append({"path": rel, "kind": "junk", "junk_kind": junk_kind, "copy_of": of,
                     "bytes": len(data)})

    sources = rng.sample(notes, min(profile.conflict_copies, len(notes)))
    styles = ("conflict-dropbox", "conflict-syncthing", "conflict-onedrive", "conflict-numbered")
    for number, note in enumerate(sources):
        style = styles[number % len(styles)]
        data = contents[note.path] + b"\nEdited on another device.\n"
        stem, folder = note.disk_stem, note.folder
        name = {
            "conflict-dropbox": f"{stem} (conflicted copy 2025-02-0{1 + number % 8}).md",
            "conflict-syncthing": f"{stem}.sync-conflict-20250209-1012{number:02d}-ABC{number:04d}.md",
            "conflict-onedrive": f"{stem}-DESKTOP-4F{number:02d}K.md",
            "conflict-numbered": f"{stem} 2.md",
        }[style]
        add(f"{folder}/{name}", data, style, note.path)
    for number in range(profile.zero_byte):
        folder = rng.choice([n.folder for n in notes])
        add(f"{folder}/Untitled{'' if number == 0 else f' {number}'}.md", b"", "zero-byte")
    add(".obsidian/app.json", b'{"alwaysUpdateLinks": true, "newFileLocation": "folder"}\n', "obsidian-config")
    add(".obsidian/appearance.json", b'{"baseFontSize": 16}\n', "obsidian-config")
    add(".obsidian/core-plugins.json", b'["file-explorer", "global-search", "backlink"]\n', "obsidian-config")
    add(".obsidian/workspace.json", b'{"main": {"type": "split", "children": []}}\n', "obsidian-config")
    add(".obsidian/plugins/sample-plugin/manifest.json",
        b'{"id": "sample-plugin", "name": "Sample Plugin", "version": "1.0.0"}\n', "obsidian-config")
    add(".obsidian/snippets/custom.css", b"body { line-height: 1.5; }\n", "obsidian-config")
    for number in range(profile.trash_notes):
        words = " ".join(sentence(rng) for _ in range(2))
        add(f".trash/Deleted {rng.choice(NOUN)} {number}.md",
            f"# Deleted {number}\n\n{words}\n".encode(), "trash")
    add(".DS_Store", bytes(rng.randrange(256) for _ in range(64)), "os-metadata")
    return rows


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
        digest.update(len(rel).to_bytes(4, "big") + rel)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while block := handle.read(1 << 20):
                digest.update(block)
    return digest.hexdigest()


def generate(seed: int, profile_name: str, out: Path, key_path: Path, heavy_bytes: int = 0) -> dict:
    profile = PROFILES[profile_name]
    out = out.resolve()
    key_path = key_path.resolve()
    if key_path == out or out in key_path.parents:
        raise SystemExit("--key must be outside the collection directory")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"collection-ingest/{GENERATOR_VERSION}/{profile_name}/{seed}")

    tmp = out.parent / f".{out.name}-gen-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        attachments = build_attachments(rng, profile, tmp)
    finally:
        for leftover in tmp.glob("*"):
            leftover.unlink()
        tmp.rmdir()
    filler_sizes: dict[str, int] = {}
    if heavy_bytes or profile.heavy_bytes:
        for placeholder, size in heavy_attachments(rng, heavy_bytes or profile.heavy_bytes):
            attachments.append(placeholder)
            filler_sizes[placeholder.path] = size
    notes = build_notes(rng, profile)
    link_all(rng, notes, attachments)

    contents: dict[str, bytes] = {}
    for note in notes:
        contents[note.path] = render_note(rng, note).encode("utf-8")
    for att in attachments:
        if not att.filler:
            contents[att.path] = att.data
    junk_rows = build_junk(rng, profile, notes, contents)

    order = 0
    for rel in sorted(contents):
        write_bytes(out, rel, contents[rel], order)
        order += 1
    for index, att in enumerate(a for a in attachments if a.filler):
        write_filler(out, att.path, seed * 1000 + index, filler_sizes[att.path], order)
        order += 1

    items = []
    for note in notes:
        items.append({
            "path": note.path, "kind": "note", "lang": note.lang, "folder": note.folder,
            "folder_kind": note.folder_kind, "title": note.title,
            "nfd_filename": note.disk_stem != note.stem,
            "bytes": len(contents[note.path]), "sha256": hashlib.sha256(contents[note.path]).hexdigest(),
            "tags": sorted(set(note.frontmatter_tags) | set(note.inline_tags)),
            "frontmatter_tags": note.frontmatter_tags, "inline_tags": note.inline_tags,
            "aliases": note.aliases, "properties": note.properties,
            "probe": {"text": note.probe, "lang": note.lang},
            "links": note.links, "embeds": note.embeds,
        })
    for att in attachments:
        if att.filler:
            file_path = out / att.path
            size = file_path.stat().st_size
            digest = hashlib.sha256()
            with file_path.open("rb") as handle:
                while block := handle.read(1 << 20):
                    digest.update(block)
            sha, nbytes = digest.hexdigest(), size
        else:
            sha, nbytes = att.sha256, len(att.data)
        items.append({
            "path": att.path, "kind": "attachment", "media_type": att.media_type, "ext": att.ext,
            "folder": att.path.rsplit("/", 1)[0], "bytes": nbytes, "sha256": sha,
            "media_phrase": ({"text": att.phrase, "lang": att.phrase_lang} if att.phrase else None),
            "dup_group": att.group, "filler": att.filler, "origin": att.origin,
            "nfd_filename": unicodedata.normalize("NFD", att.path) == att.path
            and unicodedata.normalize("NFC", att.path) != att.path,
            "embedded_by": sorted(att.embedded_by),
        })
    items += junk_rows
    items.sort(key=lambda row: row["path"])

    groups: dict[str, list[str]] = {}
    for row in items:
        if row["kind"] == "attachment":
            groups.setdefault(row["dup_group"], []).append(row["path"])
    notes_in_key = [r for r in items if r["kind"] == "note"]
    atts_in_key = [r for r in items if r["kind"] == "attachment"]
    all_links = [lk for r in notes_in_key for lk in r["links"]]
    summary = {
        "notes": len(notes_in_key),
        "attachments": len(atts_in_key),
        "junk": len(junk_rows),
        "japanese_notes": sum(1 for r in notes_in_key if r["lang"] == "ja"),
        "nfd_note_names": sum(1 for r in notes_in_key if r["nfd_filename"]),
        "folders": len({r["folder"] for r in notes_in_key}),
        "links_resolvable": sum(1 for lk in all_links if lk["resolution"] == "resolvable"),
        "links_ambiguous": sum(1 for lk in all_links if lk["resolution"] == "ambiguous"),
        "links_unresolvable": sum(1 for lk in all_links if lk["resolution"] == "unresolvable"),
        "embeds": sum(len(r["embeds"]) for r in notes_in_key),
        "attachments_in_duplicate_groups": sum(len(m) for m in groups.values() if len(m) > 1),
        "duplicate_groups": sum(1 for m in groups.values() if len(m) > 1),
        "total_bytes": sum(r["bytes"] for r in items),
    }
    key = {
        "format": 1, "generator_version": GENERATOR_VERSION, "seed": seed, "profile": profile_name,
        "fixture_tree_sha256": tree_hash(out), "summary": summary,
        "duplicate_groups": {g: m for g, m in sorted(groups.items()) if len(m) > 1},
        "items": items,
    }
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="standard")
    parser.add_argument("--out", type=Path, required=True, help="collection directory (must be empty)")
    parser.add_argument("--key", type=Path, required=True, help="answer key path, outside --out")
    parser.add_argument("--heavy-bytes", type=int, default=0,
                        help="override the filler byte total of the heavy profile (default 2 GiB)")
    args = parser.parse_args(argv)
    key = generate(args.seed, args.profile, args.out, args.key, args.heavy_bytes)
    print(json.dumps({"fixture_tree_sha256": key["fixture_tree_sha256"], **key["summary"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
