"""Attach a speech result that the owner's agent computed on its own machine to a recording's Source.

A cell cannot diarise (pyannote needs PyTorch and more memory than a cell holds), and a personal
install can. The owner's agent transcribes and diarises there, then calls `media.attach-speech`
with the result and the original's SHA-256 and size. The cell checks that the result fits the
recording, renders it with the code that renders its own speech (`speaker_assignment.label_transcript`),
and publishes it through the extraction publication (`prior_extraction.extraction_writes`). The
Source says that the result was attached: the cell never ran the engine and cannot check it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from . import media_engines, media_jobs, media_processing, originals, prior_extraction, speaker_names
from .asr_facts import AsrFacts
from .speaker_assignment import Turn, label_transcript
from .speech_attach_schema import (
    ATTACH_OPERATION,
    MAX_CHARS_PER_SECOND,
    MAX_SEGMENTS_PER_SECOND,
    MAX_TURNS_PER_SECOND,
    TIME_TOLERANCE_FRACTION,
    TIME_TOLERANCE_SECONDS,
)
from .vault import PlannedWrite, batch_atomic_write, parse_frontmatter, post_commit_batch_fanout, read_guarded_text

log = logging.getLogger(__name__)


_Committed = TypeVar("_Committed")


def refuse(code: str, reason: str) -> media_processing.MediaProcessingError:
    return media_processing.MediaProcessingError(code, reason)


@dataclass(frozen=True)
class Result:
    """The call's speech result in one shape. The door's schema has checked each field's type and size."""

    engine: str
    segments: tuple[Mapping[str, Any], ...]
    turns: tuple[Mapping[str, Any], ...]
    names: tuple[Mapping[str, str], ...]
    asr: Mapping[str, Any]
    replace_labelled: bool


def parse(
    *,
    engine: str,
    segments: Sequence[Mapping[str, Any]],
    turns: Sequence[Mapping[str, Any]] | None = None,
    names: Sequence[Mapping[str, str]] | None = None,
    asr: Mapping[str, Any] | None = None,
    replace_labelled: bool = False,
) -> Result:
    """The call's fields as a `Result`, or a refusal that names the rule the fields break."""
    lowered = engine.strip().lower()
    # The page schema reads these engine values as "no result yet", which would hide the result they came with.
    if lowered in media_processing._INCOMPLETE_ENGINES or lowered.startswith("failed") or len(engine.splitlines()) > 1:  # noqa: SLF001 - the page schema's own closed states
        raise refuse("ATTACH_PAYLOAD_INVALID", "engine must name, on one line, the engine that produced the result")
    for index, item in enumerate(segments):
        text = item["text"].strip()
        if not text:
            raise refuse("ATTACH_PAYLOAD_INVALID", f"segment {index} has no text")
        if len(text.splitlines()) > 1:
            # The page stores one line for each segment, and a break would start a line that reads as a label.
            raise refuse("ATTACH_PAYLOAD_INVALID", f"segment {index} text must be one line")
        if item.get("end") is not None and item["end"] < item["start"]:
            raise refuse("ATTACH_PAYLOAD_INVALID", f"segment {index} ends before it starts")
        _check_track("segment", index, item.get("track"))
    for index, item in enumerate(turns or ()):
        if item["end"] < item["start"]:
            raise refuse("ATTACH_PAYLOAD_INVALID", f"turn {index} ends before it starts")
        if item["label"] != item["label"].strip() or len(item["label"].splitlines()) != 1:
            raise refuse("ATTACH_PAYLOAD_INVALID", f"turn {index} label must be one line with no outer space")
        _check_track("turn", index, item.get("track"))
    _check_tracks_meet(segments, turns or ())
    _check_names(names or (), labels={item["label"] for item in turns or ()})
    try:
        AsrFacts.from_payload(dict(asr or {}))
    except ValueError as error:
        raise refuse("ATTACH_PAYLOAD_INVALID", f"asr: {error}") from error
    ordered = sorted(segments, key=lambda item: item["start"])
    return Result(engine.strip(), tuple(ordered), tuple(turns or ()), tuple(names or ()), dict(asr or {}), replace_labelled)


