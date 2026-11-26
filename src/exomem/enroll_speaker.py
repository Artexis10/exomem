"""CLI speaker enrollment — desk-side admin, not an MCP connector tool.

Enrolling a voice is a one-time local act on an audio sample (a *brain* decision about who to
name), so it lives in `python -m exomem` beside `install-hook`/`install-skill`, never on the
tool surface a remote client sees. Each command resolves the per-machine voice-profile store
(`voice_profiles.voice_profiles_path`) and delegates to the pure store + the ECAPA embedder:

- `enroll_speaker(sample, name)` ECAPA-embeds the whole sample into a 192-dim voiceprint and
  folds it into the profile (a repeat name running-averages the centroid; `--self` marks the
  vault owner).
- `enroll_from_recording(...)` keeps one speaker label of a recording's Source: it shows the label's
  sample lines, and only after the owner confirms it embeds that label's timed line spans (D14).
  The original is a local file, or a Cloud download that is deleted once the voiceprint exists.
- `list_speakers()` / `remove_speaker(name)` round-trip the store.

The embedding is a frozen audio→vector measurement (no generation, no reasoning) — pure
substrate. A model/dep failure raises `EnrollmentError` so the CLI reports it (unlike the
server diarization path, which soft-fails to anonymous).
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import extraction_parts, media_store, reserved_paths, speaker_names, voice_embed, voice_profiles
from .vault import parse_frontmatter, resolve_vault
from .voice_profiles import DEFAULT_THRESHOLD

# Single span covering the entire sample: voice_embed.embed_spans clamps the end to the audio's
# true length, so this embeds the whole file without a separate duration probe.
_WHOLE_SAMPLE_SPANS = [(0.0, 10 * 3600.0)]


#: The lines shown before the owner confirms: enough to know the voice by its words, few enough to read at a glance.
SAMPLE_LINES = 5


class EnrollmentError(RuntimeError):
    """Enrollment could not produce a voiceprint (missing dep/model, or unreadable audio).

    `code` names the cause for a caller that reports it, such as the terminal form.
    """

    def __init__(self, message: str, code: str = "ENROLLMENT_FAILED") -> None:
        super().__init__(message)
        self.code = code


def enroll_speaker(
    audio_path: str | Path,
    name: str,
    *,
    is_self: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    vault_root: Path | None = None,
) -> dict[str, Any]:
    """Embed `audio_path` into a voiceprint and persist/extend the `name` profile.

    Raises `EnrollmentError` when the sample can't be embedded (no `speechbrain`/model, or
    undecodable audio). Returns the stored record.
    """
    path = Path(audio_path).expanduser()
    if not path.is_file():
        raise EnrollmentError(f"audio sample not found: {path}")
    centroid = voice_embed.embed_spans(path, _WHOLE_SAMPLE_SPANS)
    if centroid is None:
        raise EnrollmentError(
            f"could not compute a voiceprint for {path.name} — is the [diarization] extra "
            f"(speechbrain) installed and the audio readable?"
        )
    root = vault_root or resolve_vault()
    return voice_profiles.save_profile(
        voice_profiles.voice_profiles_path(root),
        name,
        centroid,
        threshold=threshold,
        is_self=is_self,
        vault_root=root,
    )


@dataclass(frozen=True)
class Sample:
    """What the owner reads before confirming, and what the voiceprint is computed from."""

    label: str
    #: The label's timed line spans in seconds: each line runs to the next timed line's start.
    spans: tuple[tuple[float, float], ...]
    lines: tuple[str, ...]


def sample_of(page_text: str, label: str) -> Sample:
    """The lines and timed spans of `label` in an extraction, or an `EnrollmentError` that names the cause.

    A merged-turn extraction has no times to cut the audio by, so it is refused; a reprocess that keeps
    speaker labels renders timed lines, which this accepts.
    """
    if label not in speaker_names.labels_in(page_text):
        raise EnrollmentError(
            f"the recording carries no label {label!r}; it carries {speaker_names.labels_in(page_text)}",
            "SPEAKER_LABEL_UNKNOWN",
        )
    if not speaker_names.has_timed_lines(page_text):
        raise EnrollmentError(
            "this extraction has merged turns with no times, so no part of the audio can be tied to the label; "
            "process the recording again with speaker labels, then enrol from the new extraction",
            "ENROLL_NEEDS_TIMED_LINES",
        )
    spans = tuple((start, end) for start, end, found in speaker_names.timed_spans(page_text) if found == label)
    if not spans:
        raise EnrollmentError(f"the label {label!r} has no line that runs to a next line, so it has no audio span", "ENROLL_NO_SPAN")
    return Sample(label, spans, tuple(speaker_names.lines_of(page_text, label)[:SAMPLE_LINES]))


def _page_text(root: Path, page: Path) -> tuple[str, str | None]:
    """The whole extraction text of the Source page `page`, parts included, and the original's SHA-256 it records.

    The part pages sit where `extraction_parts.part_path` puts them, inside `root`. A missing part
    is refused: the label's lines would be partial, and an enrolment reads the audio they cut.
    """
    try:
        content = page.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise EnrollmentError(f"no Source page at {page.name}", "MEDIA_NOT_FOUND") from error
    frontmatter = parse_frontmatter(content)[0]
    try:
        text = extraction_parts.whole_text(root, page, content)
    except (OSError, UnicodeError) as error:
        first = extraction_parts.part_path(page, 2)
        raise EnrollmentError(
            f"the page lists {extraction_parts.count_of(frontmatter)} parts and one is missing or unreadable; "
            f"keep each part page where the first one is expected: {first}",
            "MEDIA_PARTS_UNREADABLE",
        ) from error
    recorded = frontmatter.get("binary_sha256")
    return text, recorded if isinstance(recorded, str) else None


def local_page(vault_root: Path, recording: str) -> tuple[str, str | None]:
    """The whole extraction text of a recording's Source in the vault, and the original's SHA-256 it records."""
    return _page_text(Path(vault_root), Path(vault_root) / f"{recording}.md")


def saved_page(page_file: Path) -> tuple[str, str | None]:
    """The same for a Source page saved from the Cloud, with its part pages beside it."""
    page = Path(page_file).resolve()
    return _page_text(page.parent, page)


#: An original is a context manager that yields its local path and the SHA-256 of the bytes it holds.
Original = Callable[[], AbstractContextManager[tuple[Path, str]]]


def local_original(vault_root: Path, recording: str) -> Original:
    """The original in the vault; shared or evicted bytes are hydrated for the use alone."""

    @contextmanager
    def open_original() -> Iterator[tuple[Path, str]]:
        with media_store.logical_view(Path(vault_root), recording) as audio:
            with audio.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            yield audio, digest

    return open_original


def cloud_original(config: Any, recording: str, *, api: Any = None) -> Original:
    """The original through the owner's signed-in Cloud download, in a private copy that is deleted on exit.

    The download is kept only when its size and SHA-256 verify against the Cloud's own record.
    """
    from .cloud_download_client import CloudDownloadError, download_original

    @contextmanager
    def open_original() -> Iterator[tuple[Path, str]]:
        with reserved_paths.private_copy_directory() as private:
            try:
                got = asyncio.run(download_original(config, recording, private, api=api))
            except CloudDownloadError as error:
                raise EnrollmentError(error.message, error.code) from error
            yield Path(got["path"]), got["sha256"]

    return open_original


def enroll_from_recording(
    page_text: str,
    recorded_sha256: str | None,
    label: str,
    name: str,
    *,
    original: Original,
    confirm: Callable[[Sample], bool],
    is_self: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    vault_root: Path | None = None,
) -> dict[str, Any] | None:
    """Keep the voice behind `label` as the profile `name`; None when the owner does not confirm.

    The audio is read, and a voiceprint computed, only after `confirm` returns true for the sample
    lines. The original must match the SHA-256 that the Source records.
    """
    problem = speaker_names.name_problem(name)
    if problem is not None:
        raise EnrollmentError(problem, "SPEAKER_NAME_INVALID")
    sample = sample_of(page_text, label)
    if not confirm(sample):
        return None
    with original() as (audio, sha256):
        if recorded_sha256 is None or sha256 != recorded_sha256:
            raise EnrollmentError("the original does not match the SHA-256 that the Source records", "ENROLL_ORIGINAL_MISMATCH")
        centroid = voice_embed.embed_spans(audio, list(sample.spans))
    if centroid is None:
        raise EnrollmentError(f"could not compute a voiceprint for {label!r}: is the [diarization] extra installed?", "ENROLL_EMBED_FAILED")
    root = vault_root or resolve_vault()
    return voice_profiles.save_profile(
        voice_profiles.voice_profiles_path(root), name, centroid, threshold=threshold, is_self=is_self, vault_root=root
    )


def list_speakers(vault_root: Path | None = None) -> list[dict[str, Any]]:
    """Enrolled-profile summaries (no centroid), sorted by name."""
    root = vault_root or resolve_vault()
    return voice_profiles.list_profiles(
        voice_profiles.voice_profiles_path(root), vault_root=root
    )


def remove_speaker(name: str, vault_root: Path | None = None) -> bool:
    """Delete the `name` profile. Returns True if it existed."""
    root = vault_root or resolve_vault()
    return voice_profiles.remove_profile(
        voice_profiles.voice_profiles_path(root), name, vault_root=root
    )
