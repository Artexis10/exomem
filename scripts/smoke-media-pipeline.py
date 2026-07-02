"""End-to-end smoke of the whole searchable-binaries arc — REAL engines, no claude.ai.

Drives the actual ASGI app (build_server + TestClient) against a throwaway vault:
  1. /upload an image with text in it  → server OCRs it (Tesseract) → find by that text
  2. /upload a textless red image       → server CLIP-embeds it    → find "a red square"
  3. /upload a text PDF                  → server reads it (PyMuPDF) → find by that text
  4. /download one of them back          → bytes match (scoped token)
Proves the bytes path (no base64), server-side transduction, first-class media find,
CLIP visual search, and the /download reverse channel — all wired together.

Run from a box with the [media] extra: uv run python scripts/smoke-media-pipeline.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOKEN = "smoke-token"


def _wait(predicate, *, timeout=120.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def main() -> int:
    # Real engines on; throwaway vault from the test fixtures.
    for flag in ("EXOMEM_DISABLE_MEDIA_EXTRACTION", "EXOMEM_DISABLE_CLIP", "EXOMEM_DISABLE_EMBEDDINGS"):
        os.environ.pop(flag, None)
    vault = Path(tempfile.mkdtemp()) / "vault"
    shutil.copytree(REPO / "tests" / "fixtures", vault)
    os.environ["EXOMEM_VAULT_PATH"] = str(vault)
    os.environ["EXOMEM_UPLOAD_TOKEN"] = TOKEN

    from PIL import Image, ImageDraw
    from starlette.testclient import TestClient

    from exomem import embeddings
    from exomem import find as find_module
    from exomem import server, upload_tokens

    server.load_dotenv = lambda *a, **k: None  # don't let repo .env clobber the smoke vault
    print("building server (loads bge + CLIP; first time is slow)…")
    client = TestClient(server.build_server(require_auth=False).http_app())

    def up_token():
        return upload_tokens.mint(TOKEN, scope="upload")

    def post(name, data_bytes, mime, scope, cat, text=None):
        form = {"scope": scope, "category": cat}
        if text is not None:
            form["text"] = text
        r = client.post(
            "/upload",
            files={"file": (name, data_bytes, mime)},
            data=form,
            headers={"Authorization": f"Bearer {up_token()}"},
        )
        assert r.status_code == 201, f"{name}: {r.status_code} {r.text}"
        return r.json()

    # ---- 1. image OCR ----
    img = Image.new("RGB", (640, 120), "white")
    ImageDraw.Draw(img).text((10, 45), "EVICTION NOTICE 14 days Whitechapel flat", fill="black")
    b = io.BytesIO(); img.save(b, "PNG")
    post("notice.png", b.getvalue(), "image/png", "Smoke", "01")
    sidecar = vault / "Knowledge Base/Evidence/Smoke/01/notice.png.md"
    _wait(lambda: sidecar.exists() and "extracted_by: pending" not in sidecar.read_text("utf-8"),
          what="OCR of notice.png")
    body = sidecar.read_text("utf-8")
    assert "whitechapel" in body.lower() or "eviction" in body.lower(), f"OCR text missing:\n{body[:400]}"
    find_module.clear_cache()
    hits = find_module.find(vault, query="eviction whitechapel", mode="keyword")
    assert any("notice.png.md" in h.path for h in hits), "find did not surface the OCR'd image"
    print("  [1] image OCR -> searchable by its text          PASS")

    # ---- 2. CLIP textless image ----
    red = io.BytesIO(); Image.new("RGB", (320, 320), "red").save(red, "PNG")
    post("red.png", red.getvalue(), "image/png", "Smoke", "02")
    red_rel = "Knowledge Base/Evidence/Smoke/02/red.png"
    _wait(lambda: embeddings.ClipIndex(vault).has(red_rel), what="CLIP index of red.png")
    find_module.clear_cache()
    hits = find_module.find(vault, query="a solid red square", mode="hybrid")
    red_hit = [h for h in hits if "red.png.md" in h.path]
    assert red_hit, "CLIP did not surface the textless red image"
    assert red_hit[0].as_dict().get("media_type") == "image"
    print("  [2] textless image -> found by CLIP visual match  PASS")

    # ---- 3. PDF text ----
    import fitz
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((72, 72), "MEDICAL REPORT cardiology consult Dr Avery 2026")
    pdf_bytes = doc.tobytes(); doc.close()
    post("report.pdf", pdf_bytes, "application/pdf", "Smoke", "03")
    pdf_sidecar = vault / "Knowledge Base/Evidence/Smoke/03/report.pdf.md"
    _wait(lambda: pdf_sidecar.exists() and "extracted_by: pending" not in pdf_sidecar.read_text("utf-8"),
          what="PDF extraction")
    assert "cardiology" in pdf_sidecar.read_text("utf-8").lower(), "PDF text not extracted"
    find_module.clear_cache()
    hits = find_module.find(vault, query="cardiology consult", mode="keyword")
    assert any("report.pdf.md" in h.path for h in hits), "find did not surface the PDF"
    print("  [3] PDF text -> extracted (PyMuPDF) & searchable   PASS")

    # ---- 4. /download reverse channel (scoped token) ----
    dtok = upload_tokens.mint(TOKEN, scope="download")
    r = client.get("/download", params={"path": red_rel}, headers={"Authorization": f"Bearer {dtok}"})
    assert r.status_code == 200 and r.content == red.getvalue(), "download byte mismatch"
    # scope isolation: an upload-scoped token must NOT download
    r2 = client.get("/download", params={"path": red_rel}, headers={"Authorization": f"Bearer {up_token()}"})
    assert r2.status_code == 401, f"upload token wrongly accepted on /download: {r2.status_code}"
    print("  [4] /download original back + scope isolation     PASS")

    print("\nSMOKE: PASS — full arc works end-to-end on this build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
