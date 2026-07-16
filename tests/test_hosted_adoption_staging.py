"""Lane D — hosted adoption staging intake.

A staged upload rides the SAME verified transfer grant as a normal v2 upload but
lands as RAW bytes under a vault-relative staging tree OUTSIDE ``Knowledge
Base/`` (``_Staging/adoption/<run_id>/…``) so the adoption engine treats it as
legacy input to scan. ZIP archives are expanded cell-side with zip-slip,
entry-count, and size guards; a rejected archive leaves nothing behind.

The auth/fixture patterns mirror ``tests/test_hosted_transfer_v2.py`` (the
canonical hosted transfer test) so the staging branch is proven to reuse the
exact grant flow, admission, and temp-root discipline.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from exomem import adopt as adopt_module
from exomem import hosted_transfer, hosted_transfer_routes
from exomem import overview as overview_module
from exomem.hosted_runtime import (
    HostedCellConfig,
    HostedCellLifecycle,
    HostedResourceLimits,
)
from exomem.server_hosted import register_hosted_routes

ORIGIN = "https://substratesystems.io"
TRANSFER_HOST = "transfer.substratesystems.io"
KID = "credential-7"
CREDENTIAL = "transfer-signing-credential-with-at-least-thirty-two-bytes"
PRINCIPAL = base64.urlsafe_b64encode(hashlib.sha256(b"principal").digest()).rstrip(b"=").decode()
JTI = "11111111-1111-4111-8111-111111111111"
NOW = int(time.time())
RUN_ID = "run-2026-07-14-alpha"
STAGING_ROOT = "_Staging/adoption"


class Replay(RuntimeError):
    code = "HOSTED_JTI_REPLAY"


class FakeSecurityAuthority:
    def __init__(self) -> None:
        self.accepted = {KID: CREDENTIAL}
        self.consumed: set[str] = set()
        self.consume_calls: list[dict[str, Any]] = []

    def verify_transfer_signature(
        self, kid: str, ascii_payload: bytes, signature: bytes
    ) -> bool:
        credential = self.accepted.get(kid)
        if credential is None:
            return False
        expected = hmac.new(
            credential.encode("utf-8"), ascii_payload, hashlib.sha256
        ).digest()
        return hmac.compare_digest(signature, expected)

    def consume_transfer_jti(self, **values: Any) -> None:
        self.consume_calls.append(values)
        jti = str(values["jti"])
        if jti in self.consumed:
            raise Replay
        self.consumed.add(jti)


def _staging_metadata(
    data: bytes,
    *,
    run_id: str = RUN_ID,
    filename: str = "note.txt",
    content_type: str = "text/plain",
    description: str | None = None,
    scope: str = "adoption-staging",
) -> dict[str, Any]:
    return {
        "category": run_id,
        "content_type": content_type,
        "description": description,
        "filename": filename,
        "scope": scope,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _grant(
    data: bytes,
    *,
    run_id: str = RUN_ID,
    filename: str = "note.txt",
    content_type: str = "text/plain",
    description: str | None = None,
    scope: str = "adoption-staging",
    max_bytes: int = 1024 * 1024,
    jti: str = JTI,
) -> str:
    metadata = _staging_metadata(
        data,
        run_id=run_id,
        filename=filename,
        content_type=content_type,
        description=description,
        scope=scope,
    )
    target = {
        "kind": "upload-v1",
        "metadata": metadata,
        "metadata_sha256": hashlib.sha256(
            hosted_transfer.canonical_json(metadata)
        ).hexdigest(),
    }
    return hosted_transfer.mint_transfer_grant_v2(
        signing_credential=CREDENTIAL,
        kid=KID,
        origin=ORIGIN,
        operation="upload",
        cell_id="cell-alpha",
        principal_scope=PRINCIPAL,
        jti=jti,
        max_bytes=max_bytes,
        target=target,
        issued_at=NOW,
        not_before=NOW,
        expires_at=NOW + 300,
    )


def _config(tmp_path: Path) -> HostedCellConfig:
    return HostedCellConfig(
        cell_id="cell-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential="private-service-credential-with-thirty-two-bytes",
        enforce_transfer_v1_compatibility=False,
        transfer_browser_origin=ORIGIN,
        transfer_host=TRANSFER_HOST,
        resource_limits=HostedResourceLimits(
            storage_bytes=1024 * 1024,
            upload_bytes=90 * 1024 * 1024,
            worker_count=0,
        ),
    )


def _app(
    tmp_path: Path,
    security: FakeSecurityAuthority,
    *,
    preserve_stream_func: Any | None = None,
) -> tuple[Any, HostedCellConfig, HostedCellLifecycle]:
    from exomem.init import init_vault
    from exomem.schema import load_source_schema

    config = _config(tmp_path)
    init_vault(config.vault_root)
    config.state_root.mkdir(parents=True, exist_ok=True)
    config.log_root.mkdir(parents=True, exist_ok=True)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    app = FastMCP("adoption-staging")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=load_source_schema(config.vault_root),
        transfer_security_authority=security,
        preserve_stream_func=preserve_stream_func,
        mutation_guard_factory=lambda _vault: contextlib.nullcontext(),
        runtime_temp_authority=None,
    )
    return app.http_app(), config, lifecycle


async def _put(app: Any, grant: str, data: bytes, content_type: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"https://{TRANSFER_HOST}"
    ) as client:
        return await client.request(
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": content_type,
                hosted_transfer.TRANSFER_GRANT_HEADER: grant,
            },
            content=data,
        )


def _build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _kb_files(config: HostedCellConfig) -> set[str]:
    kb_root = config.vault_root / "Knowledge Base"
    if not kb_root.exists():
        return set()
    return {
        str(path.relative_to(kb_root))
        for path in sorted(kb_root.rglob("*"))
        if path.is_file()
    }


def _temp_entries(config: HostedCellConfig) -> list[str]:
    temp_root = config.state_root / "tmp" / "transfers-v2"
    return [entry.name for entry in temp_root.iterdir()]


def _run_dir(config: HostedCellConfig, run_id: str = RUN_ID) -> Path:
    return config.vault_root / "_Staging" / "adoption" / run_id


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


def test_staged_single_file_lands_at_exact_path_and_is_scannable(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    data = b"legacy note body that predates governance"
    kb_before = _kb_files(config)

    response = asyncio.run(_put(app, _grant(data), data, "text/plain"))

    assert response.status_code == 201, response.text
    landed = _run_dir(config) / "note.txt"
    assert landed.is_file()
    assert landed.read_bytes() == data
    # Raw staging lands OUTSIDE Knowledge Base/ — the engine treats it as legacy input.
    assert _kb_files(config) == kb_before
    assert "Knowledge Base" not in landed.relative_to(config.vault_root).parts
    # Proof the staged tree is scannable via the existing read-only leaf functions.
    scan = overview_module.overview(config.vault_root, path=f"{STAGING_ROOT}/{RUN_ID}")
    assert scan["totals"]["files"] == 1
    report = adopt_module.adopt(
        config.vault_root, path=f"{STAGING_ROOT}/{RUN_ID}", mode="scan-only"
    )
    assert report["mode"] == "scan-only"
    assert report["summary"]["totals"]["files"] == 1
    assert _temp_entries(config) == []
    assert security.consumed == {JTI}


def test_staged_file_honors_optional_relative_subdir(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    data = b"nested legacy note"

    response = asyncio.run(
        _put(app, _grant(data, description="incoming/2026"), data, "text/plain")
    )

    assert response.status_code == 201, response.text
    landed = _run_dir(config) / "incoming" / "2026" / "note.txt"
    assert landed.is_file()
    assert landed.read_bytes() == data


def test_staged_zip_expands_into_run_dir_and_is_scannable(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    archive = _build_zip(
        [
            ("a.txt", b"alpha-body"),
            ("sub/b.txt", b"bravo-body"),
        ]
    )

    response = asyncio.run(
        _put(app, _grant(archive, filename="bundle.zip", content_type="application/zip"), archive, "application/zip")
    )

    assert response.status_code == 201, response.text
    assert (_run_dir(config) / "a.txt").read_bytes() == b"alpha-body"
    assert (_run_dir(config) / "sub" / "b.txt").read_bytes() == b"bravo-body"
    # The ZIP itself is never persisted — only its expanded members.
    assert not (_run_dir(config) / "bundle.zip").exists()
    scan = overview_module.overview(config.vault_root, path=f"{STAGING_ROOT}/{RUN_ID}")
    assert scan["totals"]["files"] == 2
    assert _kb_files(config) == _kb_files(config)  # KB untouched (nothing added)
    assert (config.vault_root / "Knowledge Base").exists()
    assert not any(
        "_Staging" in str(path) for path in (config.vault_root / "Knowledge Base").rglob("*")
    )
    assert _temp_entries(config) == []


# --------------------------------------------------------------------------
# Rejections — malicious / oversized archives leave NO partial extraction
# --------------------------------------------------------------------------


def test_zip_slip_entry_is_rejected_with_no_partial(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    archive = _build_zip(
        [
            ("ok.txt", b"benign"),
            ("../evil.txt", b"pwned"),
        ]
    )

    response = asyncio.run(
        _put(app, _grant(archive, filename="bundle.zip", content_type="application/zip"), archive, "application/zip")
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "TRANSFER_REQUEST_INVALID"
    # No partial extraction: the run dir was never created, no escaped file exists.
    assert not _run_dir(config).exists()
    assert not (config.vault_root / "evil.txt").exists()
    assert not (config.vault_root / "_Staging" / "adoption" / "evil.txt").exists()
    assert _temp_entries(config) == []


def test_zip_symlink_entry_is_rejected(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("link")
        # 0o120777 << 16 marks a POSIX symlink in the external attributes.
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, b"/etc/passwd")
    payload = buffer.getvalue()

    response = asyncio.run(
        _put(app, _grant(payload, filename="bundle.zip", content_type="application/zip"), payload, "application/zip")
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "TRANSFER_REQUEST_INVALID"
    assert not _run_dir(config).exists()
    assert _temp_entries(config) == []


def test_zip_entry_count_cap_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted_transfer_routes, "_ADOPTION_STAGING_MAX_ENTRIES", 1)
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    archive = _build_zip([("a.txt", b"a"), ("b.txt", b"b")])

    response = asyncio.run(
        _put(app, _grant(archive, filename="bundle.zip", content_type="application/zip"), archive, "application/zip")
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "TRANSFER_TOO_LARGE"
    assert not _run_dir(config).exists()
    assert _temp_entries(config) == []


def test_zip_per_entry_size_cap_is_rejected(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    # A zip bomb: ~8 KiB of zeros compresses tiny, but expands past the per-entry
    # grant bound (max_bytes=1024), so the compressed upload rides the grant yet
    # the expanded entry is refused.
    archive = _build_zip([("big.bin", b"\x00" * 8192)])
    assert len(archive) <= 1024  # the compressed upload itself fits the grant

    response = asyncio.run(
        _put(
            app,
            _grant(
                archive,
                filename="bundle.zip",
                content_type="application/zip",
                max_bytes=1024,
            ),
            archive,
            "application/zip",
        )
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "TRANSFER_TOO_LARGE"
    assert not _run_dir(config).exists()
    assert _temp_entries(config) == []


def test_zip_total_uncompressed_size_cap_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted_transfer_routes, "_ADOPTION_STAGING_MAX_TOTAL_BYTES", 8)
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    archive = _build_zip([("a.txt", b"aaaaaa"), ("b.txt", b"bbbbbb")])

    response = asyncio.run(
        _put(app, _grant(archive, filename="bundle.zip", content_type="application/zip"), archive, "application/zip")
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "TRANSFER_TOO_LARGE"
    assert not _run_dir(config).exists()
    assert _temp_entries(config) == []


def test_relative_subdir_escape_is_rejected_and_never_touches_kb(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    kb_before = _kb_files(config)
    data = b"attempted escape"

    response = asyncio.run(
        _put(
            app,
            _grant(data, description="../../Knowledge Base/evil"),
            data,
            "text/plain",
        )
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "TRANSFER_REQUEST_INVALID"
    assert _kb_files(config) == kb_before
    assert not (config.vault_root / "Knowledge Base" / "evil").exists()
    assert not _run_dir(config).exists()


def test_invalid_run_id_is_rejected(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    data = b"body"

    response = asyncio.run(
        _put(app, _grant(data, run_id="bad/../run"), data, "text/plain")
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "TRANSFER_REQUEST_INVALID"
    assert not (config.vault_root / "_Staging").exists()


# --------------------------------------------------------------------------
# Regression — default-scope uploads keep the preserve-style landing
# --------------------------------------------------------------------------


def test_default_scope_upload_still_uses_preserve_and_never_stages(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    calls: list[dict[str, Any]] = []

    def preserve(*_args: Any, **kwargs: Any) -> object:
        calls.append(kwargs)
        assert kwargs["stream"].read() == b"alpha"
        return object()

    app, config, _lifecycle = _app(tmp_path, security, preserve_stream_func=preserve)
    data = b"alpha"

    response = asyncio.run(
        _put(app, _grant(data, filename="alpha.txt", scope="research"), data, "text/plain")
    )

    assert response.status_code == 201, response.text
    assert len(calls) == 1
    assert calls[0]["scope"] == "research"
    assert not (config.vault_root / "_Staging").exists()


def test_zip_aggregate_expansion_is_capped_by_the_grant(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(tmp_path, security)
    # Each entry fits the 1 KiB grant on its own, but the archive expands to
    # 8x the signed per-upload allowance: the AGGREGATE must ride the grant,
    # not only the global constant.
    archive = _build_zip([(f"part-{i}.bin", b"\x00" * 1024) for i in range(8)])
    assert len(archive) <= 1024  # the compressed upload itself fits the grant

    response = asyncio.run(
        _put(
            app,
            _grant(
                archive,
                filename="bundle.zip",
                content_type="application/zip",
                max_bytes=1024,
            ),
            archive,
            "application/zip",
        )
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "TRANSFER_TOO_LARGE"
    assert not _run_dir(config).exists()
    assert _temp_entries(config) == []
