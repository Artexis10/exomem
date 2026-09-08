from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import mutation_lock, vocabulary_authority
from exomem.governance import authorization_custody
from exomem.native_owner_reviews import (
    MAX_REVIEW_JSON_BYTES,
    NativeOwnerReviewConflict,
    NativeOwnerReviewDenied,
    NativeOwnerReviewUnavailable,
    OwnerReviewStore,
)

NOW = 1_700_000_000
BINDING = "a" * 64


@pytest.mark.parametrize(
    ("suffix", "hardlink"),
    [("-journal", True), ("-wal", False), ("-shm", False)],
)
def test_windows_sidecar_protection_never_mutates_hardlinks_or_nonjournals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    hardlink: bool,
) -> None:
    if os.name == "nt":
        mutation_lock._windows_apply_private_dacl(
            tmp_path, mutation_lock._windows_current_user_sid()
        )
    database = tmp_path / "owner-reviews.sqlite"
    sidecar = database.with_name(f"{database.name}{suffix}")
    sidecar.write_bytes(b"sidecar")
    sidecar.chmod(0o600)
    if hardlink:
        os.link(sidecar, tmp_path / "shared-sidecar")

    fake_os = SimpleNamespace(name="nt", lstat=os.lstat, fstat=os.fstat)
    applied: list[Path] = []
    monkeypatch.setattr(vocabulary_authority, "os", fake_os)
    monkeypatch.setattr(
        authorization_custody,
        "_file_is_owner_protected",
        lambda _descriptor, _info: False,
    )
    monkeypatch.setattr(
        mutation_lock,
        "_windows_apply_private_dacl",
        lambda path, _sid: applied.append(path),
    )

    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        vocabulary_authority.VocabularyAuthority._validate_sqlite_sidecars(  # noqa: SLF001
            database, protect_inherited_windows=True
        )

    assert applied == []


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnerReviewStore:
    if os.name == "nt":
        pytest.skip("native owner review storage requires POSIX privacy controls")
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(authority))
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    return OwnerReviewStore(vault)


def _prepare(store: OwnerReviewStore, *, expires_at: int = NOW + 60):
    return store.prepare(
        owner_id="github:123",
        action="approve",
        body={"operation": {"paths": ["Knowledge Base/a.md"]}},
        display={"title": "Approve", "details": ["one"]},
        binding_digest=BINDING,
        expires_at=expires_at,
        now=NOW,
    )


def test_review_store_sqlite_pin_does_not_request_delete_access(
    store: OwnerReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[bool] = []
    original = mutation_lock.retain_regular_file

    def retain(path, *, delete_access=True):
        requested.append(delete_access)
        return original(path, delete_access=delete_access)

    monkeypatch.setattr(mutation_lock, "retain_regular_file", retain)

    _prepare(store)

    assert requested
    assert set(requested) == {False}


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL and sharing contract")
def test_windows_review_store_publishes_and_pins_a_private_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = mutation_lock._windows_current_user_sid()
    vault = tmp_path / "vault"
    authority = tmp_path / "authority"
    vault.mkdir()
    authority.mkdir()
    mutation_lock._windows_apply_private_dacl(vault, sid)
    mutation_lock._windows_apply_private_dacl(authority, sid)
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(authority))
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    store = OwnerReviewStore(vault)

    connection = store._connect(create=True)  # noqa: SLF001
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO settings VALUES ('probe', 'probe')")
        journal = store.database_path.with_name(
            f"{store.database_path.name}-journal"
        )
        inherited = mutation_lock.retain_regular_file(journal, delete_access=False)
        try:
            import msvcrt

            inherited_sddl = mutation_lock._windows_dacl_sddl_for_handle(
                msvcrt.get_osfhandle(inherited.fd)
            )
            assert mutation_lock._windows_private_dacl_is_valid(
                inherited_sddl, sid, directory=False
            ), inherited_sddl
            assert not authorization_custody._file_is_owner_protected(  # noqa: SLF001
                inherited.fd, os.fstat(inherited.fd)
            ), inherited_sddl
        finally:
            inherited.close()
        store._validate_sidecars(  # noqa: SLF001
            store.database_path, protect_inherited_windows=True
        )
        protected_journal = mutation_lock.retain_regular_file(
            journal, delete_access=False
        )
        try:
            assert authorization_custody._file_is_owner_protected(  # noqa: SLF001
                protected_journal.fd, os.fstat(protected_journal.fd)
            )
        finally:
            protected_journal.close()
    finally:
        connection.rollback()
        connection.close()

    _prepare(store)

    retained = mutation_lock.retain_regular_file(
        store.database_path, delete_access=False
    )
    try:
        assert authorization_custody._file_is_owner_protected(  # noqa: SLF001
            retained.fd, os.fstat(retained.fd)
        )
    finally:
        retained.close()


