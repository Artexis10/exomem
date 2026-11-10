"""An agent can look at a preserved photo, and a person gets a short download link for it."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tempfile
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from _public_call import public_call
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
        return asyncio.run(mcp.call_tool(*public_call("read_media", {"operation": operation}), run_middleware=True))


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


SAMPLES = Path(__file__).parent / "fixtures" / "media-samples"


def test_agent_sees_a_camera_raw_photo_upright_at_its_sensor_size(served, vault):
    """Defect: a preserved DNG answers IMAGE_FORMAT_UNSUPPORTED, so the agent cannot look at it."""
    from PIL import Image

    mcp, client = served
    # A phone raw shot in portrait: the sensor rows are stored turned, with orientation 6.
    original = (SAMPLES / "ocr-eng.dng").read_bytes()
    path = _upload(client, "PXL_0001.dng", original, "image/x-adobe-dng")

    result = _call(mcp, {"kind": "image", "path": path})

    [shown] = result.structured_content["images"]
    assert shown["original_pixels"] == [1068, 280]
    assert shown["shown_pixels"] == [1068, 280]
    seen = Image.open(io.BytesIO(base64.b64decode(result.content[1].data)))
    assert seen.size == (1068, 280), "the raw must reach the model upright"
    assert hashlib.sha256((vault / path).read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest()


def _dng(width: int, height: int, tags: dict | None = None, *, pixels: bytes = b"") -> bytes:
    """A DNG directory declaring a 16-bit sensor image of `width` x `height`, then `pixels`."""
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    # TIFF field types for the tags Pillow does not know: SHORT, BYTE and RATIONAL.
    types = {33421: 3, 33422: 1, 50706: 1, 50718: 5, 50829: 3}
    directory = ImageFileDirectory_v2()
    fields = {
        254: 0, 256: width, 257: height, 258: 16, 259: 1, 262: 32803, 277: 1, 278: height,
        273: 4096, 279: max(len(pixels), 2), 33421: (2, 2), 33422: b"\x00\x01\x01\x02",
        50706: b"\x01\x04\x00\x00", 50717: 4095, **(tags or {}),
    }
    for tag, value in fields.items():
        if value is None:  # the caller removes a default field
            continue
        if tag in types:
            directory.tagtype[tag] = types[tag]
        directory[tag] = value
    out = io.BytesIO()
    out.write(b"II*\x00\x08\x00\x00\x00")
    directory.save(out)
    return out.getvalue().ljust(4096, b"\x00") + pixels


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(_dng(60000, 60000), id="declared-huge"),
        # DefaultScale stretches a 4 MP sensor image tenfold on output.
        pytest.param(_dng(4000, 1000, {50718: (Fraction(10), Fraction(1))}), id="stretched"),
        # ActiveArea shows 32 x 32 pixels of an 8000 x 8000 sensor buffer that is still unpacked.
        pytest.param(_dng(8000, 8000, {50829: (0, 0, 32, 32)}), id="small-active-area"),
    ],
)
def test_a_camera_raw_that_would_decode_too_large_is_refused_before_it_is_demosaiced(served, monkeypatch, declared):
    """Defect: a small hostile DNG demosaics gigabytes in the serving process before any size check."""
    from exomem import image_view

    mcp, client = served
    monkeypatch.setattr(image_view, "MAX_DECODE_PIXELS", 10_000_000)
    path = _upload(client, "PXL_0002.dng", declared, "image/x-adobe-dng")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_TOO_LARGE" in refused


def test_bytes_that_are_not_a_dng_never_reach_the_raw_decoder(served):
    """Defect: LibRaw decodes any file of a known sensor-dump length, so noise named .dng is shown and indexed."""
    mcp, client = served
    path = _upload(client, "PXL_0004.dng", bytes(range(256)) * 3072, "image/x-adobe-dng")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_UNDECODABLE" in refused


def test_a_jpeg_saved_under_a_raw_extension_is_shown(served):
    """Defect: phone exports name their JPEG rendering .dng, and every such photo failed as 'not a DNG'."""
    from PIL import Image

    mcp, client = served
    rendered = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 40, 40)).save(rendered, "JPEG")
    path = _upload(client, "PXL_0006.dng", rendered.getvalue(), "image/x-adobe-dng")

    result = _call(mcp, {"kind": "image", "path": path})

    [shown] = result.structured_content["images"]
    assert shown["original_pixels"] == [40, 30]


def test_a_monochrome_camera_raw_is_shown_in_greyscale(served):
    """Defect: a one-channel raw crashes the decoder instead of reaching the model."""
    mcp, client = served
    # LinearRaw, one sample per pixel. LibRaw reads past the declared strip, so the file is padded.
    mono = _dng(64, 64, {262: 34892, 33421: None, 33422: None}, pixels=b"\x00\x08" * 64 * 64 + bytes(65536))
    path = _upload(client, "PXL_0005.dng", mono, "image/x-adobe-dng")

    result = _call(mcp, {"kind": "image", "path": path})

    [shown] = result.structured_content["images"]
    assert shown["original_pixels"] == [64, 64]


def test_a_camera_raw_with_a_malformed_date_is_shown_without_one(served):
    """Defect: a bad EXIF date in a raw throws away a decode that succeeded, where a JPEG loses only the date."""
    mcp, client = served
    dated = _dng(64, 64, {262: 34892, 33421: None, 33422: None, 306: "99999999:1:1 0:0:0"},
                 pixels=b"\x00\x08" * 64 * 64 + bytes(65536))
    path = _upload(client, "PXL_0006.dng", dated, "image/x-adobe-dng")

    [shown] = _call(mcp, {"kind": "image", "path": path}).structured_content["images"]

    assert shown["taken_at"] is None


def test_a_raw_the_decoder_runs_out_of_memory_on_names_the_download(served, monkeypatch):
    """Defect: decoder memory exhaustion surfaces as an unhandled error that names nothing to change."""
    from exomem import image_codecs

    mcp, client = served
    path = _upload(client, "PXL_0007.dng", (SAMPLES / "ocr-eng.dng").read_bytes(), "image/x-adobe-dng")

    def exhausted(*_args, **_kwargs):
        raise MemoryError("camera raw decoding ran out of memory")

    monkeypatch.setattr(image_codecs, "_open_raw", exhausted)

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_TOO_LARGE" in refused
    assert "download_files(pages=" in refused


def test_a_damaged_camera_raw_is_named_undecodable(served):
    mcp, client = served
    path = _upload(client, "PXL_0003.dng", b"II*\x00" + b"\x00" * 64, "image/x-adobe-dng")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_UNDECODABLE" in refused


def test_a_format_without_a_decoder_names_the_download_instead(served):
    mcp, client = served
    path = _upload(client, "cover.psd", b"8BPS" + b"\x00" * 64, "image/vnd.adobe.photoshop")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path}).structured_content)

    assert "IMAGE_FORMAT_UNSUPPORTED" in refused
    assert "download_files(pages=" in refused


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
    assert "download_files(pages=" in refused
    # A PNG decodes at full size whatever is asked, so a smaller request would fail the same way.
    assert "smaller max_side" not in refused


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


def test_an_agent_can_ask_for_the_original_resolution(served):
    """No size ceiling in code: a model that reads detail better at full size can have it."""
    mcp, client = served
    path = _upload(client, "IMG_0010.jpg", _photo(3000, 2000, orientation=6), "image/jpeg")

    result = _call(mcp, {"kind": "image", "path": path, "max_side": "original"})

    [shown] = result.structured_content["images"]
    assert shown["shown_pixels"] == shown["original_pixels"] == [2000, 3000]


def test_the_default_size_comes_from_the_setting(served, monkeypatch):
    """The size a call gets by default is the deployment's setting, so a better model needs no release."""
    monkeypatch.setenv("EXOMEM_IMAGE_VIEW_MAX_SIDE", "640")
    mcp, client = served
    path = _upload(client, "IMG_0011.jpg", _photo(3000, 2000), "image/jpeg")

    [shown] = _call(mcp, {"kind": "image", "path": path}).structured_content["images"]

    assert max(shown["shown_pixels"]) == 640


