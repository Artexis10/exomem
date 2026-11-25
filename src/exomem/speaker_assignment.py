"""Max-overlap assignment of transcript segments to diarization turns.

Each transcript unit (a Whisper segment) is assigned to the turn it overlaps most; ties go to
the earliest turn for determinism. Replaces naive midpoint assignment, which loses short turns
and mis-attributes boundary-straddling units.

Pure / torch-free (stdlib only) so it unit-tests without a GPU. Ported from a production
`speaker_assignment` module; consumed by `label_transcript`, the one place that turns ASR
segments and speaker turns into a labelled transcript, for in-cell speech (`extract._diarize`)
and for an attached speech result (`speech_attach`).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    label: str  # diarization cluster id or resolved speaker label
    track: str | None = None  # the microphone or stream whose voices the turn labels


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Length of the temporal intersection of [a0,a1] and [b0,b1] (0 if disjoint)."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_span(start: float, end: float, turns: Sequence[Turn]) -> Optional[str]:
    """Label of the turn with the greatest overlap with [start, end]; None if none overlaps.

    Ties (equal overlap) are broken toward the earliest-starting turn for determinism.
    """
    best_key: Optional[tuple[float, float]] = None
    best_label: Optional[str] = None
    for t in turns:
        ov = overlap(start, end, t.start, t.end)
        if ov <= 0.0:
            continue
        key = (ov, -t.start)  # maximize overlap, then prefer the earliest-starting turn
        if best_key is None or key > best_key:
            best_key = key
            best_label = t.label
    return best_label


def assign_spans(
    spans: Sequence[tuple[float, float]], turns: Sequence[Turn]
) -> list[Optional[str]]:
    """Assign a label to each (start, end) span by max overlap."""
    return [assign_span(s, e, turns) for (s, e) in spans]


def _letters(index: int) -> str:
    """The letters of the `index`-th voice of a track: A to Z, then AA, AB and on, so a label stays one closed shape."""
    letters, rest = "", index + 1
    while rest:
        rest, digit = divmod(rest - 1, 26)
        letters = chr(ord("A") + digit) + letters
    return letters


def label_transcript(
    segments: Sequence[object],
    turns: Sequence[Turn],
    *,
    resolved_labels: Mapping[str, str] | None = None,
    timed: bool = False,
    require_timed: bool = False,
    description: str = "the recording",
) -> tuple[str, list[dict]] | None:
    """Label ASR segments with speakers and render the transcript; None when nothing can be labelled.

    Each segment goes to the turn that overlaps it most (`assign_span`), among the turns of its own
    track. Raw turn labels become `Speaker A`, `Speaker B`, ... `Speaker Z`, `Speaker AA`, ... in order of first appearance within
    the track; a label in `resolved_labels` keeps the name it maps to instead, and takes no letter.
    A segment of a named track reads `<track>: <label>`, or the track alone where the track has no
    turns. Consecutive same-speaker segments merge into one turn. Returns `(text, merged_turns)`:
    `merged_turns` is the structured turn list that the `speakers` front matter and its filters
    read, and `text` is one timed line per segment when `timed` renders, else the merged turns as
    `[Speaker A]: ...`. A result with neither turns nor tracks has no labels: its text is the
    timed lines alone and its turn list is empty.

    A timed rendering that fails or is empty falls back to the merged turns, whether `timed` or
    `require_timed` asked for it. The caller sees from the text whether it carries times.
    """
    from . import semantic_segments

    by_track: dict[str | None, list[Turn]] = {}
    for turn in turns:
        by_track.setdefault(turn.track, []).append(turn)
    label_names: dict[tuple[str | None, str], str] = {}
    letters: dict[str | None, int] = {}

    def name_of(track: str | None, raw: str) -> str:
        if resolved_labels is not None and raw in resolved_labels:
            return resolved_labels[raw]
        if (track, raw) not in label_names:
            index = letters.get(track, 0)
            letters[track] = index + 1
            label_names[(track, raw)] = f"Speaker {_letters(index)}"
        return label_names[(track, raw)]

    labelled = bool(turns) or any(getattr(seg, "track", None) for seg in segments)
    merged: list[dict] = []
    timed_segs: list[tuple[float, float, str, str | None]] = []
    for seg in segments:
        seg_text = seg.text.strip()  # type: ignore[attr-defined]
        if not seg_text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", start) or start)
        track = getattr(seg, "track", None)
        speaker: str | None = None
        if labelled:
            pool = by_track.get(track, ())
            raw = assign_span(start, end, pool)
            if raw is not None or pool:
                speaker = name_of(track, raw) if raw is not None else "Speaker A"
            if track:
                speaker = f"{track}: {speaker}" if speaker else track
            speaker = speaker or "Speaker A"
        timed_segs.append((start, end, seg_text, speaker))
        if not labelled:
            continue
        if merged and merged[-1]["speaker"] == speaker:
            merged[-1]["text"] += " " + seg_text
            merged[-1]["end"] = end
        else:
            merged.append({"speaker": speaker, "start": start, "end": end, "text": seg_text})
    if not timed_segs:
        return None
    # Timed rendering: one line per ASR segment with the label repeated, because merged
    # multi-minute turns would destroy segmentation windows and match localization. The
    # structured `merged` list keeps the merged-turn shape either way.
    if timed or require_timed or not labelled:
        try:
            timed_text = semantic_segments.render_timed_lines(timed_segs).strip()
            if timed_text:
                return timed_text, merged
            if require_timed:
                raise RuntimeError("timed diarized rendering produced no output")
        except Exception:  # noqa: BLE001 - labelling is optional and soft-failing
            log.warning("timed diarized rendering failed for %s; using merged turns", description)
    if not labelled:
        return "\n".join(text for _start, _end, text, _speaker in timed_segs), merged
    return "\n".join(f"[{m['speaker']}]: {m['text']}" for m in merged).strip(), merged
