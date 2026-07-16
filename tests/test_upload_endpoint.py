"""/upload endpoint — out-of-band binary upload straight into Evidence/.

Drives the real FastMCP ASGI app through HTTPX's in-process ASGI transport
(no pytest-asyncio dependency). `load_dotenv` is neutralized so the repo
`.env` can't clobber the per-test fixture vault.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from exomem import server


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))


class _ASGIClient:
    """Synchronous test facade without Starlette's Python 3.14 portal deadlock."""

    def __init__(self, app) -> None:
        self.app = app

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> _ASGIClient:
    from exomem import server_transfer

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)

    # Python 3.14 currently deadlocks both Starlette's TestClient portal and its
    # AnyIO worker-return path. ASGITransport plus an inline threadpool seam keeps
    # the route contract exact; concurrent tests still run requests in real threads.
    async def inline_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline_threadpool)
    # Hermeticity: the /upload route is gated by these env vars, and a developer's
    # ambient shell may already have them set (the author's deployment box does).
    # Clear them first so a test that doesn't pass one sees the "disabled" default
    # instead of silently inheriting the ambient value (caught a false 401 vs 503).
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_UPLOAD_MAX_BYTES"):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return _ASGIClient(mcp.http_app())


def test_upload_requires_auth(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import writer_lease

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("unauthorized upload reached mutation coordination"),
    )
    r = client.post(
        "/upload",
        files={"file": ("shot.png", b"\x89PNGdata", "image/png")},
        data={"scope": "Yolo", "category": "01 - Check-in"},
    )
    assert r.status_code == 401