def _check_track(kind: str, index: int, track: str | None) -> None:
    """Refuse a track that the label syntax cannot hold: it stands in the label span of every line it labels."""
    problem = None if track is None else speaker_names.name_problem(track, "a track")
    if problem is not None:
        raise refuse("ATTACH_PAYLOAD_INVALID", f"{kind} {index}: {problem}")


def _check_tracks_meet(segments: Sequence[Mapping[str, Any]], turns: Sequence[Mapping[str, Any]]) -> None:
    """Refuse turns on a track that no segment carries: a turn labels only the segments of its own track."""
    carried = {item.get("track") for item in segments}
    for index, item in enumerate(turns):
        if item.get("track") not in carried:
            raise refuse(
                "ATTACH_PAYLOAD_INVALID",
                f"turn {index} is on track {item.get('track')!r}, which no segment carries; "
                "give the turns and the segments they label the same track",
            )


def _check_names(names: Sequence[Mapping[str, str]], *, labels: set[str]) -> None:
    """Refuse a name the label syntax cannot hold, a label no turn carries, or a label or name used twice."""
    seen_labels: set[str] = set()
    seen_names: set[str] = set()
    for item in names:
        problem = speaker_names.name_problem(item["name"])
        if problem is not None:
            raise refuse("SPEAKER_NAME_INVALID", problem)
        if item["label"] not in labels:
            raise refuse("SPEAKER_LABEL_UNKNOWN", f"no turn of the call carries the label {item['label']!r}")
        if item["label"] in seen_labels or item["name"] in seen_names:
            raise refuse("SPEAKER_NAME_IN_USE", "each label takes one name, and each name stays on one label")
        seen_labels.add(item["label"])
        seen_names.add(item["name"])


