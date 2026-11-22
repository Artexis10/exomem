"""A delegated Source keeps accepted bytes while governance cannot read its session."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest
from test_agent_upload_handoff import upload_env as upload_env
from test_authorization_session_lifecycle import NOW, _custody, _file_connection

from exomem import server_transfer, upload_handoff, upload_sessions
from exomem.governance import (
    authorization_custody,
    authorization_session_lifecycle,
    principal,
    raw_protection,
    store,
)


def _settled(vault, session, secret):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = upload_sessions.open_session(vault, session.id, secret).view()
        if current["state"] in (upload_sessions.FAILED, upload_sessions.RETRYABLE):
            return current
        time.sleep(0.02)
    pytest.fail("the Source commit did not settle")


def test_governance_read_outage_keeps_a_ticket_and_accepted_bytes_until_authority_is_known(
    upload_env, tmp_path, monkeypatch,
):
    vault, who, authority, bearer, _ = upload_env
    database = tmp_path / "governance.sqlite"
    connection, migration = _file_connection(database)
    custody = _custody(migration.activation_state_digest)
    with closing(connection):
        verified = authorization_session_lifecycle.open_session(
            connection, custody=custody, principal_id=who.audience_id,
            issuer_family=who.issuer_family, now=NOW, ttl_seconds=60,
        ).context
    who = replace(who, authorization_session_id=verified.session_id, verified_authorization_session=verified)
    monkeypatch.setattr(raw_protection, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(store, "open_authorization_session_connection", lambda _: sqlite3.connect(database, timeout=0))
    monkeypatch.setattr(authorization_custody, "load_authorization_custody", lambda *_, **__: custody)
    monkeypatch.setattr(upload_sessions, "REATTEMPT_INITIAL_SECONDS", 30.0)
    monkeypatch.setenv("EXOMEM_UPLOAD_MAX_BYTES", "8")
    original = b"Accepted Source original"
    sha256 = hashlib.sha256(original).hexdigest()
    with principal.request_scope(who):
        staged = upload_handoff.prepare_upload(vault, lane="source", expected_sha256=sha256, size_bytes=len(original))
        unspent = upload_handoff.prepare_upload(vault, lane="source", expected_sha256=sha256, size_bytes=len(original))

    def claim(ticket):
        return asyncio.run(upload_handoff.claim_upload(
            vault, ticket["token"], resumable=True, size_bytes=len(original), sha256=sha256,
        ))

    ticket = claim(staged)
    session, secret = upload_sessions.create(
        vault, binding=ticket["binding"], length=len(original), sha256=sha256,
        target={"filename": "recording.bin", "lane": "source", "raw_protection": False},
        max_bytes=len(original), source_handoff=ticket["source_handoff"],
    )
    patch = upload_sessions.Patch(session, 0, len(original))
    patch.write(original)
    assert patch.close() == (len(original), True)
    committer = partial(server_transfer.commit_session, vault)

    with closing(sqlite3.connect(database)) as exclusive:
        exclusive.execute("BEGIN EXCLUSIVE")
        upload_sessions.start_commit(session, committer)
        parked = _settled(vault, session, secret)
        assert parked["state"] == upload_sessions.RETRYABLE
        assert parked["code"] == "UPLOAD_ORIGIN_BUSY"
        assert session.part.read_bytes() == original
        with pytest.raises(upload_handoff.SourceOriginBusy, match="UPLOAD_ORIGIN_BUSY"):
            claim(unspent)

    assert claim(unspent)["source_handoff"]["binding"] == ticket["binding"]
    assert asyncio.run(authority.validate(bearer)) is not None
    with closing(sqlite3.connect(database)) as reopened:
        authorization_session_lifecycle.close_verified_session(
            reopened, custody=custody, context=verified, now=NOW,
        )
    with pytest.raises(ValueError, match="UPLOAD_HANDOFF_UNAVAILABLE"):
        upload_handoff.source_principal(vault, session.record)
    upload_sessions.start_commit(upload_sessions.open_session(vault, session.id, secret), committer)
    deadline = time.monotonic() + 10
    while session.part.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    refused = _settled(vault, session, secret)
    assert refused["state"] == upload_sessions.FAILED
    assert refused["code"] == "UPLOAD_HANDOFF_UNAVAILABLE"
    assert not session.part.exists()
