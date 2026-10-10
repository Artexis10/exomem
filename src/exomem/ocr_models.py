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
"""

from __future__ import annotations

import collections
import csv
import functools
import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import media_brakes

DEFAULT_LANGS_ENV = "EXOMEM_OCR_DEFAULT_LANGS"

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
    if media_brakes.names_allocation_failure(result.stderr):
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
            if script not in _NEUTRAL_SCRIPTS:
                scripts[script] += 1
        top = scripts.most_common(1)
        return (top[0][0] if top else None), has_lstm
    return None, has_lstm


@functools.lru_cache(maxsize=4)
def _inventory(cmd: str) -> tuple[Model, ...]:
    listing = _run(cmd, ["--list-langs"]).stdout.splitlines()
    if not listing or '"' not in listing[0]:
        raise OcrUnavailable("Tesseract did not name its tessdata directory")
    # The first line is `List of available languages in "<dir>/" (<n>):`.
    tessdata = Path(listing[0].split('"')[1])
    models: list[Model] = []
    for name in (line.strip() for line in listing[1:]):
        if not name:
            continue
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
    return tuple(models)


def installed_models(cmd: str) -> tuple[Model, ...]:
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


def _read(cmd: str, image: Path, lang: str | None, psm: str, workdir: Path) -> tuple[float, str]:
    """Read once; return (mean word confidence, text)."""
    base = workdir / f"pass-{psm}"
    args = [str(image), str(base), "--psm", psm]
    if lang:
        args[2:2] = ["-l", lang]
    _run(cmd, [*args, "txt", "tsv"])
    text = base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace").strip()
    confidences: list[float] = []
    with base.with_suffix(".tsv").open(encoding="utf-8", errors="replace", newline="") as tsv:
        for row in csv.DictReader(tsv, delimiter="\t", quoting=csv.QUOTE_NONE):
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                continue
            if confidence >= 0 and (row.get("text") or "").strip():
                confidences.append(confidence)
    return (sum(confidences) / len(confidences) if confidences else 0.0), text


def read_image(cmd: str, image: Path) -> str:
    """OCR one image file: detect its script, then read with that script's models.

    A script with vertical models gets a horizontal and a vertical pass, and the
    pass Tesseract is more confident in wins.
    """
    plans = passes_for(installed_models(cmd), _detect_script(cmd, image))
    with tempfile.TemporaryDirectory(prefix="exomem-ocr-") as tmp:
        results = [_read(cmd, image, lang, psm, Path(tmp)) for lang, psm in plans]
    return max(results, key=lambda result: result[0])[1]
