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

    assert result == {
        "stored": None,
        "contexts": {},
        "revision": "missing",
        "receipt_id": None,
    }
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
        "contexts": {},
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


# --------------------------------------------------- schema 2: per-context values


def _record(tmp_path, principal) -> dict:
    path = preferences._preference_path(tmp_path, principal.audience_id)
    return json.loads(path.read_bytes())


def _write_schema_one(tmp_path, principal, level: str) -> None:
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    state.write_text(
        json.dumps({"schema": 1, "prominence": level, "change_id": str(uuid.uuid4())}),
        encoding="utf-8",
    )


def test_a_schema_one_record_reads_as_its_identity_wide_value_with_no_contexts(
    tmp_path, principal
):
    _write_schema_one(tmp_path, principal, "maximal")

    result = _call(principal, preferences.inspect, tmp_path)

    assert result["stored"] == "maximal"
    assert result["contexts"] == {}


def test_first_write_upgrades_a_schema_one_record_in_place_without_loss(tmp_path, principal):
    _write_schema_one(tmp_path, principal, "maximal")
    before = _call(principal, preferences.inspect, tmp_path)

    saved = _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "balanced",
        before["revision"],
        context="coding",
    )

    assert saved["stored"] == "maximal", "the identity-wide value must survive the upgrade"
    assert saved["contexts"] == {"coding": "balanced"}
    assert saved["mutated"] is True
    assert saved["revision"] != before["revision"]
    assert _record(tmp_path, principal)["schema"] == 2
    assert set(_record(tmp_path, principal)) == {"schema", "prominence", "contexts", "change_id"}


def test_a_new_record_is_written_as_schema_two(tmp_path, principal):
    _call(principal, preferences.set_preference, tmp_path, "light", "missing")

    record = _record(tmp_path, principal)
    assert record["schema"] == 2
    assert record["prominence"] == "light"
    assert record["contexts"] == {}


def test_a_context_set_leaves_the_identity_wide_value_untouched(tmp_path, principal):
    first = _call(principal, preferences.set_preference, tmp_path, "maximal", "missing")

    saved = _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "balanced",
        first["revision"],
        context="coding",
    )

    assert saved["stored"] == "maximal"
    assert saved["contexts"] == {"coding": "balanced"}
    assert _call(principal, preferences.inspect, tmp_path)["contexts"] == {"coding": "balanced"}


def test_each_context_holds_its_own_level_under_one_revision(tmp_path, principal):
    first = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    second = _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "off",
        first["revision"],
        context="conversation",
    )

    assert second["contexts"] == {"coding": "balanced", "conversation": "off"}
    assert second["stored"] is None


def test_setting_the_same_context_level_again_is_a_noop(tmp_path, principal):
    saved = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )

    again = _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "medium",
        saved["revision"],
        context="coding",
    )

    assert again["mutated"] is False
    assert again["revision"] == saved["revision"]
    assert again["receipt_id"] == saved["receipt_id"]


def test_clear_removes_only_the_named_context(tmp_path, principal):
    first = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    second = _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "off",
        first["revision"],
        context="conversation",
    )
    third = _call(
        principal, preferences.set_preference, tmp_path, "maximal", second["revision"]
    )

    cleared = _call(
        principal, preferences.clear_preference, tmp_path, "coding", third["revision"]
    )

    assert cleared["mutated"] is True
    assert cleared["contexts"] == {"conversation": "off"}
    assert cleared["stored"] == "maximal", "clear must not touch the identity-wide value"


def test_clearing_an_absent_context_reports_no_mutation(tmp_path, principal):
    saved = _call(principal, preferences.set_preference, tmp_path, "light", "missing")

    cleared = _call(
        principal, preferences.clear_preference, tmp_path, "coding", saved["revision"]
    )

    assert cleared == {
        "stored": "light",
        "contexts": {},
        "revision": saved["revision"],
        "receipt_id": saved["receipt_id"],
        "mutated": False,
    }


