from __future__ import annotations

import json

import pytest
from test_vocabulary_authority import (
    NOW,
    _activate,
    _approve_request,
    _operation,
    _principal,
    _store,
    install_unit_session_boundary,
)

from exomem.vocabulary_authority import (
    CanonicalOperation,
    VocabularyAuthorityDenied,
    VocabularyAuthorityUnavailable,
)
from exomem.vocabulary_effects import CanonicalWriteImage, _result


@pytest.fixture
def pending_store(tmp_path, monkeypatch):
    install_unit_session_boundary(monkeypatch)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    _activate(authority, _principal())
    return authority


def complete_operation():
    base = _operation()
    images = (CanonicalWriteImage(base.effects[0].path, None, b"# Example\n\nA useful definition.\n"),)
    operation = CanonicalOperation.from_effects(
        operation_id=base.operation_id,
        command_digest=base.command_digest,
        effects=base.effects,
        image_digest=_result("reviewed", base.effects, (), images).digest,
        registry_digests=dict(base.registry_digests),
        target_digests=dict(base.target_digests),
    )
    return operation, images


def test_pending_preview_survives_reopen_and_approves_exact_bytes(pending_store):
    authority = pending_store
    operation, images = complete_operation()
    request = authority.request(operation, principal=_principal(), expires_at=NOW + 100, images=images)
    assert authority.request(operation, principal=_principal(), expires_at=NOW + 100, images=images) == request
    reopened = _store(authority.vault_root)
    preview = reopened.inspect_request_preview_for_owner(request.request_id, principal=_principal())
    assert preview.operation == operation
    assert preview.images == images
    approved = _approve_request(reopened, _principal(), request.request_id)
    assert reopened.reserve(operation, principal=_principal()).authority_ids == (approved,)


def test_legacy_hash_only_request_is_inspectable_but_cannot_be_approved(pending_store):
    request = pending_store.request(_operation(), principal=_principal(), expires_at=NOW + 100)
    assert pending_store.inspect_request_for_owner(request.request_id, principal=_principal()) == _operation()
    with pytest.raises(VocabularyAuthorityUnavailable):
        pending_store.inspect_request_preview_for_owner(request.request_id, principal=_principal())
    with pytest.raises(VocabularyAuthorityUnavailable):
        _approve_request(pending_store, _principal(), request.request_id)


def test_tampered_persisted_preview_refuses_owner_inspection_and_approval(pending_store):
    operation, images = complete_operation()
    request = pending_store.request(operation, principal=_principal(), expires_at=NOW + 100, images=images)
    custody = pending_store._validate_custody(_principal(), now=NOW)
    with pending_store._connect(custody, create=False) as connection:
        record = json.loads(connection.execute("SELECT operation_json FROM authority_requests").fetchone()[0])
        record["write_preview"]["images"][0]["after"] = "Different meaning"
        connection.execute("UPDATE authority_requests SET operation_json = ?", (json.dumps(record),))
    with pytest.raises(VocabularyAuthorityUnavailable):
        pending_store.inspect_request_preview_for_owner(request.request_id, principal=_principal())
    with pytest.raises(VocabularyAuthorityUnavailable):
        _approve_request(pending_store, _principal(), request.request_id)


def test_preview_is_not_disclosed_to_another_audience(pending_store):
    operation, images = complete_operation()
    request = pending_store.request(operation, principal=_principal(), expires_at=NOW + 100, images=images)
    with pytest.raises(VocabularyAuthorityDenied):
        pending_store.inspect_request_preview_for_owner(request.request_id, principal=_principal("other"))


def test_legacy_request_cannot_be_silently_upgraded_by_identity_retry(pending_store):
    operation, images = complete_operation()
    request = pending_store.request(operation, principal=_principal(), expires_at=NOW + 100)
    pending_store.request(operation, principal=_principal(), expires_at=NOW + 100, images=images)
    with pytest.raises(VocabularyAuthorityUnavailable):
        pending_store.inspect_request_preview_for_owner(request.request_id, principal=_principal())
