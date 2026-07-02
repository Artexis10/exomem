"""Semantic video segments (`EXOMEM_SEMANTIC_SEGMENTS`).

Makes the *moment* the retrieval unit for timed media. Three cooperating parts:

1. **Timed transcript codec** — `format_ts`/`TIMED_LINE_RE`/`parse_timed_lines`/
   `render_timed_lines`. Extraction renders one line per ASR segment
   (`[51:20] …`, diarized `[51:20] [Alice]: …`); everything downstream parses
   timestamps straight out of the sidecar text — no side store.
2. **Segmenter** — transcript-topic boundaries from embedding-similarity valleys
   over sliding line windows (TextTiling-style depth), FUSED with deterministic
   events: visual scene changes (persisted frame filenames), speaker-turn
   changes, and OCR-change events between consecutive frame sidecars. Pure
   measurement — no reasoning model (the pure-substrate rule).
3. **Chunking** — each segment becomes one embedding chunk whose first line
   carries the segment's `[timestamp]`, which `find` surfaces as
   `transcript_match_at`.

Soft-fail ladder: full fusion → events-only when embedding fails → `None`
(caller falls back to the ordinary paragraph chunker).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)


_FALSY_ENV = {"", "0", "false", "no", "off"}


def semantic_segments_enabled() -> bool:
    """`EXOMEM_SEMANTIC_SEGMENTS` gates timed rendering AND segment chunking.

    Truthy opt-in (same parse as `EXOMEM_DIARIZE`): unset, '', '0', 'false',
    'no', 'off' → OFF. Default OFF: extraction output and chunking stay
    byte-identical to the pre-feature behavior.
    """
    return os.environ.get("EXOMEM_SEMANTIC_SEGMENTS", "").strip().lower() not in _FALSY_ENV


# --- Timestamp codec ----------------------------------------------------------

# `[m:ss]` under an hour, `[h:mm:ss]` from an hour — identical semantics to the
# find-side formatter so `transcript_match_at` reads like `clip_match_at`.
TIMED_LINE_RE = re.compile(
    r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\](?:\s\[([^\]\n]+)\]:)?\s?(.*)$"
)


def format_ts(seconds: float) -> str:
    """Seconds → `m:ss` (or `h:mm:ss` past an hour)."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class TimedLine(NamedTuple):
    ts: float
    speaker: str | None
    text: str


def ts_from_match(m: re.Match) -> float:
    """Seconds from a TIMED_LINE_RE match."""
    hours, minutes, secs = m.group(1), m.group(2), m.group(3)
    return float(int(hours or 0) * 3600 + int(minutes) * 60 + int(secs))


def parse_timed_lines(block: str) -> list[TimedLine]:
    """Parse a timed transcript block → `[TimedLine]`.

    Lines without a leading `[ts]` marker fold into the previous line's text
    (defensive: manual edits, wrapped lines). Leading unmatched lines are
    skipped.
    """
    lines: list[TimedLine] = []
    for raw in block.splitlines():
        raw = raw.rstrip()
        if not raw:
            continue
        m = TIMED_LINE_RE.match(raw)
        if m:
            lines.append(TimedLine(ts_from_match(m), m.group(4), m.group(5).strip()))
        elif lines:
            prev = lines[-1]
            lines[-1] = TimedLine(prev.ts, prev.speaker, f"{prev.text} {raw.strip()}".strip())
    return lines


def render_timed_lines(segs: list[tuple[float, float, str, str | None]]) -> str:
    """`[(start, end, text, speaker|None)]` → one timed line per segment.

    Empty/whitespace-only segments are skipped. Diarized lines repeat the
    speaker label per segment on purpose — merged multi-minute turns would
    destroy both segmentation windows and match localization.
    """
    out: list[str] = []
    for start, _end, text, speaker in segs:
        text = (text or "").strip()
        if not text:
            continue
        if speaker:
            out.append(f"[{format_ts(start)}] [{speaker}]: {text}")
        else:
            out.append(f"[{format_ts(start)}] {text}")
    return "\n".join(out)


def _render_line(line: TimedLine) -> str:
    if line.speaker:
        return f"[{format_ts(line.ts)}] [{line.speaker}]: {line.text}"
    return f"[{format_ts(line.ts)}] {line.text}"


# --- Segmenter ------------------------------------------------------------------
#
# Boundary score per gap g (between line g and g+1, boundary ts = line g+1's ts):
#   score = topic + 0.5*scene(±5s) + 0.35*speaker_change + 0.4*ocr(±5s)
# where topic is the TextTiling depth of the embedding-similarity valley,
# normalized by DEPTH_NORM (unclamped — ranking needs the deepest valley to win
# over its clamped-equal neighbours). A gap is a boundary at score >= 0.55: a
# deep valley alone passes; a moderate valley plus any corroborating event
# passes; scene+OCR+speaker together pass without a valley.