def test_review_values_are_deeply_immutable_and_as_dict_is_detached(
    store: OwnerReviewStore,
) -> None:
    body = {"nested": {"items": ["one"]}}
    display = {"title": "Review"}
    review = store.prepare(
        owner_id="github:123",
        action="policy",
        body=body,
        display=display,
        binding_digest=BINDING,
        expires_at=NOW + 60,
        now=NOW,
    )
    body["nested"]["items"].append("changed")
    display["title"] = "Changed"

    assert review.body["nested"]["items"] == ("one",)
    assert review.display["title"] == "Review"
    with pytest.raises(TypeError):
        review.body["new"] = True
    detached = review.as_dict()
    detached["body"]["nested"]["items"].append("detached")
    assert review.body["nested"]["items"] == ("one",)


def test_reviews_persist_across_store_restart_and_expose_expiry(
    store: OwnerReviewStore,
) -> None:
    prepared = _prepare(store)

    restarted = OwnerReviewStore(store.vault_root)
    current = restarted.get(prepared.review_id, owner_id="github:123", now=NOW + 61)

    assert current.review_id == prepared.review_id
    assert current.state == "prepared"
    assert current.expired is True


def test_wrong_owner_and_changed_binding_fail_closed(store: OwnerReviewStore) -> None:
    prepared = _prepare(store)

    with pytest.raises(NativeOwnerReviewDenied):
        store.get(prepared.review_id, owner_id="github:other", now=NOW)
    with pytest.raises(NativeOwnerReviewDenied):
        store.accept(
            prepared.review_id,
            owner_id="github:123",
            expected_binding="b" * 64,
            now=NOW,
        )


def test_new_reviews_have_a_bounded_live_expiry(store: OwnerReviewStore) -> None:
    with pytest.raises(NativeOwnerReviewDenied):
        _prepare(store, expires_at=NOW)
    with pytest.raises(NativeOwnerReviewDenied):
        _prepare(store, expires_at=NOW + 301)


def test_expired_review_never_becomes_an_approval(store: OwnerReviewStore) -> None:
    prepared = _prepare(store, expires_at=NOW + 1)

    with pytest.raises(NativeOwnerReviewDenied):
        store.accept(
            prepared.review_id,
            owner_id="github:123",
            expected_binding=BINDING,
            now=NOW + 1,
        )


def test_concurrent_accept_allows_exactly_one_transition(store: OwnerReviewStore) -> None:
    prepared = _prepare(store)

    def accept():
        return store.accept(
            prepared.review_id,
            owner_id="github:123",
            expected_binding=BINDING,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.exception() for future in [pool.submit(accept), pool.submit(accept)]]

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, NativeOwnerReviewConflict) for outcome in outcomes) == 1


def test_concurrent_begin_allows_exactly_one_execution(store: OwnerReviewStore) -> None:
    prepared = _prepare(store)
    store.accept(
        prepared.review_id,
        owner_id="github:123",
        expected_binding=BINDING,
        now=NOW,
    )

    def begin():
        return store.begin(prepared.review_id, owner_id="github:123", now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.exception() for future in [pool.submit(begin), pool.submit(begin)]]

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, NativeOwnerReviewConflict) for outcome in outcomes) == 1


def test_begin_records_start_before_expiry_and_reset_clears_it(
    store: OwnerReviewStore,
) -> None:
    prepared = _prepare(store)
    accepted = store.accept(
        prepared.review_id,
        owner_id="github:123",
        expected_binding=BINDING,
        now=NOW,
    )
    assert accepted.started_at is None

    applying = store.begin(prepared.review_id, owner_id="github:123", now=NOW + 1)
    assert applying.started_at == NOW + 1
    reset = store.reset_before_execution(
        prepared.review_id, owner_id="github:123", now=NOW + 2
    )
    assert reset.started_at is None


