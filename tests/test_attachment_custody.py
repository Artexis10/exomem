"""Attachment custody: the original is preserved first, a transcription only beside it.

`preserve-attachment-originals`. A local client holds an attached file through
its local session and redeems the returned handle through the same file-handle
commands ChatGPT fills; a transcription is recorded only on its stored
original's page. All bytes, names and tokens here are synthetic.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

# The real-worker harness of the local-ingress threat tests, reused so the
# end-to-end case runs through the same manager doors production uses.
from test_local_ingress_worker import (  # noqa: F401 - fixtures are used by name
    ISSUER,
    OWNER_ID,
    UPLOAD_TOKEN,
    _real_doors,
    _RealWorker,
    managed,
    real_env,
    real_worker,
)

from exomem import commands, local_ingress
from exomem.auth_sessions import SessionIdentity
from exomem.vault import parse_frontmatter

TRANSCRIPTION = "Appointment card: follow-up visit, room 4, bring the referral letter."


def _png(width: int = 2, height: int = 2) -> bytes:
    """A small, valid PNG so the media pipeline classifies it as an image."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x7f\x20\x10" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


@contextmanager
def _local_session(session_id: str, client_id: str = "home") -> Iterator[None]:
    """Bind a verified local grant, as `LocalIngressMiddleware` does per request."""
    bearer = f"exo_s1.synthetic-{session_id}"
    grant = local_ingress.LocalGrant(
        bearer=bearer,
        access_token=local_ingress.LocalIngressAccessToken(
            token=bearer,
            client_id=client_id,
            scopes=list(local_ingress.LOCAL_SCOPES),
            expires_at=None,
        ),
        client_id=client_id,
        session_id=session_id,
    )
    token = local_ingress._GRANT.set(grant)
    try:
        yield
    finally:
        local_ingress._GRANT.reset(token)


@pytest.fixture
def lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from exomem import writer_lease

    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    return writer_lease


def _hold(vault: Path, payload: bytes, *, lane: str = "evidence", name: str = "card.png") -> dict:
    from exomem import held_uploads

    return held_uploads.hold(
        vault,
        io.BytesIO(payload),
        lane=lane,
        filename=name,
        content_type="image/png",
        max_bytes=1024 * 1024,
    )


def _preserve(lease, vault: Path, files: list[dict], key: str, **extra: Any) -> dict:
    return lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="Clinic",
        category="Appointments",
        files=files,
        idempotency_key=key,
        **extra,
    )


def _source_schema(vault: Path):
    from exomem import schema

    return schema.load_source_schema(vault)


def _evidence(vault: Path) -> Path:
    return vault / "Knowledge Base" / "Evidence" / "Clinic" / "Appointments"


def _vault_text_contains(vault: Path, needle: str) -> bool:
    for path in vault.rglob("*"):
        if path.is_file():
            try:
                if needle in path.read_text(encoding="utf-8"):
                    return True
            except (UnicodeDecodeError, OSError):
                continue
    return False


# ---- ChatGPT: both file-handle commands declare file parameters -------------------


def test_capture_source_declares_openai_file_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _command("capture_source")
    assert command.mcp_meta == {"openai/fileParams": ("files",)}
    assert _command("preserve_artifacts").mcp_meta == {"openai/fileParams": ("files",)}


def _schema_property_names(schema: Any, prefix: str = "") -> list[str]:
    names: list[str] = []
    if isinstance(schema, dict):
        for name, child in (schema.get("properties") or {}).items():
            names.append(prefix + name)
            names.extend(_schema_property_names(child, f"{prefix}{name}."))
        if "items" in schema:
            names.extend(_schema_property_names(schema["items"], f"{prefix}[]."))
        for key in ("anyOf", "oneOf", "allOf"):
            for child in schema.get(key) or []:
                names.extend(_schema_property_names(child, prefix))
        for name, child in (schema.get("$defs") or {}).items():
            names.extend(_schema_property_names(child, f"{prefix}${name}."))
    return names


#: Argument names that would hand the service a caller's filesystem location.
_LOCAL_PATH_ARGUMENT = re.compile(
    r"(^|[._\[\]])(local|file|attachment|upload|client|host)_?(path|dir|directory|location)$"
    r"|(^|[._\[\]])filepath$",
    re.IGNORECASE,
)
#: Every argument of a command that receives a file's bytes.
_FILE_INTAKE_COMMANDS = ("capture_source", "preserve_artifacts", "preserve_evidence", "transfer_artifact")