def test_a_region_shows_its_detail_at_full_resolution(served):
    """A crop must come from the full-resolution decode, not from an already shrunk image."""
    from PIL import Image

    mcp, client = served
    # The left half is dark: the top-left quarter of this photo is all dark.
    path = _upload(client, "IMG_0012.jpg", _photo(4000, 3000), "image/jpeg")

    result = _call(mcp, {"kind": "image", "path": path, "region": [0, 0, 0.25, 0.25], "max_side": 1000})

    [shown] = result.structured_content["images"]
    # The region is 1000 x 750 stored pixels, so a 1000 px view keeps every one of them.
    assert shown["shown_pixels"] == [1000, 750]
    seen = Image.open(io.BytesIO(base64.b64decode(result.content[1].data))).convert("L")
    assert sum(seen.getdata()) / (seen.size[0] * seen.size[1]) < 40


def test_a_region_outside_the_image_is_refused_with_what_to_change(served):
    mcp, client = served
    path = _upload(client, "IMG_0013.jpg", _photo(800, 600), "image/jpeg")

    refused = json.dumps(_call(mcp, {"kind": "image", "path": path, "region": [0.5, 0, 0.6, 1]}).structured_content)

    assert "IMAGE_REGION_INVALID" in refused
    assert "x + width" in refused