def test_clearing_an_absent_context_on_missing_state_writes_no_record(tmp_path, principal):
    cleared = _call(principal, preferences.clear_preference, tmp_path, "coding", "missing")

    assert cleared["mutated"] is False
    assert cleared["revision"] == "missing"
    assert cleared["receipt_id"] is None
    assert not preferences._preference_path(tmp_path, principal.audience_id).exists()


def test_a_clear_cannot_miss_a_write_that_landed_since_the_caller_inspected(
    tmp_path, principal
):
    """The stale read the unlocked fast path would have allowed."""
    _call(principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding")

    with pytest.raises(OpError, match="PREFERENCE_CONFLICT"):
        _call(principal, preferences.clear_preference, tmp_path, "coding", "missing")


def test_clear_rejects_a_stale_revision(tmp_path, principal):
    first = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    _call(principal, preferences.set_preference, tmp_path, "maximal", first["revision"])

    with pytest.raises(OpError, match="PREFERENCE_CONFLICT"):
        _call(principal, preferences.clear_preference, tmp_path, "coding", first["revision"])
    assert _call(principal, preferences.inspect, tmp_path)["contexts"] == {"coding": "balanced"}


def test_a_set_cannot_use_a_revision_a_clear_superseded(tmp_path, principal):
    first = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    _call(principal, preferences.clear_preference, tmp_path, "coding", first["revision"])

    with pytest.raises(OpError, match="PREFERENCE_CONFLICT"):
        _call(
            principal,
            preferences.set_preference,
            tmp_path,
            "off",
            first["revision"],
            context="conversation",
        )


def test_clear_rejects_an_aba_revision_across_set_and_clear(tmp_path, principal):
    first = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    cleared = _call(
        principal, preferences.clear_preference, tmp_path, "coding", first["revision"]
    )
    _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "balanced",
        cleared["revision"],
        context="coding",
    )

    with pytest.raises(OpError, match="PREFERENCE_CONFLICT"):
        _call(principal, preferences.clear_preference, tmp_path, "coding", first["revision"])


@pytest.mark.parametrize("context", ["", "CODING", "project", "coding ", None, 7])
def test_clear_rejects_an_unknown_context_before_state_creation(tmp_path, principal, context):
    with pytest.raises(OpError, match="PREFERENCE_INVALID"):
        _call(principal, preferences.clear_preference, tmp_path, context, "missing")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("context", ["", "CODING", "project", 7])
def test_set_rejects_an_unknown_context_before_state_creation(tmp_path, principal, context):
    with pytest.raises(OpError, match="PREFERENCE_INVALID"):
        _call(
            principal, preferences.set_preference, tmp_path, "light", "missing", context=context
        )
    assert not list(tmp_path.iterdir())


def test_operator_override_refuses_a_clear_without_mutation(tmp_path, principal, monkeypatch):
    saved = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    monkeypatch.setenv("EXOMEM_PROMINENCE", "light")

    with pytest.raises(OpError, match="PREFERENCE_OPERATOR_OVERRIDE"):
        _call(principal, preferences.clear_preference, tmp_path, "coding", saved["revision"])
    monkeypatch.delenv("EXOMEM_PROMINENCE")
    assert _call(principal, preferences.inspect, tmp_path)["contexts"] == {"coding": "balanced"}