def test_upload_happy_path_lands_in_evidence(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("shot.png", b"\x89PNGrealbytes", "image/png")},
        data={"scope": "Yolo", "category": "01 - Check-in"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "Evidence/Yolo/01 - Check-in/shot.png" in body["path"]
    assert body["stored_path"] == body["path"]
    assert body["size"] == len(b"\x89PNGrealbytes")
    assert body["hash"] == hashlib.sha256(b"\x89PNGrealbytes").hexdigest()
    assert body["hash_algorithm"] == "sha256"
    assert body["media_id"] == f"sha256:{body['hash']}"
    assert body["content_type"] == "image/png"
    written = vault / body["path"]
    assert written.read_bytes() == b"\x89PNGrealbytes"


def test_upload_supported_media_routes_through_canonical_reconciliation(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_processing, server_runtime

    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION")
    calls: list[tuple[Path, Path, bool]] = []

    class Worker:
        def enqueue(self, **_kwargs) -> None:
            pytest.fail("upload bypassed canonical media reconciliation")

    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _vault: Worker())
    monkeypatch.setattr(
        media_processing,
        "reconcile_media",
        lambda root, path, *, explicit=True: calls.append((root, path, explicit)),
    )
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")

    response = client.post(
        "/upload",
        files={"file": ("field-note.m4a", b"audio bytes", "audio/mp4")},
        data={"scope": "Interviews", "category": "Raw"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    binary = vault / body["path"]
    assert calls == [(vault, binary, False)]
    assert body["stored_path"] == body["path"]
    assert body["sidecar_path"].endswith("field-note.m4a.md")
    assert body["size"] == len(b"audio bytes")
    assert body["hash"] == hashlib.sha256(b"audio bytes").hexdigest()
    assert body["media_id"] == f"sha256:{body['hash']}"
    assert body["content_type"] == "audio/mp4"


def test_upload_reconciles_when_media_worker_is_unavailable(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_processing, server_runtime

    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _vault: None)
    calls: list[tuple[Path, Path, bool]] = []
    monkeypatch.setattr(
        media_processing,
        "reconcile_media",
        lambda root, path, *, explicit=True: calls.append((root, path, explicit)),
    )
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")

    response = client.post(
        "/upload",
        files={"file": ("offline.mp3", b"offline audio", "audio/mpeg")},
        data={"scope": "Interviews", "category": "Raw"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert calls == [(vault, vault / body["path"], False)]
    assert body["sidecar_path"].endswith("offline.mp3.md")
    assert "extracted_by: pending" in (vault / body["sidecar_path"]).read_text(
        encoding="utf-8"
    )


def test_upload_reconciliation_does_not_block_async_event_loop(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_processing, server_runtime, server_transfer

    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION")
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _vault: object())
    entered = threading.Event()
    release = threading.Event()
    reconcile_threads: list[int] = []

    def blocking_reconcile(root, path, *, explicit=True) -> None:
        assert root == vault
        assert path.name == "blocking.m4a"
        reconcile_threads.append(threading.get_ident())
        entered.set()
        release.wait(0.5)

    monkeypatch.setattr(media_processing, "reconcile_media", blocking_reconcile)
    app = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret").app

    async def real_threadpool(function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", real_threadpool)

    async def scenario() -> httpx.Response:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/upload",
                    files={"file": ("blocking.m4a", b"audio", "audio/mp4")},
                    data={"scope": "Interviews", "category": "Raw"},
                    headers={"Authorization": "Bearer sekret"},
                )
            )
            deadline = asyncio.get_running_loop().time() + 2.0
            while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert entered.is_set(), "reconciliation never started"
            assert not request.done(), "blocking reconciliation stalled the event loop"
            assert reconcile_threads == [reconcile_threads[0]]
            assert reconcile_threads[0] != loop_thread
            release.set()
            return await request

    response = asyncio.run(scenario())
    assert response.status_code == 201, response.text


def test_upload_parses_before_guard_and_preserves_inside_it(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import preserve, writer_lease

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    events: list[str] = []

    class RecordingManager:
        depth = 0

        def ensure_writer(self) -> None:
            events.append("legacy-ensure")

        @contextmanager
        def mutation_guard(self, guarded_vault):
            assert guarded_vault == vault
            events.append("guard-enter")
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
                events.append("guard-exit")

    manager = RecordingManager()
    original = preserve.preserve_stream

    def checked_preserve(*args, **kwargs):
        assert manager.depth == 1
        assert kwargs["scope"] == "S"
        assert kwargs["category"] == "C"
        events.append("preserve")
        return original(*args, **kwargs)

    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(preserve, "preserve_stream", checked_preserve)

    response = client.post(
        "/upload",
        files={"file": ("guarded.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 201, response.text
    assert events == ["guard-enter", "preserve", "guard-exit"]


def test_upload_media_reconciliation_uses_writer_authority(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_processing, writer_lease

    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, guarded_vault):
            nonlocal depth
            assert guarded_vault == vault
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(
        media_processing,
        "reconcile_media",
        lambda *_a, **_kw: depth == 1
        or pytest.fail("post-upload reconciliation escaped mutation guard"),
    )
    response = client.post(
        "/upload",
        files={"file": ("guarded.m4a", b"audio", "audio/mp4")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 201, response.text
    assert depth == 0


@pytest.mark.parametrize(
    ("code", "status"),
    [("MUTATION_BUSY", 409), ("MUTATION_LOCK_UNAVAILABLE", 503)],
)
def test_upload_maps_mutation_lock_errors(
    vault, monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    from exomem import writer_lease
    from exomem.cli_ops import OpError

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")

    class RejectingManager:
        def ensure_writer(self) -> None:
            return None

        @contextmanager
        def mutation_guard(self, _vault):
            raise OpError(code, "lock rejected the upload")
            yield

    monkeypatch.setattr(writer_lease, "get_manager", lambda: RejectingManager())
    response = client.post(
        "/upload",
        files={"file": (f"{code}.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_identical_concurrent_uploads_serialize_before_append_only_check(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import preserve

    first_client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    second_client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    original = preserve.preserve_stream
    state_lock = threading.Lock()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls = 0

    def overlapping_preserve(*args, **kwargs):
        nonlocal calls
        with state_lock:
            calls += 1
            call = calls
        if call == 1:
            first_entered.set()
            assert release_first.wait(3.0)
        else:
            second_entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(preserve, "preserve_stream", overlapping_preserve)
    request = {
        "files": {"file": ("same.bin", b"identical", "application/octet-stream")},
        "data": {"scope": "S", "category": "C"},
        "headers": {"Authorization": "Bearer sekret"},
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_client.post, "/upload", **request)
        assert first_entered.wait(2.0)
        second = pool.submit(second_client.post, "/upload", **request)
        try:
            assert not second_entered.wait(0.15), "second upload entered commit concurrently"
        finally:
            release_first.set()
        responses = [first.result(timeout=5.0), second.result(timeout=5.0)]

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "ARTIFACT_EXISTS"
    assert (vault / "Knowledge Base/Evidence/S/C/same.bin").read_bytes() == b"identical"


def test_spoofed_cf_access_header_is_not_trusted(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # A client-supplied Cf-Access-* header is spoofable and must NEVER authorize.
    # Regression for the spoofable-field auth-bypass finding.
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-authenticated-user-email": "attacker@evil.com"},
    )
    assert r.status_code == 401, r.text


def test_upload_disabled_ignores_spoofed_header(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # No token configured → off, and a spoofed CF header cannot re-enable it.
    client = _client(vault, monkeypatch)
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-authenticated-user-email": "attacker@evil.com"},
    )
    assert r.status_code == 503


def test_upload_rejects_oversize(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret", EXOMEM_UPLOAD_MAX_BYTES="16"
    )
    r = client.post(
        "/upload",
        files={"file": ("big.bin", b"x" * 64, "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 413, r.text


def test_upload_duplicate_is_conflict(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    headers = {"Authorization": "Bearer sekret"}
    first = client.post(
        "/upload",
        files={"file": ("dupe.bin", b"first", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers=headers,
    )
    assert first.status_code == 201
    second = client.post(
        "/upload",
        files={"file": ("dupe.bin", b"second", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers=headers,
    )
    assert second.status_code == 409, second.text


def test_minted_short_lived_token_authorizes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    minted = upload_tokens.mint("sekret")  # valid ~15 min
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"viaminted", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": f"Bearer {minted}"},
    )
    assert r.status_code == 201, r.text


def test_expired_minted_token_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    expired = upload_tokens.mint("sekret", ttl=-10)  # already past exp
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401, r.text


def test_cf_access_valid_jwt_authorizes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # No bearer token configured; a *verified* CF Access JWT is the credential.
    monkeypatch.setattr("exomem.cf_access.verify", lambda *a, **k: True)
    client = _client(
        vault,
        monkeypatch,
        EXOMEM_CF_ACCESS_TEAM_DOMAIN="t.cloudflareaccess.com",
        EXOMEM_CF_ACCESS_AUD="aud123",
    )
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"viacfaccess", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-jwt-assertion": "fake.jwt.token"},
    )
    assert r.status_code == 201, r.text


def test_cf_access_invalid_jwt_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("exomem.cf_access.verify", lambda *a, **k: False)
    client = _client(
        vault,
        monkeypatch,
        EXOMEM_CF_ACCESS_TEAM_DOMAIN="t.cloudflareaccess.com",
        EXOMEM_CF_ACCESS_AUD="aud123",
    )
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-jwt-assertion": "bad"},
    )
    assert r.status_code == 401, r.text


def test_upload_text_field_writes_searchable_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # The OCR companion over HTTP: the `text` form field becomes the embedded,
    # keyword-findable sidecar body so the binary is searchable by its content.
    from exomem import find as find_module

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("invoice.png", b"\x89PNGbytes", "image/png")},
        data={
            "scope": "Yolo",
            "category": "01 - Check-in",
            "text": "Invoice total 4200 EUR, vendor Acme Plumbing, dated 2026-05-20.",
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 201, r.text
    sidecar_rel = r.json()["sidecar_path"]
    assert sidecar_rel and sidecar_rel.endswith("invoice.png.md")
    sidecar = vault / sidecar_rel
    assert "Acme Plumbing" in sidecar.read_text(encoding="utf-8")
    find_module.clear_cache()
    hits = find_module.find(vault, query="Acme Plumbing", mode="keyword")
    assert any("invoice.png.md" in h.path for h in hits), [h.path for h in hits]


def test_upload_get_serves_prefilled_form(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.get("/upload?scope=Yolo&category=01%20-%20Check-in")
    assert r.status_code == 200
    assert "Add evidence" in r.text
    assert 'value="Yolo"' in r.text
    assert "name=text" in r.text  # searchable-text field present
