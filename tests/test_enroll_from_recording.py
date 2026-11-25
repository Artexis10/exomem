"""Keeping a voice from a recording's Source: the owner confirms, then the audio is read (design D14).

The ECAPA model is replaced by a stub that maps audio bytes to a vector; the Source pages, the
profile store, the original's SHA-256 check and the Cloud download client (over a mock transport)
are real.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx2
import numpy as np
import pytest

from exomem import commands, enroll_speaker, operation_entries, speech_attach, voice_embed, voice_profiles
from exomem.cloud_import_client import CloudImportClientConfig
from exomem.speaker_attribution import attribute_clusters

AUDIO = b"owner talking, then a guest talking"
VOICE_OWNER = np.array([1.0, 0.0, 0.0])
VOICE_GUEST = np.array([0.0, 1.0, 0.0])
SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Welcome back to the show."},
    {"start": 4.0, "end": 9.0, "text": "Thanks for having me."},
    {"start": 9.0, "end": 14.0, "text": "Let us start with the move."},
    {"start": 14.0, "end": 18.0, "text": "Gladly."},
]
TURNS = [
    {"start": 0.0, "end": 4.0, "label": "A"}, {"start": 4.0, "end": 9.0, "label": "B"},
    {"start": 9.0, "end": 14.0, "label": "A"}, {"start": 14.0, "end": 18.0, "label": "B"},
]


@pytest.fixture(autouse=True)
def _stub_model(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[float, float]]]:
    """The embedder, recording the spans it is asked for; the owner speaks in the first span of a recording."""
    asked: list[list[tuple[float, float]]] = []

    def embed(_audio, spans):
        asked.append(list(spans))
        return VOICE_OWNER if spans[0][0] == 0.0 else VOICE_GUEST

    monkeypatch.setattr(voice_embed, "embed_spans", embed)
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: 120.0)
    return asked


def _recording(vault: Path, *, turns=TURNS) -> tuple[str, str, str]:
    """An attached, labelled recording; returns its vault path, its page text and its original's SHA-256."""
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "talk.m4a"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(AUDIO)
    relative = binary.relative_to(vault).as_posix()
    digest = hashlib.sha256(AUDIO).hexdigest()
    commands.op_process_media(vault, path=relative, operation="process")
    commands.op_process_media(
        vault, path=relative, operation="attach-speech", binary_sha256=digest, binary_size=len(AUDIO),
        segments=SEGMENTS, turns=turns, engine="whisper + pyannote",
    )
    text, recorded = enroll_speaker.local_page(vault, relative)
    assert recorded == digest
    return relative, text, digest


def _enrol(vault: Path, relative: str, label: str, *, confirm=lambda _sample: True):
    text, recorded = enroll_speaker.local_page(vault, relative)
    return enroll_speaker.enroll_from_recording(
        text, recorded, label, "Hugo", original=enroll_speaker.local_original(vault, relative),
        confirm=confirm, vault_root=vault,
    )


def test_no_audio_is_read_and_no_profile_is_kept_until_the_owner_confirms(vault: Path, _stub_model) -> None:
    relative, _text, _digest = _recording(vault)
    shown: list[enroll_speaker.Sample] = []

    kept = _enrol(vault, relative, "Speaker A", confirm=lambda sample: shown.append(sample) or False)

    assert kept is None and _stub_model == []
    assert not voice_profiles.voice_profiles_path(vault).exists()
    assert shown[0].lines[0] == "[0:00] [Speaker A]: Welcome back to the show."
    assert shown[0].spans == ((0.0, 4.0), (9.0, 14.0))


def test_a_confirmed_label_is_embedded_from_its_timed_spans_and_a_later_recording_matches_it(vault: Path, _stub_model) -> None:
    relative, _text, _digest = _recording(vault)

    record = _enrol(vault, relative, "Speaker A")

    assert _stub_model == [[(0.0, 4.0), (9.0, 14.0)]] and record["samples"] == 1
    profiles = voice_profiles.load_profiles(voice_profiles.voice_profiles_path(vault), vault_root=vault)
    later = attribute_clusters({"cluster": VOICE_OWNER}, {"cluster": 0.0}, profiles)
    assert later["cluster"].matched_profile == "Hugo"
    stranger = attribute_clusters({"cluster": VOICE_GUEST}, {"cluster": 0.0}, profiles)
    assert stranger["cluster"].matched_profile is None