def test_unresolved_identity_refuses_clear_without_creating_state(tmp_path):
    unresolved = RequestPrincipal(audience_id="\x00unresolved", resolved=False, surface="rest")
    with pytest.raises(OpError, match="PREFERENCE_IDENTITY_REQUIRED"):
        _call(unresolved, preferences.clear_preference, tmp_path, "coding", "missing")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "payload",
    [
        # An empty schema-2 record carries no choice at all.
        {"schema": 2, "prominence": None, "contexts": {}},
        # Unknown top-level key.
        {"schema": 2, "prominence": None, "contexts": {"coding": "light"}, "extra": 1},
        # Unknown context name.
        {"schema": 2, "prominence": None, "contexts": {"project": "light"}},
        # Non-canonical context level.
        {"schema": 2, "prominence": None, "contexts": {"coding": "bogus"}},
        # Alias rather than the canonical spelling.
        {"schema": 2, "prominence": None, "contexts": {"coding": "medium"}},
        # Contexts must be a mapping.
        {"schema": 2, "prominence": None, "contexts": []},
        # A schema-1 record may not carry contexts.
        {"schema": 1, "prominence": "light", "contexts": {"coding": "light"}},
        # A schema-2 record must carry contexts.
        {"schema": 2, "prominence": "light"},
        # Unknown schema number.
        {"schema": 3, "prominence": "light", "contexts": {}},
    ],
)
def test_invalid_schema_two_records_fail_closed(tmp_path, principal, payload):
    state = preferences._preference_path(tmp_path, principal.audience_id, create=True)
    payload.setdefault("change_id", str(uuid.uuid4()))
    state.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OpError, match="PREFERENCE_STATE_UNAVAILABLE"):
        _call(principal, preferences.inspect, tmp_path)


def test_a_context_only_record_is_readable(tmp_path, principal):
    """A null identity-wide value is legitimate once a context value exists."""
    saved = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )

    assert saved["stored"] is None
    assert _call(principal, preferences.inspect, tmp_path)["stored"] is None
    assert _record(tmp_path, principal)["prominence"] is None


def test_the_previous_reader_degrades_on_a_schema_two_record_without_erasing_it(
    tmp_path, principal, monkeypatch
):
    """The 0.81.0 reader's exact key set rejects the newer record, and the request
    snapshot reports it unavailable rather than rewriting or deleting it."""
    from exomem import prominence as prominence_module

    _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )
    path = preferences._preference_path(tmp_path, principal.audience_id)
    raw = path.read_bytes()

    # The 0.81.0 shape check, verbatim: an exact schema-1 key set.
    assert set(json.loads(raw)) != {"schema", "prominence", "change_id"}

    def previous_reader(_vault_root):
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")

    monkeypatch.setattr(preferences, "inspect", previous_reader)
    with request_scope(principal):
        snapshot = prominence_module._request_preference(tmp_path)

    assert snapshot["preference"]["unavailable"] == "PREFERENCE_STATE_UNAVAILABLE"
    assert snapshot["preference"]["stored"] is None
    assert snapshot["preference"]["contexts"] == {}
    assert path.read_bytes() == raw, "a degraded read must neither rewrite nor delete the record"


def test_clearing_the_last_saved_value_returns_the_record_to_absent(tmp_path, principal):
    """An empty record is the shape `_read` fails closed on, so it is never written."""
    saved = _call(
        principal, preferences.set_preference, tmp_path, "balanced", "missing", context="coding"
    )

    cleared = _call(
        principal, preferences.clear_preference, tmp_path, "coding", saved["revision"]
    )

    assert cleared["mutated"] is True
    assert cleared["revision"] == "missing"
    assert cleared["receipt_id"] is None
    assert not preferences._preference_path(tmp_path, principal.audience_id).exists()
    assert _call(principal, preferences.inspect, tmp_path) == {
        "stored": None,
        "contexts": {},
        "revision": "missing",
        "receipt_id": None,
    }


def test_clearing_a_context_keeps_a_record_that_still_holds_an_identity_wide_value(
    tmp_path, principal
):
    first = _call(principal, preferences.set_preference, tmp_path, "maximal", "missing")
    saved = _call(
        principal,
        preferences.set_preference,
        tmp_path,
        "balanced",
        first["revision"],
        context="coding",
    )

    cleared = _call(
        principal, preferences.clear_preference, tmp_path, "coding", saved["revision"]
    )

    assert cleared["stored"] == "maximal"
    assert cleared["contexts"] == {}
    assert cleared["revision"] not in {"missing", saved["revision"]}