def test_begin_at_expiry_never_records_a_start(store: OwnerReviewStore) -> None:
    prepared = _prepare(store, expires_at=NOW + 1)
    store.accept(
        prepared.review_id,
        owner_id="github:123",
        expected_binding=BINDING,
        now=NOW,
    )

    with pytest.raises(NativeOwnerReviewDenied):
        store.begin(prepared.review_id, owner_id="github:123", now=NOW + 1)
    assert store.get(
        prepared.review_id, owner_id="github:123", now=NOW + 1
    ).started_at is None


def test_complete_is_not_replayable_but_remains_visible_after_expiry(
    store: OwnerReviewStore,
) -> None:
    prepared = _prepare(store, expires_at=NOW + 1)
    store.accept(
        prepared.review_id,
        owner_id="github:123",
        expected_binding=BINDING,
        now=NOW,
    )
    store.begin(prepared.review_id, owner_id="github:123", now=NOW)

    completed = store.complete(
        prepared.review_id,
        owner_id="github:123",
        result={"authority_id": "authority-1"},
        now=NOW + 2,
    )

    assert completed.state == "completed"
    assert completed.started_at == NOW
    assert completed.expired is True
    assert completed.result == {"authority_id": "authority-1"}
    with pytest.raises(NativeOwnerReviewConflict):
        store.complete(
            prepared.review_id,
            owner_id="github:123",
            result={"authority_id": "authority-1"},
            now=NOW + 2,
        )


def test_controller_can_explicitly_reset_only_an_applying_review(
    store: OwnerReviewStore,
) -> None:
    prepared = _prepare(store)
    store.accept(
        prepared.review_id,
        owner_id="github:123",
        expected_binding=BINDING,
        now=NOW,
    )
    with pytest.raises(NativeOwnerReviewConflict):
        store.reset_before_execution(prepared.review_id, owner_id="github:123", now=NOW)
    store.begin(prepared.review_id, owner_id="github:123", now=NOW)

    reset = store.reset_before_execution(
        prepared.review_id, owner_id="github:123", now=NOW
    )

    assert reset.state == "accepted"
    assert reset.started_at is None