MIN_TIMED_LINES = 8  # fewer → fall back to the paragraph chunker
WINDOW_LINES = 4  # sliding-window width for topic embeddings
DEPTH_NORM = 0.4  # valley depth that counts as a full topic signal
SCENE_WEIGHT = 0.5
SPEAKER_WEIGHT = 0.35
OCR_WEIGHT = 0.4
BOUNDARY_THRESHOLD = 0.55
EVENT_PROXIMITY_SECS = 5.0
MIN_SEG_SECS = 25.0  # closer accepted boundaries: keep the higher score
MAX_SEGMENTS = 256
OCR_JACCARD_THRESHOLD = 0.5  # below → an OCR-change event
# Stay under the text chunker's word cap so segment chunks never get truncated.
MAX_SEG_WORDS_MARGIN = 30

EmbedFn = Callable[[list[str]], "np.ndarray"]


class Events(NamedTuple):
    scene_ts: list[float]
    ocr_ts: list[float]


def _default_embed_fn(texts: list[str]) -> np.ndarray:
    from . import embeddings  # lazy: keep this module import-light

    return embeddings.embed_texts(texts, is_query=False)


def _max_seg_words() -> int:
    from . import embeddings  # lazy — single source of truth for the chunk cap

    return embeddings.MAX_WORDS_PER_CHUNK - MAX_SEG_WORDS_MARGIN


def _topic_scores(lines: list[TimedLine], embed_fn: EmbedFn) -> list[float]:
    """TextTiling-style valley depth per gap, normalized by DEPTH_NORM (unclamped)."""
    n_gaps = len(lines) - 1
    left_texts = [
        " ".join(li.text for li in lines[max(0, g - WINDOW_LINES + 1) : g + 1])
        for g in range(n_gaps)
    ]
    right_texts = [
        " ".join(li.text for li in lines[g + 1 : g + 1 + WINDOW_LINES])
        for g in range(n_gaps)
    ]
    unique = sorted(set(left_texts) | set(right_texts))
    vecs = np.asarray(embed_fn(unique), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.maximum(norms, 1e-9)  # defensive: fakes may not normalize
    by_text = {t: vecs[i] for i, t in enumerate(unique)}
    sim = np.array(
        [float(by_text[left_texts[g]] @ by_text[right_texts[g]]) for g in range(n_gaps)]
    )
    # Running peaks to each side of every gap.
    peak_left = np.maximum.accumulate(sim)
    peak_right = np.maximum.accumulate(sim[::-1])[::-1]
    depth = ((peak_left - sim) + (peak_right - sim)) / 2.0
    return [float(d) / DEPTH_NORM for d in depth]


def _near(events: list[float], ts: float) -> bool:
    return any(abs(e - ts) <= EVENT_PROXIMITY_SECS for e in events)


def _select_boundaries(scores: list[tuple[int, float, float]]) -> list[int]:
    """Greedy accept by (score desc, ts asc): threshold + min-distance + cap.

    `scores` is [(gap_index, boundary_ts, score)]. Returns accepted gap indices
    in ascending order.
    """
    candidates = [c for c in scores if c[2] >= BOUNDARY_THRESHOLD]
    candidates.sort(key=lambda c: (-c[2], c[1]))
    accepted_ts: list[float] = []
    accepted: list[int] = []
    for gap, ts, _score in candidates:
        if len(accepted) >= MAX_SEGMENTS - 1:
            break
        if any(abs(ts - a) < MIN_SEG_SECS for a in accepted_ts):
            continue
        accepted.append(gap)
        accepted_ts.append(ts)
    return sorted(accepted)


def _force_split_by_words(lines: list[TimedLine], gap_scores: list[float]) -> list[list[TimedLine]]:
    """Split an over-long run of lines at its best interior gap until each part
    fits the word budget — nothing silently truncates."""
    words = sum(len(li.text.split()) for li in lines)
    if words <= _max_seg_words() or len(lines) < 2:
        return [lines]
    # Best interior gap by score; ties/no-signal → the gap nearest the word midpoint.
    cum = np.cumsum([len(li.text.split()) for li in lines])
    target = words / 2.0
    best_gap = None
    best_key = None
    for g in range(len(lines) - 1):
        score = gap_scores[g] if g < len(gap_scores) else 0.0
        key = (-score, abs(float(cum[g]) - target))
        if best_key is None or key < best_key:
            best_key = key
            best_gap = g
    left, right = lines[: best_gap + 1], lines[best_gap + 1 :]
    return _force_split_by_words(left, gap_scores[: best_gap + 1]) + _force_split_by_words(
        right, gap_scores[best_gap + 1 :]
    )


def segment_transcript(
    block: str,
    *,
    events: Events | None = None,
    embed_fn: EmbedFn | None = None,
) -> list[str] | None:
    """Segment a timed transcript block → list of segment strings (timed lines,
    markers included; the first line's `[ts]` is the segment start).

    Soft-fail ladder: embedding failure ⇒ events-only boundaries; fewer than
    MIN_TIMED_LINES timed lines, or any other error ⇒ None (the caller falls
    back to the ordinary paragraph chunker).
    """
    try:
        lines = parse_timed_lines(block)
        if len(lines) < MIN_TIMED_LINES:
            return None
        events = events or Events([], [])
        n_gaps = len(lines) - 1
        try:
            topic = _topic_scores(lines, embed_fn or _default_embed_fn)
        except Exception as e:  # noqa: BLE001 — ladder step: events-only
            log.warning("topic embedding failed (%s); segmenting on events only", e)
            topic = [0.0] * n_gaps
        start_ts = lines[0].ts
        scored: list[tuple[int, float, float]] = []
        gap_scores: list[float] = []
        for g in range(n_gaps):
            ts = lines[g + 1].ts
            speaker_change = (
                lines[g].speaker is not None
                and lines[g + 1].speaker is not None
                and lines[g].speaker != lines[g + 1].speaker
            )
            score = (
                topic[g]
                + (SCENE_WEIGHT if _near(events.scene_ts, ts) else 0.0)
                + (SPEAKER_WEIGHT if speaker_change else 0.0)
                + (OCR_WEIGHT if _near(events.ocr_ts, ts) else 0.0)
            )
            gap_scores.append(score)
            if ts - start_ts >= MIN_SEG_SECS:  # no boundary in the opening seconds
                scored.append((g, ts, score))
        boundaries = _select_boundaries(scored)
        # Cut into line runs, then enforce the word budget per run.
        runs: list[list[TimedLine]] = []
        prev = 0
        for g in boundaries:
            runs.append(lines[prev : g + 1])
            prev = g + 1
        runs.append(lines[prev:])
        out: list[list[TimedLine]] = []
        offset = 0
        for run in runs:
            out.extend(_force_split_by_words(run, gap_scores[offset : offset + len(run) - 1]))
            offset += len(run)
        return ["\n".join(_render_line(li) for li in seg) for seg in out if seg]
    except Exception as e:  # noqa: BLE001 — ladder floor: paragraph chunker
        log.warning("semantic segmentation failed (%s); falling back", e)
        return None


# --- Boundary events from persisted scene frames --------------------------------


_REAL_ENGINE_BAD = ("none", "pending")


def _frame_ocr_tokens(sidecar: Path) -> set[str] | None:
    """Token set of a frame sidecar's OCR text; None when extraction isn't done."""
    try:
        content = sidecar.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"(?m)^extracted_by:\s*(.+?)\s*$", content[:800])
    if not m:
        return None
    engine = m.group(1).strip()
    if engine in _REAL_ENGINE_BAD or engine.startswith("failed:"):
        return None
    idx = content.find("## Extracted text")
    if idx == -1:
        return set()
    body = content[idx + len("## Extracted text") :]
    nxt = body.find("\n## ")
    if nxt != -1:
        body = body[:nxt]
    body = body.replace("(no text detected)", "")
    return set(re.findall(r"[a-z0-9]+", body.lower()))


