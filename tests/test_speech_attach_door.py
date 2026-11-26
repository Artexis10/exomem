"""An owner's agent attaches a speech result and names its speakers through a real MCP client.

The server is the local one, with the real door, argument validation, writer lease and job
ledger. The recording's bytes are fake, so only the container's duration is stated by the test;
`test_the_duration_probe_reads_a_real_container` reads a real file in the frame-codecs CI job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from pathlib import Path

import pytest
from fastmcp import Client

from exomem import call_ledger, commands, extraction_parts, media_jobs, preserve, speech_attach, writer_lease
from exomem import server as server_module
from exomem.vault import parse_frontmatter

PAYLOAD = b"fake audio bytes, no container"
#: Forty short lines, 3 s apart and alternating between two voices: enough to need part pages once the page limit is lowered.
SEGMENTS = [
    {"start": float(n * 3), "end": float(n * 3 + 2), "text": "Welcome back to the show." if n == 0 else f"Line {n} of the interview."}
    for n in range(40)
]
TURNS = [{"start": float(n * 3), "end": float(n * 3 + 2), "label": f"SPEAKER_0{n % 2}"} for n in range(40)]


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: 120.0)
    call_ledger.reset_chain_cache()
    yield lambda: server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def _session(mcp, scenario):
    async def run():
        async with Client(mcp) as client:
            return await scenario(client)

    return asyncio.run(run())


async def _call(client, name, arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"} and isinstance(body["result"], dict):
        body = body["result"]
    return body if body is not None else json.loads(" ".join(getattr(block, "text", "") for block in result.content))


def _pending_recording(vault: Path) -> tuple[str, Path, Path]:
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "interview.m4a"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(PAYLOAD)
    relative = binary.relative_to(vault).as_posix()
    commands.op_process_media(vault, path=relative, operation="process")
    return relative, binary, binary.with_name(binary.name + ".md")


def test_an_agent_attaches_a_result_names_its_speakers_and_finds_the_recording_by_name(
    build, vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 600)
    relative, binary, page = _pending_recording(vault)
    attach = {
        "path": relative, "binary_sha256": hashlib.sha256(PAYLOAD).hexdigest(), "binary_size": len(PAYLOAD),
        "segments": SEGMENTS, "turns": TURNS, "engine": "whisper-large-v3 + pyannote-3.1",
    }

    async def scenario(client):
        door = next(tool for tool in await client.list_tools() if tool.name == "manage_memory")
        operations = door.input_schema["properties"]["operation"]["enum"]
        malformed = await _call(client, "manage_memory", {
            "operation": "media.attach_speech", "arguments": {**attach, "segments": [{"start": "soon", "text": "x"}]},
        })
        attached = await _call(client, "manage_memory", {"operation": "media.attach_speech", "arguments": attach})
        renamed = await _call(client, "manage_memory", {
            "operation": "media.rename_speakers",
            "arguments": {"path": relative, "renames": {"Speaker A": "Host", "Speaker B": "Guest"}},
        })
        return operations, malformed, attached, renamed

    operations, malformed, attached, renamed = _session(build(), scenario)

    assert {"media.attach_speech", "media.rename_speakers"} <= set(operations)
    assert malformed["error"]["code"] == "INVALID_OPERATION_ARGUMENTS"
    assert attached["outcome"] == "attached" and attached["speech_job"] == "retired"
    assert renamed["outcome"] == "renamed" and renamed["speaker_verification"] == "human-verified"
    assert "enroll-speaker" in renamed["next_action"]
    fields = parse_frontmatter(page.read_text(encoding="utf-8"))[0]
    assert fields["speakers"] == ["Host", "Guest"] and fields["extraction_origin"] == "attached"
    parts = [extraction_parts.part_path(page, number) for number in range(2, extraction_parts.count_of(fields) + 1)]
    texts = [page.read_text(encoding="utf-8"), *(part.read_text(encoding="utf-8") for part in parts)]
    assert parts and not any("[Speaker " in text for text in texts), "every page, parts included, carries the names"
    assert "[0:03] [Guest]: Line 1 of the interview." in texts[0]
    assert binary.read_bytes() == PAYLOAD, "the original stays as the owner stored it"
    job = media_jobs.MediaJobStore(vault).get_by_binary(binary, media_type="audio")
    assert job is None or not job.do_ocr, "the cell queues no speech work for an attached result"
    found = commands.op_find(vault, "welcome", speakers=["Host"])
    assert [hit["path"] for hit in found] == [f"{relative}.md"], found


@pytest.mark.frame_codecs
def test_the_duration_probe_reads_a_real_container(vault, tmp_path: Path) -> None:
    clip = tmp_path / "tone.wav"
    with wave.open(str(clip), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * 8000 * 3)

    assert speech_attach.container_duration(clip) == pytest.approx(3.0, abs=0.1)
