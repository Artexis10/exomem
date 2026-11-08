"""An agent can look at a preserved photo, and a person gets a short download link for it."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tempfile
from urllib.parse import parse_qs, urlsplit

import pytest
from test_upload_endpoint import _ASGIClient

from exomem import server, server_transfer

# Runs in CI's frame-codecs step, the job that installs the pixel decoders.
pytestmark = pytest.mark.frame_codecs
pytest.importorskip("PIL")


def _photo(width: int, height: int, *, orientation: int | None = None, taken: str | None = None) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), (200, 30, 30))
    # A dark left half lets the test see which way the returned image is turned.
    image.paste((10, 10, 10), (0, 0, width // 2, height))
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    if taken is not None:
        exif.get_ifd(0x8769)[0x9003] = taken
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90, exif=exif)
    return out.getvalue()


@pytest.fixture
def served(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    async def inline_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline_threadpool)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "transport-secret")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "private-authority")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://archive.example")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    mcp = server.build_server(require_auth=False)
    return mcp, _ASGIClient(mcp.http_app())


def _upload(client, name: str, data: bytes, content_type: str) -> str:
    stored = client.post(
        "/upload",
        files={"file": (name, data, content_type)},
        data={"scope": "Progress", "category": "Photos"},
        headers={"authorization": "Bearer transport-secret"},
    )
    assert stored.status_code == 201, stored.text
    return stored.json()["path"]


def _call(mcp, operation: dict):
    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal(surface="mcp")):
        return asyncio.run(mcp.call_tool("read_media", {"operation": operation}, run_middleware=True))


def test_agent_sees_a_preserved_photo_upright_and_downscaled(served, vault):
    """The incident: an agent found the photo's record but could not look at the photo."""
    from PIL import Image

    mcp, client = served
    # Stored landscape, shot in portrait: EXIF orientation 6 turns it a quarter turn.
    original = _photo(3000, 2000, orientation=6, taken="2023:04:08 18:58:04")
    path = _upload(client, "20230408_185804.jpg", original, "image/jpeg")

    result = _call(mcp, {"kind": "image", "path": path})

    meta = result.structured_content
    assert meta["outcome"].startswith("Showing 1 image below")
    [shown] = meta["images"]
    assert shown["path"] == path
    assert shown["file_name"] == "20230408_185804.jpg"
    assert shown["taken_at"] == "2023-04-08T18:58:04"
    assert shown["original_pixels"] == [2000, 3000]
    assert max(shown["shown_pixels"]) == 1568
    [text, image_block] = result.content
    assert json.loads(text.text) == meta
    assert image_block.type == "image" and image_block.mime_type == "image/jpeg"
    seen = Image.open(io.BytesIO(base64.b64decode(image_block.data)))
    assert seen.size == tuple(shown["shown_pixels"])
    assert seen.size[0] < seen.size[1], "a portrait photo must reach the model upright"
    # The stored original is untouched.
    assert hashlib.sha256((vault / path).read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest()


def test_agent_compares_photos_in_one_call(served):
    mcp, client = served
    before = _upload(client, "before.jpg", _photo(800, 600), "image/jpeg")
    after = _upload(client, "after.jpg", _photo(640, 480), "image/jpeg")

    result = _call(mcp, {"kind": "image", "paths": [before, after], "max_side": 512})

    assert [item["file_name"] for item in result.structured_content["images"]] == ["before.jpg", "after.jpg"]
    assert [item["shown_pixels"] for item in result.structured_content["images"]] == [[512, 384], [512, 384]]
    assert [block.type for block in result.content] == ["text", "image", "image"]


def test_a_raw_photo_names_the_download_instead(served):
    mcp, client = served
    path = _upload(client, "20251117_155233.dng", b"II*\x00" + b"\x00" * 64, "image/x-adobe-dng")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_FORMAT_UNSUPPORTED" in refused
    assert "transfer_artifact operation=download" in refused


def test_download_link_is_short_and_serves_the_exact_original(served, vault):
    """The incident: a 2,116-character link was copied wrong and the download failed."""
    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope

    mcp, client = served
    original = _photo(1200, 900)
    path = _upload(client, "progress.jpg", original, "image/jpeg")
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(vault, operation="download", artifact=path)

    assert handoff["preferred_delivery"] == "https"
    url = handoff["deliveries"]["https"]["url"]
    assert len(url) < 80, url
    downloaded = client.get(url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original

    grant = parse_qs(urlsplit(url).query)["grant"][0]
    stale = client.get(url.replace(grant, ("A" if grant[0] != "A" else "B") + grant[1:]))
    assert stale.status_code == 404
    assert stale.json()["code"] == "DOWNLOAD_LINK_EXPIRED"
