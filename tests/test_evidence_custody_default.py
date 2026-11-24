"""A proof file reaches Evidence in one call, carries its caption, and never keeps a card number.

These replay the 24 November 2026 cases with invented data: a photo of a product's serial label
shared to register a warranty was transcribed but never kept, and a client with no shell had
no way to send the bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess

import httpx
import pytest
from fastmcp import FastMCP

from exomem import (
    auth_sessions,
    commands,
    held_uploads,
    server_auth,
    server_transfer,
    upload_handoff,
)
from exomem.governance import capture_redaction, principal

LABEL = b"\x89PNG\r\n\x1a\n invented serial label NW7-2611-0042"
NOTE = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"


@pytest.fixture
def upload_env(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://capture.example")
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "42")
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "github:42")
    monkeypatch.delenv("EXOMEM_UPLOAD_TOKEN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    authority = auth_sessions.SessionAuthority.local(
        directory=tmp_path / "sessions", signing_root="synthetic-private-signing-key",
        issuer="https://capture.example", audience="https://capture.example/mcp",
    )
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **_: authority)
    _bearer, record = asyncio.run(authority.issue(
        client_id="agent", scopes=["mcp"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example"),
    ))
    who = principal.RequestPrincipal(
        audience_id="owner", surface="mcp", issuer_family="mcp-oauth:synthetic", remote_owner=True,
        client_binding=principal.ClientBinding(authority.issuer, record.client_id),
        origin_session=principal.OriginSessionBinding(record.session_id, record.generation, record.audience),
    )

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline)
    app = FastMCP("evidence-custody-test")
    server_transfer.register_transfer_routes(app, vault_root=vault, media_worker=None)
    return vault, who, app.http_app()


def _send(app, token: str, data: bytes = LABEL, **fields):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://capture.example") as client:
            return await client.post(
                "/upload", headers={"Authorization": "Bearer " + token},
                files={"file": ("label.png", data, "image/png")},
                data={"hold": "1", "lane": "evidence", **fields},
            )
    return asyncio.run(send())


def _get(app, path: str):
    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://capture.example") as client:
            return await client.get(path)
    return asyncio.run(get())


def _held_handle(env, data: bytes = LABEL) -> dict:
    vault, who, app = env
    with principal.request_scope(who):
        ticket = upload_handoff.prepare_upload(vault, lane="evidence", expected_sha256=hashlib.sha256(data).hexdigest())
    reply = _send(app, ticket["token"], data)
    assert reply.status_code == 201, reply.text
    return reply.json()["file"]


def test_a_caption_on_file_evidence_lands_on_the_original_page_with_a_citation_text_can_carry(upload_env):
    # Failure caught: save_evidence refused a caption on files, so the label's meaning lived only in chat.
    vault, who, _app = upload_env
    handle = _held_handle(upload_env)
    with principal.request_scope(who):
        result = commands.op_preserve_artifacts(
            vault, scope="northwind-hp7", category="warranty", files=[handle],
            description="Box label of the HP-7 headphones; serial NW7-2611-0042",
        )
    row = result["files"][0]
    assert row["state"] == "stored"
    assert row["caption"]["state"] == "recorded"
    page = row["caption"]["page"]
    assert "serial NW7-2611-0042" in (vault / page).read_text(encoding="utf-8")

    # Failure caught: a citation that does not resolve to the kept file leaves the transcription uncited.
    with principal.request_scope(who):
        commands.op_observe_memory(
            vault, path=NOTE, category="warranty", content=f"The HP-7 serial is NW7-2611-0042 {row['cite']}",
        )
        inbound = commands.op_list_inbound_links(vault, page)
    assert NOTE in {link["path"] for link in inbound["inbound"]}


def test_an_upload_ticket_with_a_destination_saves_the_file_on_arrival_and_leaves_nothing_held(upload_env):
    # Failure caught: the third step (save_evidence with the held handle) was skipped and the hold expired.
    vault, who, app = upload_env
    with principal.request_scope(who):
        ticket = commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="warranty",
            description="Box label of the HP-7 headphones",
        )
    assert [step["step"] for step in ticket["next"]] == ["send", "page"]
    assert ticket["saves_to"] == {"scope": "northwind-hp7", "category": "warranty",
                                  "description": "Box label of the HP-7 headphones"}

    reply = _send(app, ticket["token"])
    assert reply.status_code == 201, reply.text
    receipt = reply.json()
    assert receipt["state"] == "stored"
    assert receipt["stored_path"].endswith("Evidence/northwind-hp7/warranty/label.png")
    assert receipt["hash"] == hashlib.sha256(LABEL).hexdigest()
    assert receipt["cite"].startswith("[[") and receipt["cite"].endswith("|label.png]]")
    assert (vault / receipt["stored_path"]).read_bytes() == LABEL
    with principal.request_scope(who):
        assert held_uploads._usage(held_uploads._store(vault), held_uploads._binding())[0] == 0

    # Failure caught: a spent ticket accepted a second file.
    assert _send(app, ticket["token"]).status_code == 401


def test_a_send_cannot_move_a_destination_ticket_somewhere_else(upload_env):
    # Failure caught: a form field redirecting the save to another case folder.
    vault, who, app = upload_env
    with principal.request_scope(who):
        ticket = commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="warranty",
        )
    reply = _send(app, ticket["token"], scope="another-case")
    assert reply.status_code == 400
    assert not (vault / "Knowledge Base/Evidence/another-case").exists()


def test_destination_tickets_are_evidence_only_and_need_both_segments(upload_env):
    # Failure caught: a Source or import ticket silently ignoring a destination.
    vault, who, _app = upload_env
    with principal.request_scope(who):
        with pytest.raises(ValueError, match="INVALID_MODE"):
            commands.op_transfer_artifact(vault, operation="upload", lane="source", scope="a", category="b")
        with pytest.raises(ValueError, match="INVALID_MODE"):
            commands.op_transfer_artifact(vault, operation="upload", lane="evidence", scope="a")
        with pytest.raises(ValueError, match="INVALID_PRESERVE"):
            commands.op_transfer_artifact(vault, operation="upload", lane="evidence", scope="a/b", category="c")


def test_the_send_page_is_one_static_page_that_never_holds_a_ticket(upload_env):
    # Failure caught: a ticket rendered into a page, its URL or its logs.
    vault, who, app = upload_env
    with principal.request_scope(who):
        ticket = commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="warranty",
        )
    page_url = next(step["url"] for step in ticket["next"] if step["step"] == "page")
    path, _, fragment = page_url.removeprefix("https://capture.example").partition("#")
    assert fragment == ticket["token"]

    page = _get(app, path)
    assert page.status_code == 200
    assert ticket["token"].split(":", 1)[1] not in page.text
    assert "script-src 'sha256-" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "no-store"


def test_a_card_number_never_reaches_a_kept_text_and_the_result_says_so(upload_env):
    # Failure caught: a card number stored in append-only Evidence through a caption or a text original.
    vault, who, _app = upload_env
    with principal.request_scope(who):
        result = commands.op_preserve_evidence(
            vault, scope="northwind-hp7", category="receipts", filename="order.txt",
            content="Paid with 4111 1111 1111 1111.\nBackup card: 5500000000000004, expires 09/29\n"
                    "Device IMEI 352099001761481\nEAN 4006381000093\nOrder 303-1234000-0000000\n"
                    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n",
            description="Order paid with card 4111-1111-1111-1111.",
        )
    kept = (vault / result["stored_path"]).read_text(encoding="utf-8")
    page = (vault / result["sidecar_path"]).read_text(encoding="utf-8")
    # Failure caught: a full stop or comma after the number hid it.
    assert "4111 1111 1111 1111" not in kept and "4111-1111-1111-1111" not in page
    assert "5500000000000004" not in kept
    assert "[card number removed, ends 1111]." in kept
    # Failure caught: wrong firing on the identifiers proof documents exist to keep.
    for identifier in ("352099001761481", "4006381000093", "303-1234000-0000000",
                       "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"):
        assert identifier in kept
    assert any("card number" in warning for warning in result["warnings"])


def test_a_card_number_in_a_transcription_or_a_ticket_caption_is_removed(upload_env):
    # Failure caught: the file paths kept a card number that the text path removes.
    vault, who, _app = upload_env
    handle = _held_handle(upload_env)
    with principal.request_scope(who):
        result = commands.op_preserve_artifacts(
            vault, scope="northwind-hp7", category="receipts", files=[handle],
            transcriptions=[{"file_id": handle["file_id"], "text": "Card 4111 1111 1111 1111"}],
        )
        ticket = commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="receipts",
            description="Receipt for card 4111111111111111",
        )
    page = (vault / result["files"][0]["transcription"]["page"]).read_text(encoding="utf-8")
    assert "4111 1111 1111 1111" not in page
    assert ticket["saves_to"]["description"] == "Receipt for card [card number removed, ends 1111]"
    assert ticket["warnings"]


def test_a_failed_save_on_arrival_keeps_the_hold_for_save_evidence(upload_env, monkeypatch):
    # Failure caught: a refused save on arrival losing the bytes the person already sent.
    from exomem.cli_ops import OpError

    vault, who, app = upload_env
    with principal.request_scope(who):
        ticket = commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="warranty",
        )
    real = commands.op_preserve_artifacts

    def refuse(*_args, **_kwargs):
        raise OpError("WRITE_BUSY", "the writer is busy", "retry")

    monkeypatch.setattr(commands, "op_preserve_artifacts", refuse)
    reply = _send(app, ticket["token"])
    assert reply.status_code == 201, reply.text
    body = reply.json()
    assert body["state"] == "held" and body["save"]["code"] == "WRITE_BUSY"

    monkeypatch.setattr(commands, "op_preserve_artifacts", real)
    with principal.request_scope(who):
        kept = commands.op_preserve_artifacts(vault, scope="northwind-hp7", category="warranty", files=[body["file"]])
    assert kept["files"][0]["state"] == "stored"


def test_a_labelled_key_in_a_caption_is_removed(upload_env):
    # Failure caught: a credential pasted into a caption kept on an append-only page.
    vault, who, _app = upload_env
    with principal.request_scope(who):
        result = commands.op_preserve_evidence(
            vault, scope="northwind-hp7", category="receipts", filename="note.txt", content="Order 2611.",
            description="Portal login api_key=Zm9vYmFyYmF6cXV4MTIzNDU2",
        )
    page = (vault / result["sidecar_path"]).read_text(encoding="utf-8")
    assert "Zm9vYmFyYmF6cXV4MTIzNDU2" not in page
    assert any("password or key" in warning for warning in result["warnings"])


def test_a_caption_with_an_archive_is_refused_before_anything_is_stored(upload_env):
    # Failure caught: a caption silently dropped because a members manifest has no original page.
    from exomem.cli_ops import OpError

    vault, who, _app = upload_env
    with principal.request_scope(who), pytest.raises(OpError, match="caption"):
        commands.op_preserve_artifacts(
            vault, scope="northwind-hp7", category="warranty", files=[], archive="members", description="x",
        )


def test_a_card_number_never_reaches_a_source_or_an_observation(upload_env, source_schema):
    # Failure caught: the other kept texts, where a transcription of a shared photo usually lands, kept the number.
    vault, who, _app = upload_env
    with principal.request_scope(who):
        captured = commands.op_capture_source(
            vault, source_schema, title="Northwind order", source_kind="receipt",
            content="Paid with card 4111 1111 1111 1111, order NW-2611.",
        )
        observed = commands.op_observe_memory(
            vault, path=NOTE, category="purchase", content="Paid with card 4111 1111 1111 1111.",
        )
    source_text = (vault / captured["source"]["path"]).read_text(encoding="utf-8")
    assert "4111 1111 1111 1111" not in source_text and "order NW-2611" in source_text
    assert any("card number" in warning for warning in captured["source"]["warnings"])
    assert "4111 1111 1111 1111" not in (vault / NOTE).read_text(encoding="utf-8")
    assert any("card number" in warning for warning in observed["warnings"])


def test_a_second_file_with_the_same_name_is_saved_on_arrival_under_its_digest(upload_env):
    # Failure caught: phones name many photos alike, and the second send was refused with nothing kept.
    vault, who, app = upload_env
    with principal.request_scope(who):
        first, second = (commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="warranty",
        ) for _ in range(2))
    assert _send(app, first["token"]).json()["state"] == "stored"
    other = b"\x89PNG\r\n\x1a\n another invented photo"
    receipt = _send(app, second["token"], other).json()
    assert receipt["state"] == "stored"
    assert receipt["stored_path"].endswith(f"label-{hashlib.sha256(other).hexdigest()[:12]}.png")


def test_a_file_name_that_would_break_a_wikilink_gets_no_cite_and_says_why(upload_env):
    # Failure caught: a cite for "Invoice #1234.pdf" that resolves nowhere.
    vault, who, app = upload_env
    with principal.request_scope(who):
        ticket = commands.op_transfer_artifact(
            vault, operation="upload", lane="evidence", scope="northwind-hp7", category="invoices",
        )

    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://capture.example") as client:
            return await client.post(
                "/upload", headers={"Authorization": "Bearer " + ticket["token"]},
                files={"file": ("Invoice #1234.pdf", b"%PDF-1.4 invented", "application/pdf")},
                data={"hold": "1", "lane": "evidence"},
            )
    receipt = asyncio.run(send()).json()
    assert receipt["state"] == "stored" and "cite" not in receipt
    assert any("No cite" in warning for warning in receipt["warnings"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to parse the page script")
def test_the_send_page_script_parses_as_javascript(tmp_path):
    # Failure caught: a Python escape turned a JS string into a syntax error, so the page did nothing.
    script = tmp_path / "send.js"
    script.write_text(server_transfer._SEND_PAGE_SCRIPT, encoding="utf-8")
    checked = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_a_card_followed_by_its_security_code_is_removed_with_the_code():
    # Failure caught: the 3-digit group after a card made a 19-digit candidate that failed the check, so both stayed.
    kept = capture_redaction.redact("Card 4111 1111 1111 1111 123, sent to 4111 1111 1111 1111 567 Elm St.")
    assert kept.removed == ("payment_card",)
    assert "4111" not in kept.text and " 123" not in kept.text
    assert kept.text.count("[card number removed, ends 1111]") == 2
    # Failure caught: an unbroken 19-digit reference whose first 16 digits happen to pass, removed as a card.
    assert capture_redaction.redact("Ref 5500000000000004999").removed == ()


def test_source_text_with_a_digest_and_a_card_is_refused_by_name(upload_env, source_schema):
    # Failure caught: a digest mismatch that blamed the bytes and sent the caller to a file path that keeps the card.
    vault, who, _app = upload_env
    text = "Paid with card 4111 1111 1111 1111, order NW-2611."
    with principal.request_scope(who), pytest.raises(ValueError, match="SECRET_IN_SOURCE_TEXT"):
        commands.op_capture_source(
            vault, source_schema, title="Northwind order", source_kind="receipt", content=text,
            expected_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
