"""Script-first OCR over the Tesseract models this deployment installs.

Orientation and script detection (Tesseract OSD) runs first. The image is then read
with the installed script models for the detected script and with each installed
language pack written in that script (`multilingual-recall`, "OCR reads an image
with the installed models for its script"). Nothing here lists a language or a
script: each model's script comes from its own character set, and the installed set
is whatever the deployment put in the tessdata directory.

Tesseract runs as a subprocess, so it inherits the media child's data-segment limit.
Its allocation failures reach us only on stderr, and Leptonica can report one with
exit status 0 and partial text, so this module reads stderr on every run.

Reading each model's script means reading every traineddata file (hundreds of MB), so
the result is cached in a small JSON file keyed by the tessdata directory's listing.
The Cloud image writes that file at build time; a personal install writes it on first
use and again whenever its packs change.
"""

from __future__ import annotations

import collections
import contextlib
import csv
import functools
import hashlib
import json
import logging
import os
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_LANGS_ENV = "EXOMEM_OCR_DEFAULT_LANGS"
INVENTORY_ENV = "EXOMEM_OCR_INVENTORY"
WORD_FLOOR_ENV = "EXOMEM_OCR_WORD_CONFIDENCE_FLOOR"
# Tesseract reports a word's confidence on a 0-100 scale. A floor of 70 raised token precision
# by 0.25 (95% CI 0.11 to 0.42) for a recall loss of 0.03 on 16 images with reference
# transcriptions (measured 2026-11-01); 0 keeps every word.
DEFAULT_WORD_FLOOR = 70.0

# traineddata layout (tessdatamanager.h): an int32 entry count, then one int64
# offset per component, -1 when absent. These indexes are TessdataType values.
_LSTM = 17
_LSTM_UNICHARSET = 21
_LEGACY_UNICHARSET = 1
# Unicode script values that belong to no single script (UAX #24); Tesseract's
# unicharset uses the same names.
_NEUTRAL_SCRIPTS = frozenset({"Common", "Inherited"})
# Tesseract names a vertical-text model `<name>_vert` (tessdata naming scheme).
_VERTICAL_SUFFIX = "_vert"
# Page segmentation modes (tesseract --help-psm): 0 = OSD only, 3 = automatic
# layout, 5 = one uniform block of vertical text. Automatic layout misreads
# vertical CJK columns, so vertical models read a page as one vertical block.
_PSM_OSD = "0"
_PSM_AUTO = "3"
_PSM_VERTICAL = "5"
# The tokens Tesseract's libraries print on stderr when an allocation fails (Leptonica
# "malloc fail", std::bad_alloc). Looking for them anywhere in stderr is safe because
# Tesseract echoes only the arguments this module passes: its own temp file names and
# the deployment's tessdata paths, never a tenant's file name.
_ALLOCATOR_TOKENS = ("bad_alloc", "malloc", "calloc", "realloc")


class OcrMemoryExhausted(MemoryError):
    """Tesseract could not allocate memory under the media child's data limit."""


class OcrUnavailable(Exception):
    """No Tesseract binary, or it cannot list its models."""


class OcrFailed(Exception):
    """Tesseract refused the input for a reason other than memory."""


@dataclass(frozen=True)
class Model:
    #: The name Tesseract's `-l` takes, such as `jpn`, `Japanese` or `script/Latin`.
    name: str
    #: The script most of its letters are written in, from its own unicharset.
    script: str | None
    #: A script model reads every language of its script; a language pack, one language.
    is_script_model: bool
    vertical: bool

    @property
    def base(self) -> str:
        return self.name.rsplit("/", 1)[-1]


