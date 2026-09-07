import datetime as dt
import json
import sqlite3

import pytest

from exomem import deferred_index, review_state, vocabulary_notice_index, vocabulary_notifications
from exomem.governance.principal import owner_principal, request_scope
from exomem.vocabulary_state import VocabularyState
from exomem.vocabulary_workflow import Evidence, make_item


def item(version="v1", registry="r1"):
    return make_item(
        family="relation-type/v1",
        signal="generic-pair",
        targets={"entity:a": "a1", "entity:b": "b1"},
        evidence=[Evidence("source:1", version, "origin:1")],
        registry_hashes={"relations": registry},
        projection_status="current",
    )


def deliver(vault, current, session="conversation-one"):
    from exomem import vocabulary_notifications

    with request_scope(owner_principal(surface="library").with_authorization_session(session)):
        return vocabulary_notifications.take_advisory(vault, [current])


def test_delivery_is_bounded_and_deduplicated_across_restart(tmp_path):
    current = item()
    first = deliver(tmp_path, current)
    assert first["ref"] == current.ref
    assert len(json.dumps(first).encode()) <= 1024
    assert deliver(tmp_path, current) is None
    assert deliver(tmp_path, item(registry="unrelated-registry-edit")) is None
    assert deliver(tmp_path, current, session="another-conversation")
    assert deliver(tmp_path, item(version="new-evidence"))


@pytest.mark.parametrize("outcome", ["defer", "generic", "no-edge"])
def test_durable_disposition_survives_new_conversation(tmp_path, outcome):
    current = item()
    store = VocabularyState(tmp_path)
    store.observe(current)
    store.decide(
        current,
        {
            "item_ref": current.ref,
            "fingerprint": current.fingerprint,
            "family": current.family,
            "registry_hashes": dict(current.registry_hashes),
            "target_versions": dict(current.target_versions),
            "outcome": outcome,
            "choice": None,
            "rationale": "Keep the evidence without a more specific relation.",
        },
        actor="principal:test",
    )
    assert deliver(tmp_path, current, session="new-conversation") is None
    assert deliver(tmp_path, item(registry="unrelated"), session="newer-conversation") is None
    assert deliver(tmp_path, item(version="changed-evidence"))


def test_family_quiet_preserves_explicit_work_and_integrity_state(tmp_path):
    from exomem.vocabulary_notifications import REVIEW_FAMILIES

    current = item()
    family = REVIEW_FAMILIES[current.family]
    assert review_state.parse_family_ref(review_state.family_ref(family)) == family
    owner = review_state.ReviewStateStore(tmp_path)
    owner.set_disposition(family, "quiet", why="too_frequent: review this on request")
    before = owner.load()["records"]
    assert deliver(tmp_path, current) is None
    assert VocabularyState(tmp_path).page()["items"][0]["ref"] == current.ref
    assert owner.load()["records"] == before
    assert "entity_type_unregistered" not in REVIEW_FAMILIES.values()


def test_one_advisory_per_call_leaves_other_items_available(tmp_path):
    from exomem import vocabulary_notifications

    one, two = (
        item(),
        make_item(
            family="entity-type/v1",
            signal="agent-meaning-question",
            targets={"entity:c": "c1"},
            evidence=[],
            registry_hashes={"entity_types": "r1"},
            projection_status="current",
        ),
    )
    principal = owner_principal(surface="library").with_authorization_session("one-conversation")
    with request_scope(principal):
        first = vocabulary_notifications.take_advisory(tmp_path, [one, two])
        second = vocabulary_notifications.take_advisory(tmp_path, [one, two])
    assert {first["ref"], second["ref"]} == {one.ref, two.ref}
    assert len(VocabularyState(tmp_path).page()["items"]) == 2


def test_durable_decision_stops_new_transient_reservations_without_deleting_it(tmp_path):
    current = item()
    for session in ("one", "two", "three"):
        assert deliver(tmp_path, current, session)
    store = VocabularyState(tmp_path)
    store.decide(
        current,
        {
            "item_ref": current.ref,
            "fingerprint": current.fingerprint,
            "family": current.family,
            "registry_hashes": dict(current.registry_hashes),
            "target_versions": dict(current.target_versions),
            "outcome": "defer",
            "choice": None,
            "rationale": "The reviewed relation needs more evidence.",
        },
        actor="principal:test",
    )
    payload = review_state.ReviewStateStore(tmp_path).load()["vocabulary"]
    assert payload["notifications"] == {}
    assert f"{current.ref}:{current.fingerprint}" in payload["decisions"]
    assert deliver(tmp_path, current, "new-session") is None


def test_many_sessions_stay_out_of_json_and_use_exact_index_reservations(tmp_path):
    current = item()
    for ordinal in range(100):
        assert deliver(tmp_path, current, f"session-{ordinal}")
    assert review_state.ReviewStateStore(tmp_path).load()["vocabulary"]["notifications"] == {}
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM vocabulary_notification_reservations"
        ).fetchone()[0]
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM vocabulary_notification_reservations "
            "WHERE context = ? AND item_ref = ? AND fingerprint = ?",
            ("context", current.ref, current.fingerprint),
        ).fetchall()
    assert count == 100
    assert any("USING" in row[-1].upper() for row in plan)


def test_expired_index_cleanup_is_bounded(tmp_path):
    current = item()
    now = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    for ordinal in range(65):
        assert vocabulary_notice_index.reserve(
            tmp_path,
            context=f"expired-{ordinal}",
            item_ref=current.ref,
            fingerprint=current.fingerprint,
            expires_at=(now - dt.timedelta(seconds=1)).timestamp(),
            now=(now - dt.timedelta(days=1)).timestamp(),
        )
    assert vocabulary_notice_index.reserve(
        tmp_path,
        context="requested-expired",
        item_ref=current.ref,
        fingerprint=current.fingerprint,
        expires_at=(now - dt.timedelta(seconds=1)).timestamp(),
        now=(now - dt.timedelta(days=1)).timestamp(),
    )
    assert vocabulary_notice_index.reserve(
        tmp_path,
        context="requested-expired",
        item_ref=current.ref,
        fingerprint=current.fingerprint,
        expires_at=(now + dt.timedelta(days=1)).timestamp(),
        now=now.timestamp(),
    )
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM vocabulary_notification_reservations"
        ).fetchone()[0]
    assert remaining == 2


def test_legacy_notices_require_explicit_reconciliation_before_delivery(tmp_path):
    current = item()
    owner = review_state.ReviewStateStore(tmp_path)
    payload = owner.load()
    payload["vocabulary"]["items"][current.ref] = current.to_dict()
    payload["vocabulary"]["notifications"]["legacy"] = {
        "item_ref": current.ref,
        "fingerprint": current.fingerprint,
        "context": vocabulary_notifications._hash(["owner", "legacy"]),
    }
    owner._write(payload)
    assert deliver(tmp_path, current) is None
    result = vocabulary_notifications.reconcile_legacy(tmp_path)
    assert result == {"imported": 1, "dropped": 0}
    assert owner.load()["vocabulary"]["notifications"] == {}
    assert deliver(tmp_path, current, "legacy") is None
