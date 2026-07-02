"""CLI speaker enrollment — desk-side admin, not an MCP connector tool.

Enrolling a voice is a one-time local act on an audio sample (a *brain* decision about who to
name), so it lives in `python -m exomem` beside `install-hook`/`install-skill`, never on the
tool surface a remote client sees. Each command resolves the per-machine voice-profile store
(`voice_profiles.voice_profiles_path`) and delegates to the pure store + the ECAPA embedder:

- `enroll_speaker(sample, name)` ECAPA-embeds the whole sample into a 192-dim voiceprint and
  folds it into the profile (a repeat name running-averages the centroid; `--self` marks the
  vault owner).
- `list_speakers()` / `remove_speaker(name)` round-trip the store.

The embedding is a frozen audio→vector measurement (no generation, no reasoning) — pure
substrate. A model/dep failure raises `EnrollmentError` so the CLI reports it (unlike the
server diarization path, which soft-fails to anonymous).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import voice_embed, voice_profiles
from .vault import resolve_vault
from .voice_profiles import DEFAULT_THRESHOLD

# Single span covering the entire sample: voice_embed.embed_spans clamps the end to the audio's
# true length, so this embeds the whole file without a separate duration probe.
_WHOLE_SAMPLE_SPANS = [(0.0, 10 * 3600.0)]


class EnrollmentError(RuntimeError):
    """Enrollment could not produce a voiceprint (missing dep/model, or unreadable audio)."""


def _store_path(vault_root: Path | None) -> Path:
    return voice_profiles.voice_profiles_path(vault_root or resolve_vault())


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
    return voice_profiles.save_profile(
        _store_path(vault_root), name, centroid, threshold=threshold, is_self=is_self
    )


def list_speakers(vault_root: Path | None = None) -> list[dict[str, Any]]:
    """Enrolled-profile summaries (no centroid), sorted by name."""
    return voice_profiles.list_profiles(_store_path(vault_root))


def remove_speaker(name: str, vault_root: Path | None = None) -> bool:
    """Delete the `name` profile. Returns True if it existed."""
    return voice_profiles.remove_profile(_store_path(vault_root), name)
