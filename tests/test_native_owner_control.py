from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_vocabulary_control import _Authority, _principal

from exomem.native_owner_control import NativeOwnerControl
from exomem.vocabulary_control import VocabularyControl


class _Reviews:
    def prepare(self, **kwargs):
        self.review = SimpleNamespace(review_id="review-1", state="prepared", **kwargs)
        return self.review

    def get(self, review_id, *, owner_id, now):
        assert owner_id == self.review.owner_id
        return self.review

    def accept(self, review_id, *, owner_id, expected_binding, now):
        if expected_binding != self.review.binding_digest:
            raise ValueError("changed review")
        self.review.state = "accepted"
        return self.review

    def begin(self, review_id, *, owner_id, now):
        assert self.review.state == "accepted"
        self.review.state = "applying"
        return self.review

    def complete(self, review_id, *, owner_id, result, now):
        self.review.state = "completed"
        self.review.result = result
        return self.review


@pytest.fixture
def controller(tmp_path, monkeypatch):
    from contextlib import nullcontext

    authority = _Authority()
    reviews = _Reviews()
    controller = NativeOwnerControl(tmp_path, reviews=reviews)
    monkeypatch.setattr(controller, "_principal", lambda session_id, now: _principal())
    monkeypatch.setattr(controller, "_control", lambda decision=None: VocabularyControl(
        authority, owner_decision_callback=decision,
        activation_guard_factory=lambda root: nullcontext(),
    ))
    return controller, authority, reviews


def test_prepared_approval_uses_stored_bytes_then_explicit_acceptance(controller):
    control, authority, reviews = controller
    review = control.prepare(
        owner_id="github:123", action="approve",
        body={"session_id": "session-agent", "request_id": "request-1"}, now=1_700_000_000,
    )
    assert authority.approved == []
    assert review.display["write_images"][0]["after"] == "# Exact proposal\n"
    terminal = control.accept(review.review_id, owner_id="github:123", now=1_700_000_001)
    assert terminal.result == {"authority_id": "authority-1"}
    assert authority.approved[0][1].owner_id == "github:123"
    assert authority.approved[0][1].ceremony_id == review.review_id
    assert reviews.review.state == "completed"


def test_arbitrary_actions_and_caller_confirmation_fields_refuse(controller):
    control, authority, reviews = controller
    with pytest.raises(ValueError):
        control.prepare(owner_id="github:123", action="execute", body={}, now=1_700_000_000)
    with pytest.raises(ValueError):
        control.prepare(owner_id="github:123", action="approve", body={
            "session_id": "session-agent", "request_id": "request-1", "confirmed": True,
        }, now=1_700_000_000)
    assert authority.approved == []


def test_changed_principal_binding_invalidates_acceptance(controller, monkeypatch):
    from exomem.governance.principal import RequestPrincipal

    control, authority, reviews = controller
    review = control.prepare(owner_id="github:123", action="approve", body={
        "session_id": "session-agent", "request_id": "request-1",
    }, now=1_700_000_000)
    monkeypatch.setattr(control, "_principal", lambda session_id, now: RequestPrincipal(
        audience_id="another-agent", surface="hosted", issuer_family="hosted-gateway",
    ))
    with pytest.raises(ValueError, match="changed review"):
        control.accept(review.review_id, owner_id="github:123", now=1_700_000_001)
    assert authority.approved == []


def test_grant_round_trips_real_durable_review_values(controller, tmp_path, monkeypatch):
    from exomem.native_owner_reviews import OwnerReviewStore

    root = tmp_path / "vault"
    root.mkdir()
    directory = tmp_path / "authority"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(directory))
    control, authority, _ = controller
    control.reviews = OwnerReviewStore(root)
    review = control.prepare(owner_id="github:123", action="grant", body={
        "session_id": "session-agent", "actions": ["entity_type.add"],
        "scope": "vault", "expires_at": 1_700_001_000,
    }, now=1_700_000_000)
    result = control.accept(review.review_id, owner_id="github:123", now=1_700_000_001)
    assert result.state == "completed"
    assert result.result["authority_id"] == "grant-1"
    assert authority.grants[0]["audience"] == _principal()
