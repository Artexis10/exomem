from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.vocabulary_admission import (
    VocabularyAdmissionError,
    require_mutation_admission,
    require_restore_admission,
    runtime_admission,
)
from exomem.vocabulary_authority import AuthorityStatus


class _Authority:
    def __init__(self, _root: Path, status: AuthorityStatus) -> None:
        self.status = status

    def runtime_status(self) -> AuthorityStatus:
        return self.status


class _TimedAuthority(_Authority):
    def __init__(self, root: Path, status: AuthorityStatus) -> None:
        super().__init__(root, status)
        self.now: int | None = None

    def runtime_status(self, *, now: int | None = None) -> AuthorityStatus:
        self.now = now
        return self.status


def _factory(status: AuthorityStatus):  # noqa: ANN202
    return lambda root: _Authority(root, status)


def test_v1_and_supported_v2_remain_mutation_admitted(tmp_path: Path) -> None:
    for status in (AuthorityStatus("v1", None, 0), AuthorityStatus("v2", 7, 0)):
        admission = runtime_admission(tmp_path, authority_factory=_factory(status))
        assert admission.mutations_allowed is True


def test_unavailable_authority_refuses_mutations(tmp_path: Path) -> None:
    with pytest.raises(VocabularyAdmissionError, match="VOCABULARY_AUTHORITY_UNAVAILABLE"):
        require_mutation_admission(
            tmp_path, authority_factory=_factory(AuthorityStatus("unavailable", None, 0))
        )


def test_restore_refuses_activated_or_unavailable_destination(tmp_path: Path) -> None:
    for status, code in (
        (AuthorityStatus("v2", 8, 0), "VOCABULARY_RESTORE_REQUIRES_AUTHORITY"),
        (AuthorityStatus("unavailable", None, 0), "VOCABULARY_AUTHORITY_UNAVAILABLE"),
    ):
        with pytest.raises(VocabularyAdmissionError, match=code):
            require_restore_admission(tmp_path, authority_factory=_factory(status))


def test_v1_restore_remains_admitted(tmp_path: Path) -> None:
    admission = runtime_admission(
        tmp_path, authority_factory=_factory(AuthorityStatus("v1", None, 0))
    )
    assert admission.status.mode == "v1"


def test_default_authority_store_reports_v1_without_activated_custody(tmp_path: Path) -> None:
    assert require_mutation_admission(tmp_path).status.mode == "v1"
    assert require_restore_admission(tmp_path).status.mode == "v1"


def test_mutation_admission_passes_the_writer_attachment_time(tmp_path: Path) -> None:
    authority = _TimedAuthority(tmp_path, AuthorityStatus("v1", None, 0))

    require_mutation_admission(tmp_path, authority_factory=lambda _root: authority, now=123)

    assert authority.now == 123


def test_restore_admission_uses_the_governed_restore_time(tmp_path: Path) -> None:
    authority = _TimedAuthority(tmp_path, AuthorityStatus("v1", None, 0))

    require_restore_admission(tmp_path, authority_factory=lambda _root: authority, now=123)

    assert authority.now == 123


def test_portability_refuses_before_offline_publication_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import hosted_portability

    staging = tmp_path / "staging"
    staging.mkdir()
    prepared = hosted_portability.PreparedRestore(
        staging_root=staging,
        source_archive=tmp_path / "archive.zip",
        archive_sha256="0" * 64,
        manifest={},
        context=hosted_portability.PortabilityContext(
            cell_id="cell-a",
            vault_id="vault-a",
            operation_id="restore-a",
            created_at="2026-09-07T00:00:00Z",
            operator_authorized=True,
            lifecycle_state="restore-staging",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=False,
        ),
    )
    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_restore_admission",
        lambda _root: (_ for _ in ()).throw(
            VocabularyAdmissionError("VOCABULARY_RESTORE_REQUIRES_AUTHORITY")
        ),
    )
    moved = False

    def publish(_staging: Path, _live: Path) -> None:
        nonlocal moved
        moved = True

    with pytest.raises(hosted_portability.PortabilityError) as error:
        hosted_portability.publish_prepared_restore(prepared, tmp_path / "live", publish=publish)

    assert error.value.code == "VOCABULARY_RESTORE_REQUIRES_AUTHORITY"
    assert moved is False
    assert staging.is_dir()


