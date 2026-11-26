"""The face step of the media worker, on real pictures and the real worker path.

The repository holds no licence-clean pictures of faces, so the face models are replaced at their boundary
(`face_models.load`) by a detector that reports each solid red or green square as a face. Everything around
it is real: decoding, EXIF rotation, the CLIP keyframes, the worker's checks and the face stores.
"""

import io
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    embedding_backend,
    embeddings,
    extract,
    face_models,
    face_scan,
    face_store,
    media_models,
    media_worker,
    preserve,
)

WIDTH = int(media_models.model_spec("face-embed")["dimensions"])
#: Each square colour the stub detector reports as a face, with that face's unit vector.
SQUARES = {(230, 30, 30): 0, (30, 200, 30): 1}


class _SquareFaces:
    """Stands in for `face_models.FaceModels`: every solid red or green square is a face."""

    def faces(self, picture) -> list[face_models.Face]:
        pixels = np.asarray(picture.convert("RGB")).astype(int)
        found = []
        for colour, axis in SQUARES.items():
            close = (np.abs(pixels - np.array(colour)).max(axis=2) < 60).nonzero()
            if len(close[0]) < 100:
                continue
            top, left = close[0].min(), close[1].min()
            box = (float(left), float(top), float(close[1].max() - left + 1), float(close[0].max() - top + 1))
            vector = np.zeros(WIDTH, dtype=np.float32)
            vector[axis] = 1.0
            found.append(face_models.Face(box, 0.99, vector))
        return found


@pytest.fixture(autouse=True)
def _no_loaded_face_models():
    face_scan.unload()
    yield
    face_scan.unload()


def turn_faces_on(vault: Path) -> None:
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at="2026-01-05T10:00:00+00:00", surface="test")


def face_rows(vault: Path) -> list[tuple]:
    with closing(sqlite3.connect(face_store.index_path(vault))) as index:
        return index.execute("SELECT page, frame_ts, x, y, width, height FROM faces ORDER BY page, frame_ts, x").fetchall()


def clip_job(vault: Path, stored, media_type: str) -> media_worker._Job:
    return media_worker._Job(
        binary_path=vault / stored.path,
        sidecar_path=vault / stored.sidecar_path,
        media_type=media_type,
        do_ocr=False,
        do_clip=True,
    )


