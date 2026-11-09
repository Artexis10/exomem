"""Cloud frame reads keep decoder work outside the serving process."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from test_media_store import selected_store as _selected_store  # noqa: F401 - shared encrypted-store fixture

from exomem import commands, media_engines, reserved_paths, server


@pytest.fixture
def selected_store(request):
    import shutil

    assert shutil.which("age") and shutil.which("age-keygen"), (
        "the mandatory retained-original proof requires age and age-keygen"
    )
    return request.getfixturevalue("_selected_store")


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Cloud owns POSIX process groups")


def test_selected_cloud_cell_lists_video_and_status(vault, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "frame-test")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "x" * 40)
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "frames")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    mcp = server.build_server(require_auth=True)
    try:
        tools = asyncio.run(mcp.list_tools())
        media = next(tool for tool in tools if tool.name == "read_media")
        kinds = {branch["properties"]["kind"]["const"]
                 for branch in media.parameters["properties"]["operation"]["oneOf"]}
        assert kinds == {"video", "status"}
    finally:
        mcp._exomem_local_runtime_activation._shutdown.set()


def test_cloud_video_does_not_copy_original_in_serving_process(vault, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "frames")
    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"invalid video")
    monkeypatch.setattr(commands.egress_module, "release_allows_frames", lambda *a, **kw: True)

    def no_snapshot(*args, **kwargs):
        pytest.fail("the serving process copied the complete video")

    monkeypatch.setattr(reserved_paths, "read_generic_bytes", no_snapshot)
    with pytest.raises(ValueError, match="(VIDEO_BUSY|NO_DECODABLE_FRAMES)"):
        commands.op_get_video_frames(vault, path)


def test_frame_engine_never_enables_video_transcription(monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "frames")
    assert media_engines.enabled_engines() == {"frames"}
    assert media_engines.engine_for("video") == media_engines.SPEECH
    assert not media_engines.stage_enabled("video")


@pytest.fixture
def cloud_frames(monkeypatch):
    from exomem import media_brakes

    monkeypatch.delenv("EXOMEM_CLOUD_MEDIA_SLOT_URL", raising=False)
    monkeypatch.delenv("EXOMEM_MEDIA_STORE_CONFIG", raising=False)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "frame-test")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "x" * 40)
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "frames")
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **kw: None)
    roomy = media_brakes.CellMemory(0, 3 << 30, None, 0.0)
    monkeypatch.setattr(media_brakes, "read_cell", lambda: roomy)
    return roomy


def _injected_child(monkeypatch, code):
    """Run the real child protocol with a deterministic decode or transport fault."""
    import os
    import subprocess
    import sys

    from exomem import video_frames_worker

    children = []
    source = "from pathlib import Path\nimport sys, time, os\nfrom exomem import video_frames_worker as w\n" + code
    source += "\nraise SystemExit(w._child(Path(sys.argv[1])))\n"

    def launch(vault):
        env = {**os.environ, "OPENBLAS_NUM_THREADS": "1"}
        child = subprocess.Popen([sys.executable, "-c", source, str(vault)], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        children.append(child)
        return child

    monkeypatch.setattr(video_frames_worker, "_launch_child", launch)
    return children


def _fake_decoder():
    return '''
from exomem import video_frames as v

def decode(source, relative, **kwargs):
    Path(sys.argv[1], "decoded").write_text("yes")
    return v.FramesResult(relative, 2.0, [v.Frame(0.5, bytes.fromhex("ffd84a504547ffd9"))], 1, 0, kwargs["max_frames"],
                         kwargs["max_side"])
v.sample_stream = decode
'''


def test_withheld_prepared_original_never_reaches_decoder(vault, cloud_frames, monkeypatch):
    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    children = _injected_child(monkeypatch, _fake_decoder())
    monkeypatch.setattr(commands.egress_module, "release_allows_frames", lambda *a, **kw: False)

    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_get_video_frames(vault, path)

    assert not (vault / "decoded").exists()
    assert children[0].poll() is not None


@pytest.mark.frame_codecs
def test_withheld_unavailable_stored_original_matches_missing_path(vault, cloud_frames, selected_store):
    import io

    from test_governance_egress import write_rule, write_scope
    from test_media_store import _offload

    from exomem import command_surface, media_store, preserve
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, request_scope

    captured = preserve.preserve(
        vault, scope="Test", category="video", filename="sample.mp4",
        content_stream=io.BytesIO(b"retained video original"), text="A recorded sample.",
    )
    path = vault / captured.path
    reference = _offload(vault, path)
    assert media_store.evict(vault, path)
    (Path(selected_store[0]["root"]) / reference.object / reference.version).unlink()
    write_scope(vault, paths="Sources/**")
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    command = next(item for item in commands.COMMANDS if item.name == "get_video_frames")
    read = command_surface.bind_vault(command.leaf, vault, command=command)

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        retained = read(path=captured.path)
        missing = read(path="Knowledge Base/Sources/missing.mp4")

    assert retained == missing
    assert retained["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("interruption", ["deadline", "pressure"])
def test_withheld_original_preparation_interruption_matches_missing_path(
    vault, cloud_frames, monkeypatch, interruption,
):
    import time

    from test_governance_egress import write_rule, write_scope

    from exomem import command_surface, media_brakes, request_budget, video_frames_worker
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, request_scope

    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"retained video original")
    write_scope(vault, paths="Sources/**")
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    children = _injected_child(monkeypatch, '''
from contextlib import contextmanager
from exomem import media_store
original = media_store.open_original

@contextmanager
def stalled(vault, target, **kwargs):
    if target.exists():
        (vault / "preparing").write_text("yes")
        time.sleep(30)
    with original(vault, target, **kwargs) as value:
        yield value
media_store.open_original = stalled

read_message = w._read_message
prepare = getattr(w, "_Prepare", None)

def waiting(stream, model, check):
    if model is prepare:
        (Path(sys.argv[1]) / "awaiting-prepare").write_text("yes")
    return read_message(stream, model, check)
w._read_message = waiting
''')
    if interruption == "pressure":
        monkeypatch.setattr(media_brakes, "read_cell", lambda: media_brakes.CellMemory(
            0, 3 << 30, None, 99.0) if (vault / "preparing").exists() else cloud_frames)
        read_message = video_frames_worker._read_message
        delayed = False

        def parent_yields(stream, model, check):
            nonlocal delayed
            if model is video_frames_worker._Admitted and not delayed:
                delayed = True
                until = time.monotonic() + 10
                # Let the child reach preparation or its control wait before the parent reads admission.
                while not any((vault / name).exists() for name in ("preparing", "awaiting-prepare")):
                    assert time.monotonic() < until, "the child did not reach its preparation boundary"
                    time.sleep(0.01)
            return read_message(stream, model, check)

        monkeypatch.setattr(video_frames_worker, "_read_message", parent_yields)
    command = next(item for item in commands.COMMANDS if item.name == "get_video_frames")
    read = command_surface.bind_vault(command.leaf, vault, command=command)

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with reserved_paths.hold_generic_file(vault, path, recorded_sha256=lambda version: None) as held:
            assert not egress.release_allows_frames(vault, path, artifact_copy=held)
        token = request_budget.set_current(request_budget.RequestBudget(
            seconds=request_budget.DELIVERY_RESERVE_SECONDS + (10 if interruption == "pressure" else 3)))
        try:
            retained = read(path=path)
        finally:
            request_budget.reset_current(token)
        (vault / "preparing").unlink()
        missing = read(path="Knowledge Base/Sources/missing.mp4")

    assert retained == missing
    assert retained["error"]["code"] == "NOT_FOUND"
    assert all(child.poll() is not None for child in children)


def test_replaced_prepared_original_is_refused_before_decode(vault, cloud_frames, monkeypatch):
    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    children = _injected_child(monkeypatch, _fake_decoder() + '''
wire = w._wire

def exchange(message):
    if isinstance(message, w._Prepared):
        target = Path(sys.argv[1], message.path)
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
    return wire(message)
w._wire = exchange
''')
    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_get_video_frames(vault, path)
    assert not (vault / "decoded").exists()
    assert children[0].poll() is not None


def test_authority_revoked_after_decode_releases_no_images(vault, cloud_frames, monkeypatch):
    from test_governance_egress import write_rule, write_scope

    from exomem import state_paths
    from exomem.governance import egress, policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    write_scope(vault, paths="Sources/**")
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    _injected_child(monkeypatch, _fake_decoder() + '''
original_decode = v.sample_stream

def revoke(*args, **kwargs):
    result = original_decode(*args, **kwargs)
    import yaml
    rule = Path(sys.argv[1], "Knowledge Base", "_Governance", "rules", "patterns-external.yaml")
    document = yaml.safe_load(rule.read_text())
    document["ceiling"] = 0
    rule.write_text(yaml.safe_dump(document))
    return result
v.sample_stream = revoke
''')
    proofs = []
    release = egress.release_allows_frames

    def allows(root, relative, *, artifact_copy):
        proofs.append(artifact_copy)
        return release(root, relative, artifact_copy=artifact_copy)

    monkeypatch.setattr(egress, "release_allows_frames", allows)
    with (request_scope(RequestPrincipal(audience_id="external", surface="mcp")),
          policy.request_policy_snapshot(), state_paths.resolution_scope()):
        assert not policy.load(vault).empty
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get_video_frames(vault, path)
    assert (vault / "decoded").exists()
    assert len(proofs) == 2 and proofs[0] is proofs[1]
    assert proofs[0].size == len(b"video bytes")


def test_declared_oversized_jpeg_is_refused_before_reading_payload(vault, cloud_frames, monkeypatch):
    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    children = _injected_child(monkeypatch, _fake_decoder() + '''
wire = w._wire

def oversize(message):
    if isinstance(message, w._Result):
        message.frames[0].size = 1 << 40
        sys.stdout.buffer.write(wire(message))
        sys.stdout.buffer.flush()
        time.sleep(30)
    return wire(message)
w._wire = oversize
''')
    with pytest.raises(ValueError, match="VIDEO_PROTOCOL"):
        commands.op_get_video_frames(vault, path)
    assert children[0].poll() is not None


@pytest.mark.parametrize("fault", ["oversized header", "incomplete header", "malformed metadata"])
def test_broken_child_protocol_never_returns_a_partial_frame(vault, cloud_frames, monkeypatch, fault):
    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    payloads = {
        "oversized header": "sys.stdout.buffer.write((1 << 30).to_bytes(4, 'big'))",
        "incomplete header": "sys.stdout.buffer.write(b'xx')",
        "malformed metadata": "sys.stdout.buffer.write((2).to_bytes(4, 'big') + b'{}')",
    }
    children = _injected_child(monkeypatch, payloads[fault] + "\nsys.stdout.buffer.flush()\nraise SystemExit(0)")
    with pytest.raises(ValueError, match="VIDEO_PROTOCOL"):
        commands.op_get_video_frames(vault, path)
    assert children[0].poll() is not None


def test_media_slot_contention_starts_no_child(vault, cloud_frames, monkeypatch):
    from exomem import media_slot_client, video_frames_worker

    class Busy:
        released = False

        def acquire(self, *args):
            return False

        def release(self):
            self.released = True

    slot = Busy()
    monkeypatch.setattr(media_slot_client.SlotClient, "from_env", lambda: slot)
    monkeypatch.setattr(video_frames_worker, "_launch_child", lambda _: pytest.fail("busy slot launched decoding"))
    with pytest.raises(ValueError, match="VIDEO_BUSY"):
        commands.op_get_video_frames(vault, "Knowledge Base/Sources/video.mp4")
    assert slot.released


def test_vault_media_lock_contention_returns_retryable_busy(vault, cloud_frames):
    from exomem.media_jobs import worker_lock_path
    from exomem.media_worker_child import _VaultLock

    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    lock = _VaultLock(worker_lock_path(vault))
    assert lock.acquire()
    try:
        with pytest.raises(ValueError, match="VIDEO_BUSY"):
            commands.op_get_video_frames(vault, path)
    finally:
        lock.release()


def test_stalled_preparation_uses_the_existing_request_deadline(vault, cloud_frames, monkeypatch):
    import fcntl
    import time

    from exomem import request_budget, video_frames_worker

    path = "Knowledge Base/Sources/" + "/".join(['"' * 200] * 10) + "/video.mp4"
    children = _injected_child(monkeypatch, "time.sleep(30)")
    launch = video_frames_worker._launch_child

    def narrow_pipe(root):
        child = launch(root)
        fcntl.fcntl(child.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        return child

    monkeypatch.setattr(video_frames_worker, "_launch_child", narrow_pipe)
    token = request_budget.set_current(request_budget.RequestBudget(
        seconds=request_budget.DELIVERY_RESERVE_SECONDS + 0.3))
    began = time.monotonic()
    try:
        with pytest.raises(ValueError, match="VIDEO_TIMEOUT"):
            commands.op_get_video_frames(vault, path)
    finally:
        request_budget.reset_current(token)
    assert time.monotonic() - began < 3
    assert children[0].poll() is not None


@pytest.mark.parametrize("phase", ["preparation", "decoding"])
def test_registered_cloud_cancellation_stops_child_before_slot_release(vault, cloud_frames, monkeypatch, phase):
    import contextlib
    import time

    from exomem import media_slot_client

    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    stall = '''
import subprocess
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(sys.argv[1], "descendant").write_text(str(child.pid))
Path(sys.argv[1], "stalled").write_text("yes")
time.sleep(30)
'''
    if phase == "decoding":
        code = _fake_decoder() + "\ndef stalled(*args, **kwargs):\n" + "\n".join("    " + line for line in stall.splitlines())
        code += "\nv.sample_stream = stalled\n"
    else:
        code = stall
    children = _injected_child(monkeypatch, code)
    released = []

    class Slot:
        def acquire(self, *args):
            return True

        def release(self):
            assert children[0].poll() is not None
            descendant = int((vault / "descendant").read_text())
            stat = Path(f"/proc/{descendant}/stat")
            if stat.exists():
                assert stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
            released.append(True)

    monkeypatch.setattr(media_slot_client.SlotClient, "from_env", lambda: Slot())
    mcp = server.build_server(require_auth=True)

    async def call_and_cancel():
        call = asyncio.create_task(mcp.call_tool(
            "read_media", {"operation": {"kind": "video", "path": path}}, run_middleware=True))
        try:
            until = time.monotonic() + 8
            while not (vault / "stalled").exists():
                if call.done():
                    pytest.fail(f"registered call ended before owned work: {call.result()}")
                assert time.monotonic() < until, "registered call never started its child"
                await asyncio.sleep(0.02)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            until = time.monotonic() + 4
            while not released:
                assert time.monotonic() < until, "cancelled call left its worker or slot alive"
                await asyncio.sleep(0.02)
        finally:
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call

    try:
        asyncio.run(call_and_cancel())
    finally:
        mcp._exomem_local_runtime_activation._shutdown.set()


def test_frame_status_checks_codecs_without_importing_native_decoder(cloud_frames, monkeypatch):
    import importlib.util

    inspected = []

    def missing(name):
        inspected.append(name)
        return None

    monkeypatch.setattr(importlib.util, "find_spec", missing)
    assert media_engines.status()["frames"] == media_engines.UNAVAILABLE
    assert "av" in inspected


@pytest.fixture
def codec_video(tmp_path):
    import av
    import numpy as np

    video = tmp_path / "sample.mp4"
    with av.open(str(video), "w") as container:
        stream = container.add_stream("mpeg4", rate=8)
        stream.width, stream.height, stream.pix_fmt = 960, 640, "yuv420p"
        pattern = (np.indices((640, 960)).sum(axis=0) // 8 % 2 * 255).astype(np.uint8)
        for index in range(16):
            pixels = np.stack([np.roll(pattern, index * 3, axis=1), pattern,
                               np.full_like(pattern, index * 15)], axis=-1)
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return video


@pytest.mark.frame_codecs
def test_registered_cloud_read_hydrates_retained_video_and_returns_native_jpegs(
    vault, cloud_frames, monkeypatch, selected_store, codec_video,
):
    import hashlib
    import io
    import json
    import time

    from PIL import Image
    from test_media_store import _offload

    from exomem import (
        media_jobs,
        media_store,
        originals,
        preserve,
        video_frames,
        video_frames_worker,
    )

    video = codec_video
    original = video.read_bytes()
    captured = preserve.preserve(
        vault, scope="Test", category="video", filename="sample.mp4",
        content_stream=io.BytesIO(original), text="A small recorded sample.",
    )
    path = vault / captured.path
    expected = video_frames._get_frames_local(vault, captured.path, max_frames=2)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    assert originals.original_state(vault, path).state == "evicted"
    canonical = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    ledger = media_jobs.MediaJobStore(vault)
    with ledger._connect() as connection:
        jobs = [tuple(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")]
    real_read = video_frames_worker._read_message
    children, resource_readings, proofs = [], [], []
    release = commands.egress_module.release_allows_frames

    def tracked_release(root, relative, *, artifact_copy):
        proofs.append(artifact_copy)
        return release(root, relative, artifact_copy=artifact_copy)

    monkeypatch.setattr(commands.egress_module, "release_allows_frames", tracked_release)
    launch = video_frames_worker._launch_child
    stop = video_frames_worker._stop_child
    retained_leaders = []

    def tracked_stop(child):
        assert child.returncode is None, "the leader was reaped before process-group cleanup"
        exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert exited is not None
        retained_leaders.append(child.pid)
        return stop(child)

    def tracked_launch(root):
        child = launch(root)
        children.append(child)
        return child

    def tracked_read(stream, model, check):
        message = real_read(stream, model, check)
        if model is video_frames_worker._Result:
            status = Path(f"/proc/{children[0].pid}/status")
            if status.exists():
                resource_readings.extend(line for line in status.read_text().splitlines()
                                         if line.startswith(("VmHWM:", "VmRSS:")))
        return message

    monkeypatch.setattr(video_frames_worker, "_launch_child", tracked_launch)
    monkeypatch.setattr(video_frames_worker, "_stop_child", tracked_stop)
    monkeypatch.setattr(video_frames_worker, "_read_message", tracked_read)
    read_generic = reserved_paths.read_generic_bytes

    def no_original_copy(root, relative, *args, **kwargs):
        assert str(relative) != captured.path, "the serving process copied the original"
        return read_generic(root, relative, *args, **kwargs)

    monkeypatch.setattr(reserved_paths, "read_generic_bytes", no_original_copy)
    mcp = server.build_server(require_auth=True)
    began = time.monotonic()
    try:
        result = asyncio.run(mcp.call_tool(
            "read_media", {"operation": {"kind": "video", "path": captured.path, "max_frames": 2,
                                           "max_side": "original"}},
            run_middleware=True,
        ))
    finally:
        mcp._exomem_local_runtime_activation._shutdown.set()
    metadata = result.structured_content
    assert "error" not in metadata, metadata
    assert metadata["path"] == captured.path
    assert metadata["duration_sec"] == expected.duration_sec
    assert metadata["frames"] == [{"index": i, "timestamp_sec": f.timestamp_sec}
                                  for i, f in enumerate(expected.frames)]
    assert 0 < metadata["frame_count"] <= 2
    assert metadata["candidates"] == expected.candidates
    assert metadata["dedup_dropped"] == expected.dedup_dropped
    # Cloud keeps the edge its worker sizes memory for, and says so, whatever the caller asked.
    assert metadata["max_side_effective"] == video_frames.CLOUD_MAX_SIDE
    import base64

    for block in result.content[1:]:
        assert block.mime_type == "image/jpeg"
        with Image.open(io.BytesIO(base64.b64decode(block.data))) as image:
            assert image.format == "JPEG" and max(image.size) == video_frames.CLOUD_MAX_SIDE
    assert path.read_bytes() == original
    assert originals.original_state(vault, path).state == "cached"
    after = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert after.pop(path.relative_to(vault)) == original
    assert after == canonical
    with ledger._connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")] == jobs
        assert connection.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0
    assert len(proofs) == 2 and proofs[0] is proofs[1]
    assert proofs[0].sha256 == hashlib.sha256(original).hexdigest()
    with reserved_paths.open_generic_binary(vault, captured.path) as retained:
        assert proofs[0].source_version == retained.version
    assert children[0].poll() is not None
    assert retained_leaders == [children[0].pid]
    print(json.dumps({"duration_sec": round(time.monotonic() - began, 3),
                      "frames": metadata["frames"], "child_memory_kib": resource_readings,
                      "original_bytes": len(original), "sha256": hashlib.sha256(original).hexdigest()}))


def test_memory_admission_starts_no_child(vault, cloud_frames, monkeypatch):
    from exomem import media_brakes, video_frames_worker

    monkeypatch.setattr(media_brakes, "read_cell", lambda: media_brakes.CellMemory(
        int(2.3 * (1 << 30)), 3 << 30, None, 0.0))
    monkeypatch.setattr(video_frames_worker, "_launch_child", lambda _: pytest.fail("admission launched a child"))
    with pytest.raises(ValueError, match="VIDEO_BUSY"):
        commands.op_get_video_frames(vault, "Knowledge Base/Sources/video.mp4")


def test_pressure_stops_stalled_preparation_and_releases_slot(vault, cloud_frames, monkeypatch):
    from exomem import media_brakes, media_slot_client

    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    children = _injected_child(monkeypatch, "time.sleep(30)")
    released = []

    class Slot:
        def acquire(self, *args):
            return True

        def release(self):
            assert children[0].poll() is not None
            released.append(True)

    monkeypatch.setattr(media_slot_client.SlotClient, "from_env", lambda: Slot())
    monkeypatch.setattr(media_brakes, "read_cell", lambda: media_brakes.CellMemory(
        0, 3 << 30, None, 99.0) if children else cloud_frames)
    with pytest.raises(ValueError, match="VIDEO_BUSY"):
        commands.op_get_video_frames(vault, path)
    assert released


def test_default_off_cell_starts_no_frame_work(vault, cloud_frames, monkeypatch):
    from exomem import video_frames_worker

    monkeypatch.delenv("EXOMEM_MEDIA_ENGINES")
    monkeypatch.setattr(video_frames_worker, "_launch_child", lambda _: pytest.fail("disabled cell launched a child"))
    with pytest.raises(ValueError, match="VIDEO_UNAVAILABLE"):
        commands.op_get_video_frames(vault, "Knowledge Base/Sources/video.mp4")
    assert any(exclusion.command == "read_media" for exclusion in commands.cloud_surface_exclusions())


def test_frame_artifact_proof_reaches_both_disclosure_boundaries(vault, monkeypatch):
    from exomem.governance import egress

    proof = object()
    observed = []

    def content(root, path, who, **kwargs):
        observed.append(("content", kwargs))
        return True

    def binary(root, path, **kwargs):
        observed.append(("binary", kwargs))
        return True

    monkeypatch.setattr(egress, "content_permits", content)
    monkeypatch.setattr(egress, "_binary_boundary", binary)
    assert egress.release_allows_frames(vault, "sample.mp4", artifact_copy=proof)
    assert observed[0] == ("content", {"derived": True, "artifact_copy": proof})
    assert observed[1][1]["artifact_copy"] is proof
    assert observed[1][1]["boundary_name"] == "video_frame"
    assert observed[1][1]["minimum_level"] == egress.LEVEL_FULL


def test_request_cleanup_reaps_adopted_tools_in_its_own_process(vault, tmp_path, cloud_frames):
    import subprocess
    import sys
    import textwrap

    path = "Knowledge Base/Sources/video.mp4"
    (vault / path).write_bytes(b"video bytes")
    helper = textwrap.dedent('''
        import os, signal, subprocess, sys, threading
        from pathlib import Path
        from exomem import media_brakes, video_frames_worker as w
        from exomem.service_manager import enable_subreaper
        from exomem.video_frames import VideoFramesError

        # Only this disposable helper adopts children, never the shared test process.
        enable_subreaper()
        media_brakes.read_cell = lambda: media_brakes.CellMemory(0, 3 << 30, None, 0.0)
        marker = Path(sys.argv[2])
        cancelled = threading.Event()
        # The owned tool signals readiness, so cancellation cannot precede its launch.
        signal.signal(signal.SIGUSR1, lambda *_: cancelled.set())
        child_source = (
            "import os, signal, subprocess, sys, time\\n"
            "from pathlib import Path\\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\\n"
            "Path(sys.argv[1]).write_text(str(child.pid))\\n"
            "os.kill(os.getppid(), signal.SIGUSR1)\\n"
            "time.sleep(30)\\n"
        )
        decoder = None

        def owned_launch(root):
            global decoder
            decoder = subprocess.Popen([sys.executable, '-c', child_source, str(marker)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            return decoder

        w._launch_child = owned_launch
        token = w._CANCEL.set(cancelled)
        try:
            try:
                w.get_frames(Path(sys.argv[1]), 'Knowledge Base/Sources/video.mp4')
            except VideoFramesError as error:
                assert error.code == 'VIDEO_CANCELLED', error
            else:
                raise AssertionError('the request ignored cancellation after tool startup')
            assert decoder is not None and decoder.returncode is not None
            descendant = int(marker.read_text())
            try:
                os.waitpid(descendant, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                raise AssertionError('the adopted tool was not reaped')
            assert not Path('/proc', str(descendant)).exists()
            print('owned decoder and adopted tool reaped')
        finally:
            w._CANCEL.reset(token)
            if decoder is not None and decoder.returncode is None:
                os.killpg(decoder.pid, signal.SIGKILL)
                decoder.wait()
    ''')
    completed = subprocess.run([sys.executable, "-c", helper, str(vault), str(tmp_path / "descendant")],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert "owned decoder and adopted tool reaped" in completed.stdout


def test_missing_and_excluded_originals_share_not_found_refusal(vault, cloud_frames, monkeypatch):
    from exomem import access

    excluded = "Knowledge Base/Sources/excluded.mp4"
    (vault / excluded).write_bytes(b"private video bytes")
    monkeypatch.setattr(access, "refuse_if_excluded", lambda root, relative: relative == excluded)
    for path in ("Knowledge Base/Sources/missing.mp4", excluded):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get_video_frames(vault, path)


@pytest.mark.frame_codecs
def test_original_hash_failure_keeps_all_candidates_before_the_frame_cap(codec_video, monkeypatch):
    from exomem import embeddings, video_frames

    original_hash = embeddings._avg_hash

    def fail_before_resize(image):
        if max(image.size) > video_frames.CLOUD_MAX_SIDE:
            raise ValueError("the original image hash is unavailable")
        return original_hash(image)

    monkeypatch.setattr(embeddings, "_avg_hash", fail_before_resize)
    with codec_video.open("rb") as source:
        local = video_frames.sample_stream(source, "sample.mp4", max_frames=2)
    with codec_video.open("rb") as source:
        bounded = video_frames.sample_stream(source, "sample.mp4", max_frames=2, resize_candidates=True)

    assert len(local.frames) == len(bounded.frames) == 2
    assert local.dedup_dropped == bounded.dedup_dropped == 0
    assert [frame.timestamp_sec for frame in bounded.frames] == [frame.timestamp_sec for frame in local.frames]