def result_digest(result: Result) -> str:
    """Identify the attached result by what the call states, so a repeat of the call matches it."""
    material = {
        "engine": result.engine,
        "asr": dict(result.asr),
        "segments": [[s["start"], s.get("end"), s["text"], s.get("track")] for s in result.segments],
        "turns": [[t["start"], t["end"], t["label"], t.get("track")] for t in result.turns],
        "names": sorted([n["label"], n["name"], n["basis"]] for n in result.names),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def check_bounds(result: Result, duration: float) -> None:
    """Refuse a result that does not fit a recording of `duration` seconds, naming the bound it breaks."""
    reach = duration + max(TIME_TOLERANCE_SECONDS, duration * TIME_TOLERANCE_FRACTION)
    for kind, items in (("segment", result.segments), ("turn", result.turns)):
        for index, item in enumerate(items):
            if max(item["start"], item.get("end") or 0.0) > reach:
                raise refuse(
                    "ATTACH_PAYLOAD_OUT_OF_BOUNDS",
                    f"{kind} {index} runs past the recording's {duration:.1f} seconds and its {reach - duration:.1f} of tolerance",
                )
    # The tolerance is part of the length these rates apply to, so a short clip is not refused for its rounding.
    for bound, count, rate in (
        ("segments per second", len(result.segments), MAX_SEGMENTS_PER_SECOND),
        ("turns per second", len(result.turns), MAX_TURNS_PER_SECOND),
        ("characters per second", sum(len(s["text"]) for s in result.segments), MAX_CHARS_PER_SECOND),
    ):
        if count > rate * reach:
            raise refuse(
                "ATTACH_PAYLOAD_OUT_OF_BOUNDS",
                f"{bound}: {count} is more than {rate} for each of the recording's {reach:.0f} seconds",
            )


def container_duration(path: Path) -> float | None:
    """The duration in seconds that the container states, read from its metadata without decoding.

    None when the container states none, or when PyAV (the `frame-codecs` extra) is not installed.
    """
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as container:
            if container.duration:
                return float(container.duration) / av.time_base
            stated = [
                float(stream.duration * stream.time_base)
                for stream in container.streams
                if stream.duration and stream.time_base
            ]
    except (av.error.FFmpegError, OSError, ValueError):
        return None
    return max(stated, default=None)


@dataclass(frozen=True)
class _Span:
    """A segment with its end filled, in the shape `label_transcript` reads."""

    start: float
    end: float
    text: str
    track: str | None


def _timeline(result: Result, duration: float) -> list[_Span]:
    """The segments with every end filled: a missing end is the next segment's start on the same track.

    The last segment of a track has no next one, so it ends with the recording.
    """
    spans: list[_Span] = []
    following: dict[str | None, float] = {}
    for item in reversed(result.segments):
        track = item.get("track")
        end = item.get("end")
        if end is None:
            end = following.get(track, duration)
        following[track] = item["start"]
        spans.append(_Span(item["start"], max(end, item["start"]), item["text"], track))
    spans.reverse()
    return spans


def render(result: Result, duration: float) -> tuple[str, list[dict]]:
    """The labelled transcript of the call and its structured turns, from the code that renders in-cell speech."""
    turns = [Turn(item["start"], item["end"], item["label"], item.get("track")) for item in result.turns]
    rendered = label_transcript(
        _timeline(result, duration),
        turns,
        resolved_labels={item["label"]: item["name"] for item in result.names},
        timed=True,
        require_timed=True,
        description="an attached result",
    )
    if rendered is None:
        raise refuse("ATTACH_PAYLOAD_INVALID", "the segments carry no text")
    text, merged = rendered
    # The page's lines are its turns: a segment whose own words start like a label line would add a speaker the call never named.
    if set(speaker_names.labels_in(text)) != {turn["speaker"] for turn in merged}:
        raise refuse("ATTACH_PAYLOAD_INVALID", "a segment's text reads as a speaker label on its line; reword it")
    return text, merged


def verification(result: Result) -> str:
    """`speaker_verification` before a person names every label: what the call's own names say."""
    if not result.turns:
        return "unavailable"
    if any(item["basis"] == speaker_names.BASIS_PROFILE for item in result.names):
        return "profile-matched"
    return "anonymous"


@dataclass(frozen=True)
class Source:
    """A recording's original and its Source page, as one read of them found them."""

    binary: Path
    relative: str
    sidecar: Path
    sidecar_relative: str
    media_type: str
    sha256: str
    size: int
    local: bool
    content: str


def resolve(vault: Path, path: str) -> Source:
    """The Source of the recording at `path`, the original's path or its Source page's.

    Refuses a path outside the knowledge base, one that is no audio or video recording, one that
    `_access.yaml` does not let a write reach, and a recording with no original and Source page.
    """
    from . import access
    from .kbdir import managed_root, managed_root_rel

    binary = Path(os.path.abspath(vault / path.removesuffix(".md")))
    try:
        binary.relative_to(managed_root(vault))
    except ValueError as error:
        raise refuse("MEDIA_PATH_OUTSIDE_KB", f"media path must be inside {managed_root_rel()}: {path}") from error
    media_type = media_processing.classify_media(binary)
    if media_engines.engine_for(media_type) != media_engines.SPEECH:
        raise refuse("ATTACH_NOT_MEDIA", f"{binary.name!r} is not an audio or video recording")
    relative = binary.relative_to(vault).as_posix()
    if access.access_tier(vault, relative) in {access.TIER_EXCLUDED, access.TIER_READONLY}:
        raise refuse("MEDIA_PATH_ACCESS_DENIED", f"media path is not writable under _access.yaml: {relative}")
    stored = originals.original_state(vault, relative)
    if not stored.present or stored.binary_sha256 is None or stored.binary_size is None:
        raise refuse("MEDIA_NOT_FOUND", "this recording has no original and Source page; process it first")
    content = media_processing._read_sidecar_text(vault, stored.sidecar_path)  # noqa: SLF001 - the one reader that confines a sidecar to the vault
    if content is None:
        raise refuse("MEDIA_NOT_FOUND", "this recording has no Source page; process it first")
    return Source(
        binary, relative, stored.sidecar_path, stored.sidecar_path.relative_to(vault).as_posix(), media_type,
        stored.binary_sha256, stored.binary_size, stored.state in {"local", "cached"}, content,
    )


def bind(vault: Path, source: Source, sha256: str, size: int) -> None:
    """Refuse a call whose SHA-256 or size differs from the original that the Source recorded.

    The cell does not hash the original again: the Source's record is its identity, and a file
    whose size changed since the record is refused here too.
    """
    if (sha256.lower(), size) != (source.sha256, source.size):
        raise refuse("ATTACH_BINARY_MISMATCH", "the result was computed for other bytes than the Source's original")
    if source.local:
        try:
            on_disk = media_processing.held_path(vault, source.binary).stat().st_size
        except OSError:
            return
        if on_disk != source.size:
            raise refuse("ATTACH_BINARY_MISMATCH", "the original on disk differs from the Source's record; process the recording again")


def probe(vault: Path, binary: Path) -> float:
    """The recording's duration, read from its container (hydrated first when the original is evicted)."""
    from . import media_store

    try:
        with media_store.logical_view(vault, binary) as view:
            duration = container_duration(view)
    except media_store.MediaStoreError as error:
        raise refuse(error.code, error.reason) from error
    if duration is None or not duration > 0:
        raise refuse("ATTACH_DURATION_UNKNOWN", "the recording's container states no duration, so the result cannot be checked against it")
    return duration


def reply(source: Source, content: str, *, outcome: str, **extra: Any) -> dict[str, Any]:
    """The reply of an attach, read from the page as it stands."""
    fields = parse_frontmatter(content)[0]
    labels, not_carried = fields.get("speakers"), fields.get("speaker_names_not_carried")
    return {
        "operation": ATTACH_OPERATION,
        "path": source.relative,
        "outcome": outcome,
        "media_type": source.media_type,
        "state": fields.get("processing_state"),
        "sidecar_path": source.sidecar_relative,
        "extracted_by": fields.get("extracted_by"),
        "attached_digest": fields.get("attached_digest"),
        "speakers": labels if isinstance(labels, list) else [],
        "speaker_verification": fields.get("speaker_verification"),
        "names_not_carried": not_carried if isinstance(not_carried, list) else [],
        **extra,
    }


def commit_under_guard(
    vault: Path,
    commit_guard: Callable[[], AbstractContextManager[object]],
    operation: str,
    unit: Callable[[Callable[[Sequence[PlannedWrite]], None]], _Committed],
) -> _Committed:
    """Run `unit` under the mutation guard, publish what it commits, and fan out once the guard is released.

    `unit` reads its pages and calls the `commit` it is given with the writes it plans. The index
    fan-out runs after the guard releases, because it embeds text and the guard is held only while
    a commit needs it.
    """
    from . import file_watcher, preserve
    from .governance import catalog_publication

    written: list[Path] = []
    intents: list[object] = []

    def commit(writes: Sequence[PlannedWrite]) -> None:
        try:
            done = preserve.commit_media_sidecar_writes(
                vault, tuple(writes), post_commit_fanout=False, publication_intents_out=intents,
                batch_writer=batch_atomic_write,
            )
        except catalog_publication.CatalogCommitError as error:
            raise media_processing.MediaProcessingError(error.code, error.reason) from error
        assert isinstance(done, list)
        written.extend(done)

    def abort() -> None:
        if intents:
            file_watcher.abort_publication_intents(intents, force_paths=written)

    try:
        outcome = media_processing._committing(vault, commit_guard, operation, lambda: unit(commit))  # noqa: SLF001 - the shared hop onto the store thread
        if written and post_commit_batch_fanout(vault, written, None, None, publication_intents=intents) is not True:
            abort()
    except BaseException:
        abort()
        raise
    return outcome


def attach(
    vault_root: Path,
    path: str,
    *,
    binary_sha256: str,
    binary_size: int,
    segments: Sequence[Mapping[str, Any]],
    engine: str,
    turns: Sequence[Mapping[str, Any]] | None = None,
    names: Sequence[Mapping[str, str]] | None = None,
    asr: Mapping[str, Any] | None = None,
    replace_labelled: bool = False,
    commit_guard: Callable[[], AbstractContextManager[object]],
) -> dict[str, Any]:
    """Attach the call's speech result to the Source of the recording at `path`; the reply says what changed.

    Every refusal comes before the first write. A repeat of an attached call writes nothing.
    """
    vault = Path(vault_root).resolve()
    result = parse(engine=engine, segments=segments, turns=turns, names=names, asr=asr, replace_labelled=replace_labelled)
    source = resolve(vault, path)
    bind(vault, source, binary_sha256, binary_size)
    digest = result_digest(result)
    if attached_earlier(source.content, digest):
        return reply(source, source.content, outcome="unchanged")
    _require_replaceable(source.content, source.media_type, result)
    duration = probe(vault, source.binary)
    check_bounds(result, duration)
    text, merged = render(result, duration)
    facts = AsrFacts(**result.asr, duration_seconds=duration)

    def unit(commit: Callable[[Sequence[PlannedWrite]], None]) -> dict[str, Any]:
        # The Source may have changed since the plan, so every check reads the page as it stands under the guard.
        current = resolve(vault, path)
        bind(vault, current, binary_sha256, binary_size)
        before, guard = read_guarded_text(vault, current.sidecar)
        if attached_earlier(before, digest):
            return reply(current, before, outcome="unchanged")
        replacing = _require_replaceable(before, current.media_type, result)
        writes, surplus = prior_extraction.extraction_writes(
            vault, current.sidecar, before, guard,
            binary_path=current.binary, text=text, engine=result.engine, speakers=merged,
            speaker_verification=verification(result), diarize=None, attempts=None, asr=facts,
            ocr=None, capture=None, reprocessing=replacing, attached_digest=digest,
            given_names=[dict(item) for item in result.names],
        )
        commit(writes)
        retire_parts(vault, surplus)
        stage = retire_speech_stage(vault, current)
        after, _ = read_guarded_text(vault, current.sidecar)
        links = prior_extraction.links_of(after)
        return reply(
            current, after, outcome="replaced" if replacing else "attached", duration_seconds=duration,
            prior_extraction=links[-1] if replacing and links else None, speech_job=stage,
        )

    return commit_under_guard(vault, commit_guard, "media_attach_speech_commit", unit)


def attached_earlier(content: str, digest: str) -> bool:
    """Whether `content` holds the result that `digest` identifies, attached by an earlier call."""
    fields = parse_frontmatter(content)[0]
    return fields.get("extraction_origin") == "attached" and fields.get("attached_digest") == digest


def _require_replaceable(content: str, media_type: str, result: Result) -> bool:
    """Whether the attach replaces a transcript, or a refusal when that would drop labels or names unasked."""
    verdict = prior_extraction.replacement(content, media_type=media_type, replace_labelled=result.replace_labelled)
    if verdict == prior_extraction.REFUSE:
        raise refuse(
            "ATTACH_REPLACES_LABELS",
            "the Source holds speaker labels or names; set replace_labelled to replace them, and the old result stays on a prior page",
        )
    return verdict == prior_extraction.REPLACE


def retire_parts(vault: Path, surplus: tuple[Path, ...]) -> None:
    """Retire the part pages the new result no longer uses; a stored part that stays is logged, not raised."""
    from . import extraction_parts

    kept = extraction_parts.retire(vault, surplus)
    if kept:
        log.info("%s", extraction_parts.kept_note(kept))


def retire_speech_stage(vault: Path, source: Source) -> str:
    """End the queued speech work of this recording, and only that stage: a video keeps its frame-vector stage.

    A running job keeps going; its result is stale against the new page, and the worker drops it.
    The result is a state word for the reply, because the page has committed and a failure here
    must not report the write as failed.
    """
    try:
        if not media_jobs.job_store_path(vault).exists():
            return "none"
        retired = media_jobs.MediaJobStore(vault, create=False).retire_ocr(
            source.binary, source.media_type, superseded=True
        )
    except Exception:  # noqa: BLE001 - the page has committed; a ledger error must not report it as failed
        log.warning("could not retire the speech job of %s", source.relative, exc_info=True)
        return "retire_failed"
    return "retired" if retired else "none"