def _noise(width: int, height: int) -> bytes:
    """A photo JPEG compresses badly, like a real one, so its size can pass an inline limit."""
    import random

    from PIL import Image

    rng = random.Random(3)
    image = Image.frombytes("RGB", (width, height), bytes(rng.getrandbits(8) for _ in range(width * height * 3)))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=95)
    return out.getvalue()


def test_a_view_over_the_clients_image_limit_is_shrunk_and_links_the_original(served, monkeypatch):
    """A client that rejects a large inline image still gets a view, and the original's link for its own crop.

    The client's limit applies to the base64 an image block carries, a third larger than the JPEG: a
    view whose JPEG fits but whose base64 does not must still be shrunk.
    """
    mcp, client = served
    path = _upload(client, "IMG_0014.jpg", _noise(600, 400), "image/jpeg")
    full = _call(mcp, {"kind": "image", "path": path, "max_side": "original"})
    sent = len(full.content[1].data)
    limit = sent - 1000
    assert limit > len(base64.b64decode(full.content[1].data)), "the JPEG alone must fit the limit"
    monkeypatch.setenv("EXOMEM_IMAGE_VIEW_INLINE_MAX_BYTES", str(limit))

    result = _call(mcp, {"kind": "image", "path": path, "max_side": "original"})

    [shown] = result.structured_content["images"]
    assert shown["shrunk_to_fit"] is True and max(shown["shown_pixels"]) < 600
    assert len(result.content[1].data) <= limit
    assert shown["original_link"].startswith("https://archive.example/download?grant=")


def test_a_view_longer_than_the_clients_pixel_limit_is_shrunk_and_links_the_original(served, monkeypatch):
    """A client rejects an image past its pixel limit however few bytes it has."""
    monkeypatch.setenv("EXOMEM_IMAGE_VIEW_INLINE_MAX_SIDE", "300")
    mcp, client = served
    path = _upload(client, "IMG_0015.jpg", _photo(800, 600), "image/jpeg")

    [shown] = _call(mcp, {"kind": "image", "path": path, "max_side": "original"}).structured_content["images"]

    assert shown["shown_pixels"] == [300, 225] and shown["shrunk_to_fit"] is True
    assert shown["original_link"].startswith("https://archive.example/download?grant=")


def test_a_call_that_runs_out_of_request_time_returns_what_it_showed(served, monkeypatch):
    """Past the request's time, a call keeps the images it has and names the rest, never fails whole."""
    from exomem import request_budget

    mcp, client = served
    paths = [_upload(client, f"IMG_003{n}.jpg", _photo(400, 300), "image/jpeg") for n in range(3)]
    monkeypatch.setattr(request_budget, "can_afford", lambda reserve: False)

    meta = _call(mcp, {"kind": "image", "paths": paths}).structured_content

    assert [item["path"] for item in meta["images"]] == paths[:1]
    assert meta["not_shown"] == paths[1:]
    assert "2 more ran out of request time" in meta["outcome"]


def test_images_past_the_calls_byte_limit_are_listed_not_dropped(served, monkeypatch):
    """No count cap: a call shows what fits and names the rest, so nothing goes missing unseen."""
    monkeypatch.setenv("EXOMEM_IMAGE_VIEW_MAX_CALL_BYTES", "1")
    mcp, client = served
    paths = [_upload(client, f"IMG_002{n}.jpg", _photo(400, 300), "image/jpeg") for n in range(3)]

    meta = _call(mcp, {"kind": "image", "paths": paths}).structured_content

    assert [item["path"] for item in meta["images"]] == paths[:1]
    assert meta["not_shown"] == paths[1:]
    assert "2 more did not fit" in meta["outcome"]