def test_writer_guard_refuses_unavailable_authority_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.cli_ops import OpError
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_mutation_admission",
        lambda _root, **_kwargs: (_ for _ in ()).throw(
            VocabularyAdmissionError("VOCABULARY_AUTHORITY_UNAVAILABLE")
        ),
    )
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    with pytest.raises(OpError) as error:
        with manager.writer_authority_guard(vault_root=tmp_path):
            raise AssertionError("authority guard yielded")

    assert error.value.code == "VOCABULARY_AUTHORITY_UNAVAILABLE"


def test_writer_guard_uses_attachment_time_for_vocabulary_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.writer_lease import LeaseConfig, LeaseManager

    received: dict[str, int | None] = {}
    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_mutation_admission",
        lambda _root, *, now=None: received.setdefault("now", now),
    )
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    with manager.writer_authority_guard(vault_root=tmp_path, attachment_now=123):
        pass

    assert received == {"now": 123}


def test_writer_guard_uses_the_floor_two_session_open_admission_only_for_session_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.writer_lease import LeaseConfig, LeaseManager

    admitted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_session_open_admission",
        lambda _root, *, now: admitted.append(("session-open", now)),
    )
    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_mutation_admission",
        lambda _root, *, now: admitted.append(("mutation", now)),
    )
    monkeypatch.setattr(
        "exomem.governance.authorization_custody.require_standalone_mutation_admission",
        lambda _root, *, now: admitted.append(("custody", now)),
    )
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    with manager.writer_authority_guard(
        vault_root=tmp_path,
        attachment_now=123,
        session_open_admission=True,
    ):
        pass

    assert admitted == [("session-open", 123), ("custody", 123)]


def test_writer_invocation_reserves_floor_two_pending_for_session_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.cli_ops import OpError
    from exomem.writer_lease import LeaseConfig, LeaseManager

    calls: list[str] = []
    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_session_open_admission",
        lambda _root, *, now: calls.append("session-open"),
    )
    monkeypatch.setattr(
        "exomem.vocabulary_admission.require_mutation_admission",
        lambda _root, *, now: (_ for _ in ()).throw(
            VocabularyAdmissionError("VOCABULARY_AUTHORITY_UNAVAILABLE")
        ),
    )
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    session_open = SimpleNamespace(
        name="govern_memory",
        read_only=False,
        leaf=lambda _root, **_kwargs: "opened",
    )
    content_write = SimpleNamespace(
        name="remember",
        read_only=False,
        leaf=lambda _root, **_kwargs: "written",
    )

    assert manager.invoke(
        session_open,
        (tmp_path,),
        {"operation": "session", "session_action": "open", "ttl_seconds": 60},
    ) == "opened"
    with pytest.raises(OpError, match="VOCABULARY_AUTHORITY_UNAVAILABLE"):
        manager.invoke(content_write, (tmp_path,), {})

    assert calls == ["session-open"]


def test_hosted_activation_guard_keeps_the_shared_writer_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import hosted_runtime

    calls: list[tuple[Path, dict[str, object]]] = []

    class _Manager:
        def mutation_guard(self, vault_root: Path, **kwargs: object):
            calls.append((vault_root, kwargs))
            return nullcontext()

    monkeypatch.setattr("exomem.writer_lease.get_manager", lambda: _Manager())

    with hosted_runtime.hosted_vocabulary_activation_guard(tmp_path):
        pass

    assert calls == [
        (
            tmp_path,
            {
                "operation": "vocabulary-authority-activation",
                "holder_kind": "vocabulary-authority-control",
                "activation_admission": True,
            },
        )
    ]
