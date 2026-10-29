"""A seeded folder for the large agent-led import rehearsal, and the answer key that judges the run.

The generator decides what each file is expected to become. It never reads the product's own
suffix lists or the cell's replies: a note or caption is text, every other visible file is an
artifact, and a file's size against the adoption ceiling and its hidden state fix the rest. The
manifest it writes (path, size, SHA-256, disposition) is the only truth the pass criteria use.
File names are ASCII, so a path compares equal before and after the cell normalises it.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

GIB = 1 << 30
MIB = 1 << 20

DEFAULT_FILES = 70_000
DEFAULT_TARGET_BYTES = 6_000_000_000

# Dispositions are this module's own closed result set, fixed by construction.
FILED_ARTIFACT = "filed_artifact"  # a visible non-text file under the ceiling: Source, then consumed
FILED_TEXT = "filed_text"  # a note or caption: filed as text, its staged copy stays until discard
OVER_CEILING = "over_ceiling"  # above the adoption ceiling: stays staged, never filed
HIDDEN_FOLDER = "hidden_folder"  # inside a hidden folder that the client sent: stays staged
HIDDEN_FILE = "hidden_file"  # a hidden file outside any hidden folder that the client sent
LEFT_BEHIND = "left_behind"  # hidden and the client was not told to send it: never staged

# The product's residual report groups a staged leftover under these result keys. Mapping the
# dispositions onto them is a fixed protocol of the report, not a vocabulary of the data.
RESIDUAL_REASON = {
    FILED_TEXT: "text",
    OVER_CEILING: "over_ceiling",
    HIDDEN_FOLDER: "ineligible",
    HIDDEN_FILE: "outside_run",
}
# A page counts only its own files under these; a leftover on another page reads `outside_run`.
COUNTED_REASONS = ("text", "over_ceiling", "ineligible")
# A staged file under these has not gone where it should: it failed, was kept, or was skipped.
EMPTY_REASONS = ("failed", "kept", "pending", "unselected")
STAGE_DIR = "_Imports"  # the vault folder that holds a Cloud import's stage

TEXT_KINDS = ("note", "caption")
FILED = (FILED_ARTIFACT, FILED_TEXT)

NOTE_BYTES = 1_500  # nominal size of a note; the written note varies around it
CAPTION_BYTES = 300
HIDDEN_BYTES = 1_000
MIN_BULK_BYTES = 1_024
FLAT_ATTACHMENTS = 5_200  # one flat folder, more than the 5,000 files a page holds
NOTE_LEAF = 40  # notes per leaf folder
SYLLABLES = 70  # 14 consonants x 5 vowels
VOCAB = SYLLABLES**3
CHUNK = MIB
LARGE_MIN_BYTES = 100 * MIB
LARGE_NOTES, LARGE_MEDIA = 4, 5
GIT_FILES, OBSIDIAN_FILES = 60, 4
CAPTIONS = 20
MEDIA_FILES_DIVISOR = 100  # the second folder holds this share of the files
NOTES_SHARE = 0.8  # the first folder holds this share of the bytes
MIN_NOTES = 3

# Peak disk use of the first folder: its source, the client's ciphertext (x1.05), the object store
# (x1.05) and the backup repository (x1.0), plus the cell volume and some headroom for images.
PEAK_COPIES = 4.1
VOLUME_FACTOR = 1.6  # volume = 1.6 x the first folder, which holds one copy and not two
MIN_VOLUME_GIB = 4  # the platform chart's smallest cellStorage.maxCellGib
HEADROOM_BYTES = 4 * GIB
SHRINK = 0.9

_CONSONANTS = "bdfgklmnprstvz"
_VOWELS = "aeiou"


class FixtureError(ValueError):
    pass


class SizingError(FixtureError):
    pass


@dataclass(frozen=True)
class Entry:
    """One file of the answer key, as written to disk."""

    path: str
    size: int
    sha256: str
    disposition: str


@dataclass(frozen=True)
class Spec:
    """One planned file; `size` of a text kind is nominal, the written file is the truth."""

    path: str
    kind: str  # note, caption, binary or hidden
    size: int
    disposition: str
    ordinal: int = 0
    hidden: str | None = None  # "folder" for a file inside a hidden folder, "file" for a loose one
    marked: bool = False  # carries the sentinel phrase


@dataclass(frozen=True)
class Sizing:
    notes_bytes: int
    media_bytes: int
    volume_gib: int
    files_notes: int
    files_media: int
    scaled: bool
    proves_one_copy_only: bool
    note: str


@dataclass(frozen=True)
class Tally:
    """What the residual reports said over all pages."""

    files_by_reason: dict[str, int]
    final_files: int


def disposition_of(kind: str, size: int, *, ceiling: int, hidden: str | None, include_hidden: bool) -> str:
    if hidden is not None:
        if not include_hidden:
            return LEFT_BEHIND
        return HIDDEN_FOLDER if hidden == "folder" else HIDDEN_FILE
    if kind in TEXT_KINDS:
        return FILED_TEXT
    return OVER_CEILING if size > ceiling else FILED_ARTIFACT


def word(number: int) -> str:
    """A pronounceable unique name for a number; the width grows with it so no two collide."""
    base = len(_CONSONANTS) * len(_VOWELS)
    parts = []
    while len(parts) < 3 or number:
        number, digit = divmod(number, base)
        parts.append(_CONSONANTS[digit // len(_VOWELS)] + _VOWELS[digit % len(_VOWELS)])
    return "".join(parts)


def sentinel_phrase(seed: str, folder: str) -> str:
    digest = hashlib.sha256(f"{seed}:{folder}:sentinel".encode()).digest()
    return " ".join(word(int.from_bytes(digest[i : i + 4], "big") % VOCAB) for i in (0, 4, 8))


# --- layout ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fixed:
    specs: list[Spec]  # everything but the notes and the bulk files
    text_count: int
    bulk_count: int
    fixed_bytes: int  # nominal bytes of the specs and of the text


def _large_sizes(count: int, ceiling: int, large_min: int) -> tuple[list[int], int]:
    """Sizes spread from `large_min` up to just under the ceiling, and one size just over it."""
    margin = max(1, (ceiling - large_min) // 10)
    high = max(large_min, ceiling - margin)
    sizes = [large_min + (high - large_min) * k // max(1, count - 1) for k in range(count)]
    return sizes, ceiling + margin


def _hidden_specs(names: Sequence[tuple[str, str]], *, ceiling: int, include_hidden: bool) -> list[Spec]:
    return [
        Spec(path, "hidden", HIDDEN_BYTES, disposition_of("hidden", HIDDEN_BYTES, ceiling=ceiling,
                                                           hidden=hidden, include_hidden=include_hidden),
             ordinal=n, hidden=hidden)
        for n, (path, hidden) in enumerate(names)
    ]


def _binary(path: str, size: int, ordinal: int, ceiling: int) -> Spec:
    return Spec(path, "binary", size, disposition_of("binary", size, ceiling=ceiling, hidden=None,
                                                     include_hidden=False), ordinal=ordinal)


def _fixed(folder: str, files: int, *, ceiling: int, large_min: int, include_hidden: bool) -> _Fixed:
    if folder == "notes":
        sizes, over = _large_sizes(LARGE_NOTES, ceiling, large_min)
        specs = [_binary(f"archive/backup-{k:02d}.dat", size, k, ceiling) for k, size in enumerate(sizes)]
        specs.append(_binary("archive/disk-image.dat", over, len(sizes), ceiling))
        git = [(".git/HEAD", "folder"), (".git/config", "folder"), (".git/description", "folder"),
               (".git/refs/heads/main", "folder")]
        git += [(f".git/objects/{n % 256:02x}/{word(n)}", "folder") for n in range(GIT_FILES - len(git))]
        obsidian = [(f".obsidian/{name}.json", "folder")
                    for name in ("app", "appearance", "core-plugins", "workspace")[:OBSIDIAN_FILES]]
        loose = [(".DS_Store", "file"), (".hidden-note.md", "file"), ("notes/.draft.md", "file")]
        hidden = _hidden_specs([*git, *obsidian, *loose], ceiling=ceiling, include_hidden=include_hidden)
        specs += hidden
        bulk = min(FLAT_ATTACHMENTS, files // 10)
        text = files - bulk - len(specs)
        text_bytes = text * NOTE_BYTES
    elif folder == "media":
        sizes, _ = _large_sizes(LARGE_MEDIA, ceiling, large_min)
        specs = [_binary(f"masters/master-{k:02d}.dat", size, k, ceiling) for k, size in enumerate(sizes)]
        hidden = _hidden_specs([(".DS_Store", "file"), (".hidden-caption.txt", "file"),
                                ("clips/.thumbs/thumb-00.dat", "folder")],
                               ceiling=ceiling, include_hidden=include_hidden)
        specs += hidden
        text = CAPTIONS
        bulk = files - len(specs) - text
        text_bytes = text * CAPTION_BYTES
    else:
        raise FixtureError(f"unknown fixture folder {folder!r}")
    if text < MIN_NOTES or bulk < 1:
        raise FixtureError(f"{files} files cannot hold the {folder} folder's fixed files")
    return _Fixed(specs, text, bulk, sum(spec.size for spec in specs) + text_bytes)


def minimum_bytes(folder: str, files: int, ceiling: int, large_min: int) -> int:
    """The fewest bytes that the folder's fixed files and its bulk files can take."""
    fixed = _fixed(folder, files, ceiling=ceiling, large_min=large_min, include_hidden=True)
    return fixed.fixed_bytes + fixed.bulk_count * MIN_BULK_BYTES