def _run(cmd: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603 - the resolved Tesseract binary, fixed arguments
            [cmd, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError as error:
        raise OcrUnavailable(f"Tesseract binary not found: {cmd}") from error
    if any(token in result.stderr for token in _ALLOCATOR_TOKENS):
        raise OcrMemoryExhausted(f"Tesseract allocation failed: {result.stderr.strip()[-300:]}")
    if result.returncode != 0:
        raise OcrFailed(f"Tesseract exited {result.returncode}: {result.stderr.strip()[-300:]}")
    return result


def _plurality_script(path: Path) -> tuple[str | None, bool]:
    """Return (the script most of the model's letters use, whether it has an LSTM)."""
    data = path.read_bytes()
    (count,) = struct.unpack_from("<i", data, 0)
    offsets = struct.unpack_from(f"<{count}q", data, 4)
    has_lstm = count > _LSTM and offsets[_LSTM] != -1
    for index in (_LSTM_UNICHARSET, _LEGACY_UNICHARSET):
        if index >= count or offsets[index] == -1:
            continue
        start = offsets[index]
        end = min((offset for offset in offsets if offset > start), default=len(data))
        lines = data[start:end].decode("utf-8", "replace").splitlines()[1:]
        scripts: collections.Counter[str] = collections.Counter()
        for line in lines:
            fields = line.split(" ")
            # `<char> <props> [<metrics>] <script> ...`; metrics hold commas.
            if len(fields) < 3:
                continue
            script = fields[3] if "," in fields[2] and len(fields) > 3 else fields[2]
            # nosemgrep: ep-lexical-intent -- UAX #24 fixes these script values; unicharset data.
            if script not in _NEUTRAL_SCRIPTS:
                scripts[script] += 1
        top = scripts.most_common(1)
        return (top[0][0] if top else None), has_lstm
    return None, has_lstm


def _inventory_path() -> Path:
    configured = (os.environ.get(INVENTORY_ENV) or "").strip()
    return Path(configured) if configured else Path.home() / ".cache" / "exomem" / "ocr-inventory.json"


def _inventory_key(tessdata: Path, names: list[str]) -> str:
    """Name, size and mtime of each listed model: what changes when a pack changes."""
    entries = [str(tessdata)]
    for name in sorted(names):
        try:
            stat = (tessdata / f"{name}.traineddata").stat()
        except OSError:
            entries.append(f"{name} missing")
            continue
        entries.append(f"{name} {stat.st_size} {stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _cached_models(path: Path, key: str) -> tuple[Model, ...] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("key") != key:
            return None
        return tuple(Model(**entry) for entry in cached["models"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _store_models(path: Path, key: str, models: tuple[Model, ...]) -> None:
    """Best effort: a read-only image or home only costs the next process a re-read."""
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".ocr-inventory-", delete=False
        ) as handle:
            temporary = handle.name
            json.dump({"key": key, "models": [asdict(model) for model in models]}, handle)
        # Model names only, no secret: the Cloud image writes it as root at build time
        # and the cell's own user reads it.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except OSError as error:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        log.debug("OCR inventory cache not written to %s: %s", path, error)


@functools.lru_cache(maxsize=4)
def _inventory(cmd: str) -> tuple[Model, ...] | None:
    """The installed models, or None when this Tesseract cannot name its tessdata."""
    listing = _run(cmd, ["--list-langs"]).stdout.splitlines()
    if not listing or '"' not in listing[0]:
        return None  # Tesseract 4.x lists languages without their directory
    # The first line is `List of available languages in "<dir>/" (<n>):`.
    tessdata = Path(listing[0].split('"')[1])
    names = [name for name in (line.strip() for line in listing[1:]) if name]
    key = _inventory_key(tessdata, names)
    path = _inventory_path()
    cached = _cached_models(path, key)
    if cached is not None:
        return cached
    models: list[Model] = []
    for name in names:
        try:
            script, has_lstm = _plurality_script(tessdata / f"{name}.traineddata")
        except (OSError, struct.error):
            continue
        if not has_lstm:
            continue  # osd and legacy-only packs cannot read in the default LSTM mode
        models.append(
            Model(
                name=name,
                script=script,
                # Tesseract publishes script models under `script/` and names them
                # after their script, capitalised; language packs use lower-case codes.
                is_script_model=name.startswith("script/") or name[:1].isupper(),
                vertical=name.endswith(_VERTICAL_SUFFIX),
            )
        )
    _store_models(path, key, tuple(models))
    return tuple(models)


def installed_models(cmd: str) -> tuple[Model, ...] | None:
    return _inventory(cmd)


def passes_for(models: tuple[Model, ...], detected: str | None) -> list[tuple[str | None, str]]:
    """The (languages, page segmentation mode) passes that read a page of `detected`.

    A script's models are named after it (`Japanese`, `Japanese_vert`; `HanS`,
    `HanT` for Han). Its language packs are the packs whose letters are mostly in
    the script those models read, so kanji-only text that OSD calls Han is read
    with the Japanese packs, and a Latin page never loads them.
    """
    chosen: list[Model] = []
    if detected:
        own = [m for m in models if m.is_script_model and m.base.startswith(detected)]
        # A name prefix also catches another script's model (`Han` -> `Hangul`); where
        # some models' own letters are the detected script, those are its models.
        own = [m for m in own if m.script == detected] or own
        scripts = {m.script for m in own if m.script} or {detected}
        chosen = own + [m for m in models if not m.is_script_model and m.script in scripts]
    if not chosen:
        # No script, or none installed for it: the deployment's default packs, or
        # Tesseract's own default when the deployment names none.
        named = set((os.environ.get(DEFAULT_LANGS_ENV) or "").split("+"))
        chosen = [m for m in models if m.name in named]
    if not chosen:
        return [(None, _PSM_AUTO)]
    plans: list[tuple[str | None, str]] = []
    horizontal = [m.name for m in chosen if not m.vertical]
    vertical = [m.name for m in chosen if m.vertical]
    if horizontal:
        plans.append(("+".join(horizontal), _PSM_AUTO))
    if vertical:
        plans.append(("+".join(vertical), _PSM_VERTICAL))
    return plans


def _detect_script(cmd: str, image: Path) -> str | None:
    try:
        result = _run(cmd, [str(image), "stdout", "--psm", _PSM_OSD])
    except OcrFailed:
        return None  # too few characters, or no osd model: detection is inconclusive
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Script" and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class OcrWord:
    #: Tesseract's own confidence for the word, 0-100.
    confidence: float
    text: str


@dataclass(frozen=True)
class OcrReading:
    """One pass of Tesseract: its own text, and each word it read with its confidence."""

    #: Tesseract's text output, in its own line layout and spacing.
    text: str
    #: Every word of `text` in reading order.
    words: tuple[OcrWord, ...]

    @property
    def confidence(self) -> float | None:
        """Mean word confidence; None when Tesseract read no word, so nothing was measured."""
        if not self.words:
            return None
        return sum(word.confidence for word in self.words) / len(self.words)

    def split(self, floor: float) -> tuple[str, str]:
        """`(kept, dropped)`: the text with the words under `floor` removed, and those words alone.

        Each word is removed from Tesseract's own text line, so the words that stay keep the
        spacing Tesseract measured, CJK included. A reading whose words cannot all be placed
        in that text, or a floor of 0, keeps the whole text: nothing is withheld that is not
        proven to be a low-confidence word.
        """
        if floor <= 0 or not self.words:
            return self.text, ""
        spans = self._spans()
        if spans is None:
            log.warning("OCR words do not align with Tesseract's text; keeping the whole text")
            return self.text, ""
        under = [span for word, span in zip(self.words, spans, strict=True) if word.confidence < floor]
        if not under:
            return self.text, ""
        over = [span for word, span in zip(self.words, spans, strict=True) if word.confidence >= floor]
        return _without_spans(self.text, under), _without_spans(self.text, over)

    def _spans(self) -> list[tuple[int, int]] | None:
        """Where each word sits in `text`, in reading order; None unless the words are all of it."""
        spans: list[tuple[int, int]] = []
        cursor = 0
        for word in self.words:
            while cursor < len(self.text) and self.text[cursor].isspace():
                cursor += 1
            if not self.text.startswith(word.text, cursor):
                return None
            spans.append((cursor, cursor + len(word.text)))
            cursor += len(word.text)
        return spans if not self.text[cursor:].strip() else None


def _without_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """`text` with the spans removed, each with the spaces after it; a line left empty goes.

    `spans` are ascending and each lies inside one line.
    """
    lines: list[str] = []
    offset = 0
    pending = 0
    for line in text.split("\n"):
        end = offset + len(line)
        pieces: list[str] = []
        cursor = offset
        while pending < len(spans) and spans[pending][0] < end:
            start, stop = spans[pending]
            pending += 1
            pieces.append(text[cursor:start])
            cursor = stop
            while cursor < end and text[cursor] in " \t":
                cursor += 1
        pieces.append(text[cursor:end])
        remaining = "".join(pieces).rstrip()
        if remaining or not line.strip():
            lines.append(remaining)
        offset = end + 1
    # A paragraph that lost every line leaves two blank lines side by side.
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def word_floor() -> float:
    """The confidence under which a word is withheld from search (`WORD_FLOOR_ENV`)."""
    try:
        return float(os.environ.get(WORD_FLOOR_ENV, DEFAULT_WORD_FLOOR))
    except ValueError:
        return DEFAULT_WORD_FLOOR


def _read(cmd: str, image: Path, lang: str | None, psm: str, workdir: Path) -> OcrReading:
    """Read once: Tesseract's text and the confidence of each word in it."""
    base = workdir / f"pass-{psm}"
    # Space only at the gaps Tesseract measured; by default it spaces apart each CJK word.
    args = [str(image), str(base), "--psm", psm, "-c", "preserve_interword_spaces=1"]
    if lang:
        args[2:2] = ["-l", lang]
    _run(cmd, [*args, "txt", "tsv"])
    text = base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace").strip()
    words: list[OcrWord] = []
    with base.with_suffix(".tsv").open(encoding="utf-8", errors="replace", newline="") as tsv:
        for row in csv.DictReader(tsv, delimiter="\t", quoting=csv.QUOTE_NONE):
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                continue
            word = (row.get("text") or "").strip()
            if confidence >= 0 and word:
                words.append(OcrWord(confidence, word))
    return OcrReading(text, tuple(words))


def read_image(cmd: str, image: Path) -> OcrReading:
    """OCR one image file: detect its script, then read with that script's models.

    A script with vertical models gets a horizontal and a vertical pass, and the
    pass Tesseract is more confident in wins.
    """
    models = installed_models(cmd)
    if models is None:
        # Without its tessdata directory the models' scripts cannot be read: one pass in
        # Tesseract's default language, as OCR read before script detection.
        plans: list[tuple[str | None, str]] = [(None, _PSM_AUTO)]
    else:
        plans = passes_for(models, _detect_script(cmd, image))
    with tempfile.TemporaryDirectory(prefix="exomem-ocr-") as tmp:
        readings = [_read(cmd, image, lang, psm, Path(tmp)) for lang, psm in plans]
    # A pass that read no word has no confidence and ranks as 0.0, as it always did.
    return max(readings, key=lambda reading: reading.confidence or 0.0)
