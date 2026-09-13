from __future__ import annotations

import hashlib
import json
import threading
import uuid

import pytest

from exomem import prominence_preferences as preferences
from exomem.cli_ops import OpError
from exomem.governance.principal import RequestPrincipal, request_scope


@pytest.fixture
def principal():
    return RequestPrincipal(audience_id="principal:alice", surface="mcp")


def _call(principal, fn, *args, **kwargs):
    with request_scope(principal):
        return fn(*args, **kwargs)


def test_missing_preference_is_read_only_and_reports_missing_revision(tmp_path, principal):
    result = _call(principal, preferences.inspect, tmp_path)

    assert result == {"stored": None, "revision": "missing", "receipt_id": None}
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize("level", ["off", "light", "balanced", "maximal"])
def test_each_level_round_trips_with_aliases(tmp_path, principal, level):
    before = _call(principal, preferences.inspect, tmp_path)
    result = _call(principal, preferences.set_preference, tmp_path, level, before["revision"])

    assert result["stored"] == level
    assert result["mutated"] is True
    assert _call(principal, preferences.inspect, tmp_path)["stored"] == level


def test_preference_isolated_by_principal_and_vault(tmp_path, principal):
    other = RequestPrincipal(audience_id="principal:bob", surface="mcp")
    rev = _call(principal, preferences.inspect, tmp_path)["revision"]
    _call(principal, preferences.set_preference, tmp_path, "max", rev)

    assert _call(other, preferences.inspect, tmp_path)["stored"] is None
    assert _call(principal, preferences.inspect, tmp_path / "other")["stored"] is None


def test_unresolved_identity_refuses_read_and_write_without_creating_state(tmp_path):
    unresolved = RequestPrincipal(audience_id="\x00unresolved", resolved=False, surface="rest")
    with pytest.raises(OpError, match="PREFERENCE_IDENTITY_REQUIRED"):
        _call(unresolved, preferences.inspect, tmp_path)
    with pytest.raises(OpError, match="PREFERENCE_IDENTITY_REQUIRED"):
        _call(unresolved, preferences.set_preference, tmp_path, "light", "missing")
    assert not list(tmp_path.iterdir())


def test_invalid_value_is_rejected_before_state_creation(tmp_path, principal):
    with pytest.raises(OpError, match="PREFERENCE_INVALID"):
        _call(principal, preferences.set_preference, tmp_path, "bogus", "missing")
    assert not list(tmp_path.iterdir())


def test_compare_and_swap_rejects_stale_revision(tmp_path, principal):
    first = _call(principal, preferences.inspect, tmp_path)
    saved = _call(principal, preferences.set_preference, tmp_path, "light", first["revision"])
    with pytest.raises(OpError, match="PREFERENCE_CONFLICT"):
        _call(principal, preferences.set_preference, tmp_path, "maximal", first["revision"])
    assert _call(principal, preferences.inspect, tmp_path)["revision"] == saved["revision"]


def test_compare_and_swap_rejects_aba_revision(tmp_path, principal):
    initial = _call(principal, preferences.inspect, tmp_path)
    first = _call(principal, preferences.set_preference, tmp_path, "light", initial["revision"])
    second = _call(principal, preferences.set_preference, tmp_path, "maximal", first["revision"])
    _call(principal, preferences.set_preference, tmp_path, "light", second["revision"])
    with pytest.raises(OpError, match="PREFERENCE_CONFLICT"):
        _call(principal, preferences.set_preference, tmp_path, "maximal", first["revision"])


def test_same_level_set_is_a_noop(tmp_path, principal):
    first = _call(principal, preferences.inspect, tmp_path)
    saved = _call(principal, preferences.set_preference, tmp_path, "light", first["revision"])
    again = _call(principal, preferences.set_preference, tmp_path, "low", saved["revision"])
    assert again == {
        "stored": "light",
        "revision": saved["revision"],
        "receipt_id": saved["receipt_id"],
        "mutated": False,
    }


def test_corrupt_preference_fails_closed(tmp_path, principal):
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    state.write_text("{not json", encoding="utf-8")
    with pytest.raises(OpError, match="PREFERENCE_STATE_UNAVAILABLE"):
        _call(principal, preferences.inspect, tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "prominence": "light"},
        {"schema": 1, "prominence": "bogus"},
    ],
)
def test_invalid_preference_schema_fails_closed(tmp_path, principal, payload):
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    payload.setdefault("change_id", str(uuid.uuid4()))
    state.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OpError, match="PREFERENCE_STATE_UNAVAILABLE"):
        _call(principal, preferences.inspect, tmp_path)


def test_boolean_schema_is_not_schema_one(tmp_path, principal):
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    state.write_text(json.dumps({"schema": True, "prominence": "light"}), encoding="utf-8")
    with pytest.raises(OpError, match="PREFERENCE_STATE_UNAVAILABLE"):
        _call(principal, preferences.inspect, tmp_path)


def test_oversize_preference_fails_closed(tmp_path, principal):
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    state.write_bytes(b"x" * (preferences._MAX_BYTES + 1))
    with pytest.raises(OpError, match="PREFERENCE_STATE_UNAVAILABLE"):
        _call(principal, preferences.inspect, tmp_path)


def test_symlink_preference_fails_closed(tmp_path, principal):
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    state.symlink_to(tmp_path / "missing.json")
    with pytest.raises(OpError, match="PREFERENCE_STATE_UNAVAILABLE"):
        _call(principal, preferences.inspect, tmp_path)


def test_operator_override_rejects_conflicting_write_without_mutation(
    tmp_path, principal, monkeypatch
):
    monkeypatch.setenv("EXOMEM_PROMINENCE", "light")
    current = _call(principal, preferences.inspect, tmp_path)
    with pytest.raises(OpError, match="PREFERENCE_OPERATOR_OVERRIDE"):
        _call(principal, preferences.set_preference, tmp_path, "maximal", current["revision"])
    assert _call(principal, preferences.inspect, tmp_path) == current


@pytest.mark.parametrize("revision", ["missing!", "ABC" + "0" * 61, "0" * 63, "0" * 65])
def test_invalid_revision_is_rejected_before_state_creation(tmp_path, principal, revision):
    with pytest.raises(OpError, match="PREFERENCE_INVALID"):
        _call(principal, preferences.set_preference, tmp_path, "light", revision)
    assert not list(tmp_path.iterdir())


def test_concurrent_changes_are_serialized(tmp_path, principal):
    barrier = threading.Barrier(2)
    outcomes = []
    revision = _call(principal, preferences.inspect, tmp_path)["revision"]

    def update(level):
        barrier.wait()
        try:
            outcomes.append(_call(principal, preferences.set_preference, tmp_path, level, revision))
        except OpError as error:
            outcomes.append(error.code)

    left = threading.Thread(target=update, args=("light",))
    right = threading.Thread(target=update, args=("maximal",))
    left.start()
    right.start()
    left.join()
    right.join()
    assert sorted(outcomes, key=str)[0] == "PREFERENCE_CONFLICT"
    assert _call(principal, preferences.inspect, tmp_path)["stored"] in {"light", "maximal"}


def test_revision_is_sha256_of_exact_file_bytes(tmp_path, principal):
    result = _call(principal, preferences.set_preference, tmp_path, "off", "missing")
    path = preferences._preference_path(tmp_path, principal.audience_id)
    assert result["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