def gather_events(vault_root: Path, media_rel: str) -> Events:
    """Boundary events for a video from its persisted scene frames.

    Visual events are the MIDPOINTS between consecutive representative-frame
    timestamps (rep_ts marks a scene's middle, so the change lies between two
    reps — the ±EVENT_PROXIMITY_SECS window absorbs the approximation). OCR
    events are those midpoints where consecutive frames' OCR token sets have
    Jaccard overlap below OCR_JACCARD_THRESHOLD. Soft: any error ⇒ no events.
    """
    try:
        from . import scene_frames  # lazy: avoid import cycles

        frames_dir = vault_root / (media_rel + scene_frames.FRAMES_DIR_SUFFIX)
        if not frames_dir.is_dir():
            return Events([], [])
        frames: list[tuple[float, Path]] = []
        for f in frames_dir.iterdir():
            ts = scene_frames.parse_frame_ts(f.name)
            if ts is not None:
                frames.append((ts, f))
        frames.sort()
        scene_ts: list[float] = []
        ocr_ts: list[float] = []
        for (ts_a, f_a), (ts_b, f_b) in zip(frames, frames[1:], strict=False):
            mid = (ts_a + ts_b) / 2.0
            scene_ts.append(mid)
            tok_a = _frame_ocr_tokens(f_a.with_name(f_a.name + ".md"))
            tok_b = _frame_ocr_tokens(f_b.with_name(f_b.name + ".md"))
            if tok_a is None or tok_b is None:
                continue
            union = tok_a | tok_b
            jaccard = (len(tok_a & tok_b) / len(union)) if union else 1.0
            if jaccard < OCR_JACCARD_THRESHOLD:
                ocr_ts.append(mid)
        return Events(scene_ts, ocr_ts)
    except Exception as e:  # noqa: BLE001 — events are an enrichment, never fatal
        log.warning("gather_events failed for %s: %s", media_rel, e)
        return Events([], [])
