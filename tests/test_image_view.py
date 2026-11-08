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

from exomem import commands, download_links, server, server_transfer

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
    # This test process stands in for the HTTP worker that answers /download.
    monkeypatch.setattr(download_links, "_serving", True)
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
    original = _photo(3000, 2000, orientation=6, taken="2021:06:15 09:30:00")
    path = _upload(client, "IMG_0001.jpg", original, "image/jpeg")

    result = _call(mcp, {"kind": "image", "path": path})

    meta = result.structured_content
    assert meta["outcome"].startswith("Showing 1 image below")
    [shown] = meta["images"]
    assert shown["path"] == path
    assert shown["file_name"] == "IMG_0001.jpg"
    assert shown["taken_at"] == "2021-06-15T09:30:00"
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

    result = _call(mcp, {"kind": "image", "paths": [before, after], "max_side": 256})

    images = result.structured_content["images"]
    assert [item["file_name"] for item in images] == ["before.jpg", "after.jpg"]
    assert [item["shown_pixels"] for item in images] == [[256, 192], [256, 192]]
    # The JPEG decodes at a reduced scale; the reported original is still the stored size.
    assert [item["original_pixels"] for item in images] == [[800, 600], [640, 480]]
    assert [block.type for block in result.content] == ["text", "image", "image"]


def test_a_raw_photo_names_the_download_instead(served):
    mcp, client = served
    path = _upload(client, "IMG_0002.dng", b"II*\x00" + b"\x00" * 64, "image/x-adobe-dng")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_FORMAT_UNSUPPORTED" in refused
    assert "transfer_artifact operation=download" in refused


def test_download_link_is_short_and_serves_the_exact_original(served, vault):
    """The incident: a 2,116-character link was copied wrong and the download failed."""
    from exomem.governance.principal import owner_principal, request_scope

    mcp, client = served
    original = _photo(1200, 900)
    path = _upload(client, "progress.jpg", original, "image/jpeg")
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(vault, operation="download", artifact=path)

    assert handoff["preferred_delivery"] == "https"
    url = handoff["deliveries"]["https"]["url"]
    grant = parse_qs(urlsplit(url).query)["grant"][0]
    assert len(grant) == 22, url
    downloaded = client.get(url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original

    stale = client.get(url.replace(grant, ("A" if grant[0] != "A" else "B") + grant[1:]))
    assert stale.status_code == 404
    assert stale.json()["code"] == "DOWNLOAD_LINK_EXPIRED"


def test_a_full_signed_grant_still_downloads_from_a_process_without_short_links(served, vault, monkeypatch):
    """Links minted before this change, or by a stdio process, carry the whole grant."""
    from exomem.governance.principal import owner_principal, request_scope

    _mcp, client = served
    original = _photo(400, 300)
    path = _upload(client, "IMG_0003.jpg", original, "image/jpeg")
    monkeypatch.setattr(download_links, "_serving", False)
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(vault, operation="download", artifact=path)

    url = handoff["deliveries"]["https"]["url"]
    assert len(parse_qs(urlsplit(url).query)["grant"][0]) > 500
    downloaded = client.get(url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original


def test_a_caller_below_full_disclosure_cannot_see_the_image(served, vault):
    """The image read must refuse wherever the original download refuses."""
    from test_governance_egress import _external, _reset_governance_caches, write_rule, write_scope

    from exomem.governance.principal import request_scope

    _mcp, client = served
    path = _upload(client, "IMG_0004.jpg", _photo(400, 300), "image/jpeg")
    write_scope(vault, paths=path.removeprefix("Knowledge Base/"))
    write_rule(vault, ceiling=5)
    _reset_governance_caches()

    with request_scope(_external()), pytest.raises(ValueError, match="ORIGINAL_UNAVAILABLE"):
        commands.op_read_media(vault, media="image", path=path)

    write_rule(vault, ceiling=6)
    _reset_governance_caches()
    with request_scope(_external()):
        shown = commands.op_read_media(vault, media="image", path=path)
    assert shown.structured_content["images"][0]["file_name"] == "IMG_0004.jpg"


def test_content_that_is_not_an_image_format_never_reaches_another_decoder(served):
    """A .jpg holding PostScript must not start Ghostscript in the serving process."""
    mcp, client = served
    postscript = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\nshowpage\n"
    path = _upload(client, "IMG_0005.jpg", postscript, "image/jpeg")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_UNDECODABLE" in refused


def test_an_image_too_large_to_decode_names_the_download(served, monkeypatch):
    """A small file can declare a huge canvas; it is refused before any pixel is decoded."""
    from PIL import Image, ImageFile

    mcp, client = served
    out = io.BytesIO()
    Image.new("L", (9000, 6000)).save(out, format="PNG")
    path = _upload(client, "IMG_0006.png", out.getvalue(), "image/png")
    decoded = []
    load = ImageFile.ImageFile.load
    monkeypatch.setattr(ImageFile.ImageFile, "load", lambda image: decoded.append(image.size) or load(image))

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_TOO_LARGE" in refused
    assert (9000, 6000) not in decoded, "the canvas was decoded before the size check"
    assert "transfer_artifact operation=download" in refused


def test_a_black_and_white_scan_keeps_its_thin_lines_when_downscaled(served):
    """Bilevel scans shrink by nearest neighbour unless converted first, dropping strokes."""
    from PIL import Image, ImageDraw

    mcp, client = served
    scan = Image.new("1", (3136, 400), 1)
    draw = ImageDraw.Draw(scan)
    for x in range(0, 3136, 2):
        draw.line([(x, 0), (x, 399)], fill=0)
    out = io.BytesIO()
    scan.save(out, format="PNG")
    path = _upload(client, "IMG_0007.png", out.getvalue(), "image/png")

    result = _call(mcp, {"kind": "image", "path": path})

    seen = Image.open(io.BytesIO(base64.b64decode(result.content[1].data))).convert("L")
    # Half the columns are black, so the shrunk image is mid grey, not blank white.
    assert 64 < sum(seen.getdata()) / (seen.size[0] * seen.size[1]) < 192