def test_an_image_with_two_faces_gets_two_rows_in_read_media_region_form(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches boxes in rotated or pixel coordinates that `read_media` would crop wrongly."""
    from PIL import Image

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setattr(embeddings, "embed_image", lambda path: np.ones(embeddings.CLIP_DIM, dtype=np.float32))
    monkeypatch.setattr(face_models, "load", _SquareFaces)
    turn_faces_on(vault)
    # Upright, the picture is 200 wide and 300 tall: red square top left, green square lower right.
    upright = Image.new("RGB", (200, 300), (240, 240, 240))
    upright.paste((230, 30, 30), (20, 30, 80, 90))
    upright.paste((30, 200, 30), (120, 180, 180, 240))
    # The camera stored it on its side, with EXIF orientation 6 telling viewers to turn it upright.
    exif = Image.Exif()
    exif[0x0112] = 6
    data = io.BytesIO()
    upright.transpose(Image.Transpose.ROTATE_90).save(data, "JPEG", quality=95, exif=exif)
    stored = preserve.preserve_bytes(
        vault, scope="Rental", category="photos", filename="porch.jpg", data=data.getvalue(), text="porch"
    )

    media_worker.MediaWorker(vault)._process(clip_job(vault, stored, "image"))

    rows = face_rows(vault)
    assert [row[:2] for row in rows] == [(stored.path, None), (stored.path, None)]
    regions = [row[2:] for row in rows]
    assert regions == [
        pytest.approx((20 / 200, 30 / 300, 60 / 200, 60 / 300), abs=0.02),
        pytest.approx((120 / 200, 180 / 300, 60 / 200, 60 / 300), abs=0.02),
    ]


def write_video(path: Path, scenes: list[tuple[int, int, int]], seconds_per_scene: int = 6) -> None:
    """An mp4 at 8 frames a second: each scene a plain background with a red square that drifts right."""
    import av

    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=8)
        stream.width = stream.height = 160
        stream.pix_fmt = "yuv420p"
        stream.codec_context.gop_size = 8
        for background in scenes:
            for number in range(8 * seconds_per_scene):
                pixels = np.empty((160, 160, 3), dtype=np.uint8)
                pixels[:] = background
                left = 10 + number
                pixels[50:110, left : left + 60] = (230, 30, 30)
                frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def preserve_video(vault: Path, tmp_path: Path, scenes: list[tuple[int, int, int]], filename: str):
    pytest.importorskip("av")
    source = tmp_path / filename
    write_video(source, scenes)
    return preserve.preserve_bytes(
        vault, scope="Rental", category="video", filename=filename, data=source.read_bytes(), text="walk"
    )


def use_fake_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_clip import _FakeClip

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: _FakeClip())


def test_a_video_gets_face_rows_at_the_clip_keyframe_times(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches keyframe faces that cannot collapse to the video: rows at times the CLIP index never held."""
    use_fake_clip(monkeypatch)
    monkeypatch.setattr(face_models, "load", _SquareFaces)
    turn_faces_on(vault)
    stored = preserve_video(vault, tmp_path, [(20, 20, 160), (240, 220, 120)], "walk.mp4")

    media_worker.MediaWorker(vault, execution_mode="inline")._process(clip_job(vault, stored, "video"))

    keyframe_times = embeddings.ClipIndex(vault).frame_timestamps(stored.path)
    rows = face_rows(vault)
    assert len(keyframe_times) >= 2
    assert {row[0] for row in rows} == {stored.path}
    assert sorted(row[1] for row in rows) == pytest.approx(sorted(keyframe_times))


def test_a_video_in_scene_mode_gets_face_rows_at_its_scene_keyframes_and_its_scene_frames_get_none(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches duplicate faces from the scene-frame images that the video's keyframes already cover."""
    use_fake_clip(monkeypatch)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    monkeypatch.setattr(face_models, "load", _SquareFaces)
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(text="", media_type="image", engine="ocr:test"),
    )
    turn_faces_on(vault)
    stored = preserve_video(vault, tmp_path, [(20, 20, 160), (240, 220, 120)], "scenes.mp4")
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    worker._process(clip_job(vault, stored, "video"))
    while not worker._q.empty():
        # The jobs the worker queued for the scene-frame images it wrote.
        worker._process(worker._q.get_nowait())

    scene_frames = sorted((vault / stored.path).parent.glob("scenes.mp4.frames/*.jpg"))
    assert scene_frames
    keyframe_times = embeddings.ClipIndex(vault).frame_timestamps(stored.path)
    rows = face_rows(vault)
    assert {row[0] for row in rows} == {stored.path}
    assert sorted(row[1] for row in rows) == pytest.approx(sorted(keyframe_times))


def test_a_face_model_that_fails_its_digest_check_leaves_extraction_and_clip_as_they_were(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a face failure that blocks ordinary media processing."""
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    clip_vector = np.linspace(-1.0, 1.0, embeddings.CLIP_DIM, dtype=np.float32)
    clip_vector /= np.linalg.norm(clip_vector)
    monkeypatch.setattr(embeddings, "embed_image", lambda path: clip_vector)
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="a receipt for two coffees", media_type="image", engine="ocr:test"
        ),
    )

    def digest_mismatch(model_name, served):
        raise embedding_backend.ModelFilesUnavailable(f"{model_name}: the installed bytes do not match the digest")

    monkeypatch.setattr(embedding_backend, "ensure_artifact", digest_mismatch)
    turn_faces_on(vault)
    stored = preserve.preserve_stream(
        vault, scope="Rental", category="photos", filename="receipt.jpg", stream=io.BytesIO(b"\xff\xd8\xff"),
        media_pending=True,
    )
    job = media_worker._Job(
        binary_path=vault / stored.path, sidecar_path=vault / stored.sidecar_path, media_type="image",
        do_ocr=True, do_clip=True,
    )

    media_worker.MediaWorker(vault, execution_mode="inline")._process(job)

    assert "a receipt for two coffees" in (vault / stored.sidecar_path).read_text(encoding="utf-8")
    paths, _, matrix = embeddings.ClipIndex(vault).all_vectors()
    assert paths == [stored.path]
    np.testing.assert_array_equal(matrix[0], clip_vector)
    assert face_rows(vault) == []
    setting = face_store.read_setting(vault)
    assert setting.enabled is True
    assert "do not match the digest" in setting.unavailable


def test_the_pinned_face_models_find_no_face_in_a_photo_of_a_bicycle(tmp_path: Path) -> None:
    """Catches pinned models that fail on a real photo: a shape, scale or decode error in the real path."""
    if not all(media_models.media_model_cached(kind) for kind in (face_models.DETECT, face_models.EMBED)):
        pytest.skip("the pinned face models are not installed on this host")
    try:
        models = face_models.load()
    except face_models.FacesUnavailable as error:
        pytest.skip(str(error))
    photo = Path(__file__).parent / "fixtures" / "clip" / "photo.jpg"

    assert face_scan.image_faces(models, photo) == []