def test_an_extraction_of_merged_turns_is_refused_with_a_code_that_names_the_remedy(vault: Path, _stub_model) -> None:
    relative, text, _digest = _recording(vault)
    merged = "\n".join(line.split("] ", 1)[1] for line in text.splitlines() if line.startswith("[0:") or line.startswith("[1"))

    with pytest.raises(enroll_speaker.EnrollmentError) as refused:
        enroll_speaker.enroll_from_recording(
            merged, hashlib.sha256(AUDIO).hexdigest(), "Speaker A", "Hugo",
            original=enroll_speaker.local_original(vault, relative), confirm=lambda _sample: True, vault_root=vault,
        )

    assert refused.value.code == "ENROLL_NEEDS_TIMED_LINES" and "speaker labels" in str(refused.value)
    assert _stub_model == []


def test_an_unknown_label_and_an_original_that_is_not_the_source_are_refused_and_keep_nothing(vault: Path, _stub_model) -> None:
    relative, text, _digest = _recording(vault)

    with pytest.raises(enroll_speaker.EnrollmentError) as unknown:
        _enrol(vault, relative, "Speaker Z")
    with pytest.raises(enroll_speaker.EnrollmentError) as swapped:
        enroll_speaker.enroll_from_recording(
            text, "0" * 64, "Speaker A", "Hugo", original=enroll_speaker.local_original(vault, relative),
            confirm=lambda _sample: True, vault_root=vault,
        )

    assert unknown.value.code == "SPEAKER_LABEL_UNKNOWN" and swapped.value.code == "ENROLL_ORIGINAL_MISMATCH"
    assert not voice_profiles.voice_profiles_path(vault).exists()


def test_a_cloud_original_is_downloaded_verified_embedded_and_deleted(vault: Path, _stub_model, tmp_path: Path) -> None:
    relative, text, digest = _recording(vault)
    copies: list[Path] = []
    headers = {"x-exomem-sha256": digest, "x-exomem-size": str(len(AUDIO)), "content-length": str(len(AUDIO)),
               "content-disposition": "attachment; filename*=utf-8''talk.m4a"}

    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield AUDIO

    api = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, headers=headers, stream=Chunks())))
    config = CloudImportClientConfig("https://cloud.example/mcp", "https://cloud.example")
    real_embed = voice_embed.embed_spans

    def embed(audio, spans):
        copies.append(Path(audio))
        assert Path(audio).read_bytes() == AUDIO
        return real_embed(audio, spans)

    voice_embed.embed_spans = embed
    try:
        record = enroll_speaker.enroll_from_recording(
            text, digest, "Speaker A", "Hugo", original=enroll_speaker.cloud_original(config, relative, api=api),
            confirm=lambda _sample: True, vault_root=vault,
        )
    finally:
        voice_embed.embed_spans = real_embed

    assert record["samples"] == 1 and len(copies) == 1
    assert not copies[0].exists() and not copies[0].parent.exists(), "the downloaded copy is deleted after use"


def test_no_connector_operation_enrols_a_voice_and_the_cell_stores_no_embedding(vault: Path) -> None:
    relative, _text, _digest = _recording(vault)
    commands.op_process_media(
        vault, path=relative, operation="rename-speakers", renames={"Speaker A": "Hugo"},
    )

    tools = {command.name for command in commands.PRODUCT_COMMANDS}
    actions = {action for grouped in operation_entries.OPERATIONS.values() for action in grouped.actions()}
    assert not [name for name in tools | actions if "enroll" in name or "voice" in name]
    stored = [path for path in vault.rglob("*") if path.is_file() and b"centroid" in path.read_bytes()]
    assert stored == [] and not voice_profiles.voice_profiles_path(vault).exists()