def test_tampered_started_at_fails_closed(store: OwnerReviewStore) -> None:
    prepared = _prepare(store)
    store.accept(
        prepared.review_id,
        owner_id="github:123",
        expected_binding=BINDING,
        now=NOW,
    )
    store.begin(prepared.review_id, owner_id="github:123", now=NOW)
    connection = sqlite3.connect(store.database_path)
    connection.execute(
        "UPDATE owner_reviews SET started_at=expires_at WHERE review_id=?",
        (prepared.review_id,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(NativeOwnerReviewUnavailable):
        store.get(prepared.review_id, owner_id="github:123", now=NOW)


def _applying_activation(store: OwnerReviewStore, *, owner_id: str = "github:123"):
    review = store.prepare(
        owner_id=owner_id,
        action="activation",
        body={"renewal": "same-authority"},
        display={"title": "Activate"},
        binding_digest=BINDING,
        expires_at=NOW + 60,
        now=NOW,
    )
    store.accept(
        review.review_id, owner_id=owner_id, expected_binding=BINDING, now=NOW
    )
    store.begin(review.review_id, owner_id=owner_id, now=NOW)
    return review


def _completed_activation(store: OwnerReviewStore, *, owner_id: str = "github:123"):
    review = _applying_activation(store, owner_id=owner_id)
    return store.complete(
        review.review_id, owner_id=owner_id, result={"status": "active"}, now=NOW
    )


def test_completed_activation_can_enable_same_authority_renewal(
    store: OwnerReviewStore,
) -> None:
    completed = _completed_activation(store)

    enabled = store.enable_renewal(
        completed.review_id, owner_id="github:123", now=NOW + 100
    )
    restarted = OwnerReviewStore(store.vault_root)
    renewal = restarted.renewal_review(owner_id="github:123", now=NOW + 100)

    assert enabled.review_id == completed.review_id
    assert renewal is not None
    assert renewal.review_id == completed.review_id
    assert renewal.expired is True


def test_activation_completion_publishes_renewal_permission_atomically(
    store: OwnerReviewStore,
) -> None:
    applying = _applying_activation(store)

    completed = store.complete(
        applying.review_id,
        owner_id="github:123",
        result={"status": "active"},
        now=NOW,
    )
    renewal = OwnerReviewStore(store.vault_root).renewal_review(
        owner_id="github:123", now=NOW
    )

    assert renewal is not None
    assert renewal.review_id == completed.review_id


def test_renewal_write_failure_rolls_back_activation_completion(
    store: OwnerReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    applying = _applying_activation(store)
    original_connect = store._connect

    class _FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if "INSERT INTO settings" in sql:
                raise sqlite3.OperationalError("injected renewal failure")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            store,
            "_connect",
            lambda *, create: _FailingConnection(original_connect(create=create)),
        )
        with pytest.raises(sqlite3.OperationalError, match="injected renewal failure"):
            store.complete(
                applying.review_id,
                owner_id="github:123",
                result={"status": "active"},
                now=NOW,
            )

    current = store.get(applying.review_id, owner_id="github:123", now=NOW)
    assert current.state == "applying"
    assert store.renewal_review(owner_id="github:123", now=NOW) is None


def test_renewal_permission_rejects_wrong_review_and_tampered_pointer(
    store: OwnerReviewStore,
) -> None:
    ordinary = _prepare(store)
    with pytest.raises(NativeOwnerReviewDenied):
        store.enable_renewal(ordinary.review_id, owner_id="github:123", now=NOW)
    completed = _completed_activation(store)
    store.enable_renewal(completed.review_id, owner_id="github:123", now=NOW)
    connection = sqlite3.connect(store.database_path)
    connection.execute(
        "UPDATE settings SET value=? WHERE name='renewal_review_id'",
        ("owner-review-" + "f" * 32,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(NativeOwnerReviewUnavailable):
        store.renewal_review(owner_id="github:123", now=NOW)


def test_rejects_unknown_actions_and_oversized_json(store: OwnerReviewStore) -> None:
    with pytest.raises(ValueError):
        store.prepare(
            owner_id="github:123",
            action="execute",
            body={},
            display={},
            binding_digest=BINDING,
            expires_at=NOW + 60,
            now=NOW,
        )
    with pytest.raises(ValueError, match="OWNER_REVIEW_TOO_LARGE"):
        store.prepare(
            owner_id="github:123",
            action="policy",
            body={"value": "x" * MAX_REVIEW_JSON_BYTES},
            display={},
            binding_digest=BINDING,
            expires_at=NOW + 60,
            now=NOW,
        )


@pytest.mark.parametrize("tamper", ["permissions", "hardlink", "sidecar", "schema"])
def test_database_tampering_fails_closed(
    store: OwnerReviewStore, tmp_path: Path, tamper: str
) -> None:
    prepared = _prepare(store)
    database = store.database_path
    if tamper == "permissions":
        database.chmod(0o644)
    elif tamper == "hardlink":
        os.link(database, tmp_path / "database-hardlink")
    elif tamper == "sidecar":
        sidecar = database.with_name(f"{database.name}-wal")
        sidecar.write_text("tampered")
        sidecar.chmod(0o644)
    else:
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE unexpected (value TEXT)")
        connection.commit()
        connection.close()

    with pytest.raises(NativeOwnerReviewUnavailable):
        store.get(prepared.review_id, owner_id="github:123", now=NOW)


def test_rechecks_the_held_vault_root_attachment(store: OwnerReviewStore) -> None:
    prepared = _prepare(store)
    old_vault = store.vault_root.with_name("old-vault")
    store.vault_root.rename(old_vault)
    store.vault_root.mkdir(mode=0o700)

    with pytest.raises(NativeOwnerReviewUnavailable):
        store.get(prepared.review_id, owner_id="github:123", now=NOW)


def test_ledger_writes_only_to_external_authority_storage(
    store: OwnerReviewStore, tmp_path: Path
) -> None:
    _prepare(store)

    assert store.database_path.parent == tmp_path / "authority"
    assert store.database_path.name.startswith("owner-reviews-")
    assert list(store.vault_root.iterdir()) == []
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "xdg-state").exists()
    assert not (tmp_path / "xdg-config").exists()