def test_no_tool_schema_takes_a_local_path_for_a_file() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "mcp_tool_schemas.json"
    schemas = json.loads(fixture.read_text(encoding="utf-8"))

    for name, tool in schemas.items():
        names = _schema_property_names(tool["inputSchema"])
        assert not [n for n in names if _LOCAL_PATH_ARGUMENT.search(n)], name
    for name in _FILE_INTAKE_COMMANDS:
        names = _schema_property_names(schemas[name]["inputSchema"])
        assert not [n for n in names if "path" in n.lower() or "dir" in n.lower()], name
    for name in ("capture_source", "preserve_artifacts"):
        files = schemas[name]["inputSchema"]["properties"]["files"]
        assert list(files["items"]["properties"]) == [
            "download_url",
            "file_id",
            "mime_type",
            "file_name",
        ]
    transcriptions = schemas["preserve_artifacts"]["inputSchema"]["properties"]["transcriptions"]
    assert sorted(transcriptions["items"]["properties"]) == ["file_id", "text"]


# ---- Local clients: held uploads -------------------------------------------------


def test_a_held_image_is_preserved_byte_for_byte_with_its_transcription(
    vault: Path, lease
) -> None:
    original = _png()
    with _local_session("session-home"):
        held = _hold(vault, original)
        handle = held["file"]
        assert set(handle) == {"download_url", "file_id", "mime_type", "file_name"}
        assert handle["download_url"].startswith("exomem-held:")
        secret = handle["download_url"].removeprefix("exomem-held:")
        assert secret not in handle["file_id"]
        assert held["hash"] == hashlib.sha256(original).hexdigest()
        assert held["size"] == len(original)
        assert held["lane"] == "evidence"
        # Held, not preserved: nothing is in the vault until a command commits it.
        assert not _evidence(vault).exists()

        result = _preserve(
            lease,
            vault,
            [handle],
            "held-first",
            transcriptions=[{"file_id": handle["file_id"], "text": TRANSCRIPTION}],
        )

    assert result["status"] == "committed", result
    row = result["files"][0]
    assert row["state"] == "stored", row
    stored = vault / row["stored_path"]
    assert stored.read_bytes() == original
    assert row["hash"] == hashlib.sha256(original).hexdigest()
    assert row["size"] == len(original)
    assert row["content_type"] == "image/png"

    # The transcription is on the original's own page, marked derived, and bound
    # to the exact bytes it was made from.
    assert row["transcription"]["state"] == "recorded"
    page = vault / row["transcription"]["page"]
    assert page == stored.with_name(stored.name + ".md")
    frontmatter, body, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert frontmatter["extracted_by"] == "client-transcription"
    assert frontmatter["transcription"] == {
        "origin": "client",
        "sha256": hashlib.sha256(original).hexdigest(),
        "size": len(original),
        "content_type": "image/png",
    }
    assert frontmatter["binary_sha256"] == hashlib.sha256(original).hexdigest()
    assert TRANSCRIPTION in body.split("## Extracted text", 1)[1]


def test_a_hold_is_withheld_from_every_other_principal_and_survives_them(
    vault: Path, lease
) -> None:
    with _local_session("session-home"):
        handle = _hold(vault, _png())["file"]

    with _local_session("session-laptop", client_id="laptop"):
        foreign = _preserve(lease, vault, [handle], "held-foreign")
    no_grant = _preserve(lease, vault, [handle], "held-no-grant")

    for result in (foreign, no_grant):
        row = result["files"][0]
        assert row["state"] == "failed"
        assert row["code"] == "HELD_UPLOAD_UNAVAILABLE"
    assert foreign["files"][0]["reason"] == no_grant["files"][0]["reason"]
    assert not _evidence(vault).exists()

    with _local_session("session-home"):
        owned = _preserve(lease, vault, [handle], "held-owner")
    assert owned["files"][0]["state"] == "stored"