def _bulk_sizes(seed: str, folder: str, count: int, budget: int) -> list[int]:
    """Uneven sizes, at least `MIN_BULK_BYTES` each, that add up to `budget` exactly."""
    rng = random.Random(f"{seed}:{folder}:sizes")
    weights = [0.25 + 1.5 * rng.random() for _ in range(count)]
    spare = budget - count * MIN_BULK_BYTES
    sizes = [MIN_BULK_BYTES + int(spare * weight / sum(weights)) for weight in weights]
    sizes[0] += budget - sum(sizes)
    return sizes


def layout(folder: str, *, seed: str, files: int, total_bytes: int, ceiling: int, large_min: int,
           include_hidden: bool) -> list[Spec]:
    """Plan every file of a folder: exactly `files` of them, adding up to about `total_bytes`.

    `include_hidden` says whether the client will be told to send hidden files; it decides which
    of them are expected to reach the stage.
    """
    fixed = _fixed(folder, files, ceiling=ceiling, large_min=large_min, include_hidden=include_hidden)
    budget = total_bytes - fixed.fixed_bytes
    if budget < fixed.bulk_count * MIN_BULK_BYTES:
        raise FixtureError(f"{total_bytes} bytes cannot hold the {folder} folder; it needs at least "
                           f"{minimum_bytes(folder, files, ceiling, large_min)}")
    sizes = _bulk_sizes(seed, folder, fixed.bulk_count, budget)
    specs = list(fixed.specs)
    if folder == "notes":
        for n in range(fixed.text_count):
            leaf = n // NOTE_LEAF
            path = f"notes/area-{leaf // 40:02d}/topic-{leaf % 40:03d}/{word(n)}.md"
            specs.append(Spec(path, "note", NOTE_BYTES, FILED_TEXT, ordinal=n,
                              marked=n == fixed.text_count // 2))
        specs += [_binary(f"attachments/att-{n:05d}.dat", size, n, ceiling) for n, size in enumerate(sizes)]
    else:
        for n in range(fixed.text_count):
            specs.append(Spec(f"captions/caption-{n:02d}.txt", "caption", CAPTION_BYTES, FILED_TEXT,
                              ordinal=n, marked=n == 0))
        specs += [_binary(f"clips/set-{n // 50:02d}/clip-{n:04d}.dat", size, n, ceiling)
                  for n, size in enumerate(sizes)]
    return specs


# --- writing --------------------------------------------------------------------------------


def _stream(seed: str, path: str, size: int) -> Iterator[bytes]:
    """Unique deterministic bytes for one file, a chunk at a time."""
    made, counter = 0, 0
    while made < size:
        length = min(CHUNK, size - made)
        yield hashlib.shake_256(f"{seed}:{path}:{counter}".encode()).digest(length)
        made += length
        counter += 1


def _link_targets(ordinal: int, notes: int) -> list[int]:
    found: list[int] = []
    for step in (1, 2, 3):
        target = (ordinal * 7919 + step * 104_729 + 13) % notes
        if target != ordinal and target not in found:
            found.append(target)
    return found or [(ordinal + 1) % notes]


def note_text(seed: str, ordinal: int, notes: int, attachments: int, sentinel: str | None) -> str:
    rng = random.Random(f"{seed}:note:{ordinal}")
    title = f"{word(rng.randrange(VOCAB))} {word(rng.randrange(VOCAB))}"
    links = " ".join(f"[[{word(target)}]]" for target in _link_targets(ordinal, notes))
    lines = [f"---\ntags: [topic-{ordinal % 40}]\n---\n", f"# {title}\n",
             f"Note {ordinal} of {notes}. Related: {links}\n"]
    if attachments:
        lines.append(f"![[att-{(ordinal * 31) % attachments:05d}.dat]]\n")
    if sentinel:
        lines.append(f"Remember {sentinel}.\n")
    wanted = rng.randint(NOTE_BYTES * 2 // 3, NOTE_BYTES * 4 // 3)
    body = ""
    while len(body) < wanted:
        body += " ".join(word(rng.randrange(VOCAB)) for _ in range(rng.randint(6, 14))) + ".\n"
    return "\n".join(lines) + "\n" + body


def write_tree(root: Path, specs: Sequence[Spec], *, seed: str, sentinel: str | None = None) -> list[Entry]:
    """Write the planned files under `root` and return the manifest entries of what was written."""
    notes = sum(1 for spec in specs if spec.kind == "note")
    attachments = sum(1 for spec in specs if spec.path.startswith("attachments/"))
    made_directories: set[Path] = set()
    entries: list[Entry] = []
    for spec in specs:
        target = root / spec.path
        if target.parent not in made_directories:
            target.parent.mkdir(parents=True, exist_ok=True)
            made_directories.add(target.parent)
        if spec.kind == "note":
            chunks: Iterable[bytes] = [note_text(seed, spec.ordinal, notes, attachments,
                                                 sentinel if spec.marked else None).encode()]
        elif spec.kind == "caption":
            text = f"Caption {spec.ordinal}. {word(spec.ordinal)} {sentinel if spec.marked else ''}\n"
            chunks = [text.encode()]
        else:
            chunks = _stream(seed, spec.path, spec.size)
        digest, size = hashlib.sha256(), 0
        with target.open("wb") as output:
            for chunk in chunks:
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        entries.append(Entry(spec.path, size, digest.hexdigest(), spec.disposition))
    return entries


def write_manifest(path: Path, entries: Iterable[Entry]) -> str:
    """Write the manifest as JSON lines and return its SHA-256."""
    lines = (json.dumps({"path": e.path, "size": e.size, "sha256": e.sha256, "disposition": e.disposition},
                        sort_keys=True) for e in entries)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(path: Path) -> list[Entry]:
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return [Entry(row["path"], row["size"], row["sha256"], row["disposition"]) for row in rows]


# --- sizing ---------------------------------------------------------------------------------


def volume_gib_for(notes_bytes: int) -> int:
    return max(MIN_VOLUME_GIB, math.ceil(VOLUME_FACTOR * notes_bytes / GIB))


def generation_need_bytes(folder_bytes: int) -> int:
    """The runner disk a folder takes while it is imported: its source, the client's ciphertext, the
    object store and the backup repository, plus some headroom."""
    return math.ceil(PEAK_COPIES * folder_bytes) + HEADROOM_BYTES


def peak_disk_bytes(notes_bytes: int) -> int:
    """The runner disk the first folder needs at its peak, whatever else is already on it."""
    return generation_need_bytes(notes_bytes) + volume_gib_for(notes_bytes) * GIB


def plan_sizes(*, free_bytes: int, ceiling: int, files: int, target_bytes: int,
               large_min: int = LARGE_MIN_BYTES) -> Sizing:
    """Choose the bytes of each folder and the cell volume from the free disk.

    The file count never shrinks; the bytes do, by a tenth at a time, until the first folder's peak
    fits. The volume is sized to hold one copy of the first folder and not two, and the result says
    when scaling left the volume too large to prove that.
    """
    files_media = files // MEDIA_FILES_DIVISOR
    files_notes = files - files_media
    floor_notes = minimum_bytes("notes", files_notes, ceiling, large_min)
    floor_media = minimum_bytes("media", files_media, ceiling, large_min)
    notes = max(floor_notes, int(target_bytes * NOTES_SHARE))
    scaled = False
    while True:
        media = max(floor_media, int(notes * (1 - NOTES_SHARE) / NOTES_SHARE))
        needed = peak_disk_bytes(max(notes, media))
        if needed <= free_bytes:
            break
        if notes <= floor_notes:
            raise SizingError(f"{free_bytes / GIB:.1f} GiB free disk cannot hold even the smallest fixture, "
                              f"which needs {needed / GIB:.1f} GiB")
        notes, scaled = max(floor_notes, int(notes * SHRINK)), True
    volume = volume_gib_for(notes)
    proves = volume * GIB < 2 * notes
    note = (f"first folder {notes / GIB:.2f} GiB and second {media / GIB:.2f} GiB on a {volume} GiB volume"
            + (f"; scaled down from {target_bytes / GIB:.2f} GiB to fit {free_bytes / GIB:.1f} GiB free"
               if scaled else ""))
    if not proves:
        note += (f"; the volume is not below two copies of the first folder ({2 * notes / GIB:.2f} GiB), "
                 "so the run cannot show that consuming is what makes it fit")
    return Sizing(notes, media, volume, files_notes, files_media, scaled, proves, note)


# --- the answer key -------------------------------------------------------------------------


def expected_stage(entries: Iterable[Entry]) -> dict[str, tuple[int, str]]:
    """What the client sends: every file that it was not told to leave behind."""
    return {e.path: (e.size, e.sha256) for e in entries if e.disposition != LEFT_BEHIND}


def expected_leftover(entries: Iterable[Entry]) -> dict[str, tuple[int, str]]:
    """What stays staged once every artifact is consumed."""
    return {e.path: (e.size, e.sha256) for e in entries if e.disposition in RESIDUAL_REASON}


def expected_by_reason(entries: Iterable[Entry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        if e.disposition in RESIDUAL_REASON:
            reason = RESIDUAL_REASON[e.disposition]
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def count_consumable(entries: Iterable[Entry]) -> int:
    return sum(1 for e in entries if e.disposition == FILED_ARTIFACT)


def _examples(paths: Iterable[str], limit: int = 3) -> str:
    ordered = sorted(paths)
    return ", ".join(ordered[:limit]) + (f" and {len(ordered) - limit} more" if len(ordered) > limit else "")


def check_sources(entries: Iterable[Entry], import_id: str, sources: Mapping[str, Sequence[str]]) -> list[str]:
    """Every filed file has exactly one Source, named for its staged original, with its SHA-256."""
    missing, wrong = [], []
    for e in entries:
        if e.disposition not in FILED:
            continue
        found = sources.get(f"{STAGE_DIR}/{import_id}/{e.path}")
        if not found:
            missing.append(e.path)
        elif list(found) != [e.sha256]:
            wrong.append(e.path)
    problems = []
    if missing:
        problems.append(f"no Source for {len(missing)} filed files: {_examples(missing)}")
    if wrong:
        problems.append(f"{len(wrong)} Sources hold another SHA-256 than the original: {_examples(wrong)}")
    return problems


def _compare(expected: Mapping[str, tuple[int, str]], found: Mapping[str, tuple[int, str]],
             extra_means: str, lost_means: str, altered_means: str) -> list[str]:
    extra = [path for path in found if path not in expected]
    lost = [path for path in expected if path not in found]
    altered = [path for path in expected if path in found and found[path] != expected[path]]
    problems = []
    if extra:
        problems.append(f"{len(extra)} {extra_means}: {_examples(extra)}")
    if lost:
        problems.append(f"{len(lost)} {lost_means}: {_examples(lost)}")
    if altered:
        problems.append(f"{len(altered)} {altered_means}: {_examples(altered)}")
    return problems


def check_stage(entries: Iterable[Entry], listing: Mapping[str, tuple[int, str]]) -> list[str]:
    """What stays in the stage equals the key's leftover, file by file, in size and SHA-256."""
    return _compare(expected_leftover(entries), listing, "staged files should be gone",
                    "staged files that should stay are gone", "staged files changed")


def check_members(entries: Iterable[Entry], members: Iterable[Mapping]) -> list[str]:
    """The stage's receipt lists exactly the files that the client was told to send, with their hashes."""
    listed: dict[str, tuple[int, str]] = {}
    repeated = []
    for member in members:
        if member["path"] in listed:
            repeated.append(member["path"])
        listed[member["path"]] = (int(member["size"]), member["sha256"])
    problems = _compare(expected_stage(entries), listed, "receipt members were never sent",
                        "sent files are missing from the receipt", "receipt members differ from the sent file")
    if repeated:
        problems.append(f"{len(repeated)} receipt members are listed twice: {_examples(repeated)}")
    return problems


def tally_residuals(residuals: Sequence[Mapping | None]) -> Tally:
    """Add each page's residual files by reason; the last page also says how much is left in all."""
    totals: dict[str, int] = {}
    for residual in residuals:
        for reason, row in ((residual or {}).get("by_reason") or {}).items():
            totals[reason] = totals.get(reason, 0) + int((row or {}).get("files", 0))
    last = residuals[-1] if residuals else None
    return Tally(totals, int((last or {}).get("files", 0)))


def check_residual(entries: Iterable[Entry], tally: Tally) -> list[str]:
    """The pages' residual reports agree with the key, by reason, and nothing was left unfiled."""
    expected = expected_by_reason(entries)
    problems = []
    for reason in COUNTED_REASONS:
        reported, wanted = tally.files_by_reason.get(reason, 0), expected.get(reason, 0)
        if reported != wanted:
            problems.append(f"residual reason {reason}: the key expects {wanted} files, the pages reported {reported}")
    for reason in EMPTY_REASONS:
        if tally.files_by_reason.get(reason, 0):
            problems.append(f"residual reason {reason}: {tally.files_by_reason[reason]} files; none should remain")
    wanted_total = sum(expected.values())
    if tally.final_files != wanted_total:
        problems.append(f"{tally.final_files} files left in the stage after the last page, the key expects {wanted_total}")
    return problems


def check_consumed(entries: Iterable[Entry], consumed: int) -> list[str]:
    wanted = count_consumable(entries)
    return [] if consumed == wanted else [f"consumed {consumed} staged copies, the key expects {wanted}"]