def test_a_spent_expired_or_unknown_hold_gets_one_answer(
    vault: Path, lease, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import held_uploads

    with _local_session("session-home"):
        spent = _hold(vault, _png(2, 2))["file"]
        assert _preserve(lease, vault, [spent], "spent-1")["files"][0]["state"] == "stored"
        reused = _preserve(lease, vault, [dict(spent)], "spent-2")["files"][0]

        stale = _hold(vault, _png(3, 3), name="stale.png")["file"]
        now = held_uploads._now()
        monkeypatch.setattr(held_uploads, "_now", lambda: now + held_uploads.HOLD_TTL_SECONDS + 1)
        expired = _preserve(lease, vault, [stale], "stale")["files"][0]

        unknown = _preserve(
            lease,
            vault,
            [{"download_url": "exomem-held:" + "A" * 43, "file_id": "held-unknown"}],
            "unknown",
        )["files"][0]
        malformed = _preserve(
            lease,
            vault,
            [{"download_url": "exomem-held:../../etc", "file_id": "held-malformed"}],
            "malformed",
        )["files"][0]

    rows = (reused, expired, unknown, malformed)
    assert {row["code"] for row in rows} == {"HELD_UPLOAD_UNAVAILABLE"}
    assert len({row["reason"] for row in rows}) == 1
    assert not (_evidence(vault) / "stale.png").exists()


def test_the_wrong_lane_is_named_and_leaves_the_hold_redeemable(vault: Path, lease) -> None:
    with _local_session("session-home"):
        handle = _hold(vault, _png())["file"]
        wrong = lease.invoke_command(
            _command("capture_source"),
            vault,
            _source_schema(vault),
            title="Appointment card",
            files=[handle],
            idempotency_key="wrong-lane",
        )
        right = _preserve(lease, vault, [handle], "right-lane")

    assert wrong["files"][0]["code"] == "HELD_UPLOAD_LANE"
    assert right["files"][0]["state"] == "stored"


def test_a_source_hold_is_captured_losslessly_by_capture_source(vault: Path, lease) -> None:
    original = b"%PDF-1.4 synthetic field manual\n"
    with _local_session("session-home"):
        handle = _hold(vault, original, lane="source", name="manual.pdf")["file"]
        result = lease.invoke_command(
            _command("capture_source"),
            vault,
            _source_schema(vault),
            title="Field manual",
            files=[handle],
            idempotency_key="source-hold",
        )

    row = result["files"][0]
    assert row["outcome"] == "stored", row
    assert "/Sources/" in f"/{row['stored_path']}"
    assert (vault / row["stored_path"]).read_bytes() == original


def test_held_bytes_that_changed_are_not_committed(vault: Path, lease) -> None:
    from exomem import held_uploads, state_paths

    with _local_session("session-home"):
        handle = _hold(vault, _png())["file"]
        store = state_paths.vault_state_dir(vault) / held_uploads.STORE_DIRNAME
        [payload] = list(store.glob("*.bin"))
        payload.write_bytes(b"not the bytes that were held")
        result = _preserve(lease, vault, [handle], "tampered")

    row = result["files"][0]
    assert row["state"] == "failed"
    assert row["code"] == "HELD_UPLOAD_CHANGED"
    assert not _evidence(vault).exists()


def test_a_byte_budget_refusal_does_not_spend_the_hold(vault: Path) -> None:
    from exomem import held_uploads

    def over_budget(size: int) -> None:
        raise RuntimeError(f"{size} bytes is over this batch's budget")

    with _local_session("session-home"):
        handle = _hold(vault, _png())["file"]
        with pytest.raises(RuntimeError):
            held_uploads.redeem(vault, handle["download_url"], lane="evidence", admit=over_budget)
        redeemed = held_uploads.redeem(vault, handle["download_url"], lane="evidence")

    assert redeemed.path.read_bytes() == _png()
    redeemed.path.unlink()


def test_holding_needs_a_local_grant(vault: Path) -> None:
    from exomem import held_uploads

    with pytest.raises(held_uploads.HeldUploadError) as refused:
        _hold(vault, _png())
    assert refused.value.code == "HELD_UPLOAD_LOCAL_ONLY"


# ---- Order of operations: a transcription only with its original -----------------


def test_a_transcription_naming_no_file_refuses_before_anything_is_held_or_staged(
    vault: Path, lease
) -> None:
    from exomem.cli_ops import OpError

    with _local_session("session-home"):
        handle = _hold(vault, _png())["file"]
        with pytest.raises(OpError) as refused:
            commands.op_preserve_artifacts(
                vault,
                scope="Clinic",
                category="Appointments",
                files=[handle],
                transcriptions=[{"file_id": "not-supplied", "text": TRANSCRIPTION}],
            )
        # The hold was not consumed by the refused call.
        kept = _preserve(lease, vault, [handle], "after-refusal")

    assert refused.value.code == "INVALID_PRESERVE"
    assert refused.value.details["field"] == "transcriptions"
    assert kept["files"][0]["state"] == "stored"
    assert not _vault_text_contains(vault, TRANSCRIPTION)


def test_a_transcription_is_written_nowhere_when_its_original_fails(vault: Path, lease) -> None:
    with _local_session("session-laptop", client_id="laptop"):
        result = _preserve(
            lease,
            vault,
            [{"download_url": "exomem-held:" + "B" * 43, "file_id": "held-missing"}],
            "failed-original",
            transcriptions=[{"file_id": "held-missing", "text": TRANSCRIPTION}],
        )

    row = result["files"][0]
    assert row["state"] == "failed"
    assert "transcription" not in row
    assert not _vault_text_contains(vault, TRANSCRIPTION)


def test_an_already_stored_original_keeps_its_page_and_says_so(vault: Path, lease) -> None:
    original = _png()
    with _local_session("session-home"):
        first = _preserve(lease, vault, [_hold(vault, original)["file"]], "dup-first")
        page = vault / first["files"][0]["stored_path"]
        page = page.with_name(page.name + ".md")
        before = page.read_bytes()

        again = _hold(vault, original, name="card-again.png")["file"]
        duplicate = _preserve(
            lease,
            vault,
            [again],
            "dup-second",
            transcriptions=[{"file_id": again["file_id"], "text": TRANSCRIPTION}],
        )

    row = duplicate["files"][0]
    assert row["state"] == "already_stored"
    assert row["transcription"]["state"] == "not_recorded"
    assert page.read_bytes() == before
    assert not _vault_text_contains(vault, TRANSCRIPTION)


# ---- End to end through the real local door ---------------------------------------


@pytest.mark.anyio
async def test_attach_hold_then_preserve_over_the_local_listener(
    real_worker: _RealWorker,  # noqa: F811 - the imported fixture, by name
    vault: Path,
) -> None:
    from exomem import server_auth

    original = _png(4, 4)
    identity = SessionIdentity(github_user_id=OWNER_ID, github_login="example-owner")
    other, _ = await server_auth.build_local_session_authority().issue(
        client_id="laptop", scopes=list(local_ingress.LOCAL_SCOPES), identity=identity
    )
    home = {"authorization": f"Bearer {real_worker.local_token}"}

    async with _real_doors(real_worker) as doors:
        held = await doors.local.post(
            "/upload",
            headers=home,
            files={"file": ("card.png", original, "image/png")},
            data={"hold": "1"},
        )
        public_hold = await doors.public.post(
            "/upload",
            headers={"authorization": f"Bearer {UPLOAD_TOKEN}"},
            files={"file": ("card.png", original, "image/png")},
            data={"hold": "1"},
        )
        assert held.status_code == 201, held.text
        handle = held.json()["file"]
        body = {
            "scope": "Clinic",
            "category": "Appointments",
            "files": [handle],
            "transcriptions": [{"file_id": handle["file_id"], "text": TRANSCRIPTION}],
        }
        foreign = await doors.local.post(
            "/api/preserve_artifacts",
            headers={"authorization": f"Bearer {other}", "idempotency-key": "e2e-foreign"},
            json=body,
        )
        owned = await doors.local.post(
            "/api/preserve_artifacts",
            headers={**home, "idempotency-key": "e2e-owner"},
            json=body,
        )

    assert public_hold.status_code == 400
    assert public_hold.json()["code"] == "HELD_UPLOAD_LOCAL_ONLY"
    assert foreign.status_code == 200, foreign.text
    assert foreign.json()["data"]["files"][0]["code"] == "HELD_UPLOAD_UNAVAILABLE"
    assert owned.status_code == 200, owned.text
    row = owned.json()["data"]["files"][0]
    assert row["state"] == "stored"
    assert (vault / row["stored_path"]).read_bytes() == original
    assert row["transcription"]["state"] == "recorded"
    assert ISSUER not in json.dumps(held.json())


# ---- exomem attach ----------------------------------------------------------------


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "home.token"
    path.write_text("exo_s1.synthetic-local-token\n", encoding="ascii")
    return path


@pytest.mark.parametrize("lane", ["evidence", "source"])
def test_attach_without_a_destination_holds_and_prints_the_handle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lane: str
) -> None:
    from exomem.__main__ import _attach_main

    source = tmp_path / "card.png"
    source.write_bytes(_png())
    seen: list[httpx.Request] = []
    handle = {
        "download_url": "exomem-held:" + "C" * 43,
        "file_id": "held-0123456789abcdef",
        "mime_type": "image/png",
        "file_name": "card.png",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(201, json={"state": "held", "lane": lane, "file": handle})

    argv = [str(source), "--token-file", str(_token_file(tmp_path)), "--port", "8764"]
    if lane != "evidence":
        argv += ["--lane", lane]
    assert _attach_main(argv, transport=httpx.MockTransport(handler)) == 0

    assert json.loads(capsys.readouterr().out)["file"] == handle
    body = seen[0].content
    assert b'name="hold"' in body
    assert re.search(rb'name="lane"\r\n\r\n' + lane.encode(), body)
    assert b'name="scope"' not in body
    assert str(tmp_path).encode() not in body


def test_attach_refuses_a_source_lane_for_a_direct_evidence_preserve(tmp_path: Path) -> None:
    from exomem.__main__ import _attach_main

    source = tmp_path / "card.png"
    source.write_bytes(_png())
    with pytest.raises(SystemExit):
        _attach_main(
            [
                str(source),
                "--scope",
                "Clinic",
                "--category",
                "Appointments",
                "--lane",
                "source",
                "--token-file",
                str(_token_file(tmp_path)),
                "--port",
                "8764",
            ]
        )
