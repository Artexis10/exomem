from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import native_owner_maintenance, server_runtime
from exomem.governance import authorization_custody
from exomem.governance.authorization_serving_membership import (
    MAX_ATTESTATION_TTL_SECONDS,
)
from exomem.native_owner_reviews import OwnerReviewStore

NOW = 1_700_000_000


def _renewal_module():
    return importlib.import_module("exomem.native_owner_renewal")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


def _completed_activation(
    store: OwnerReviewStore,
    *,
    owner_id: str = "github:123",
    body: dict | None = None,
):
    review = store.prepare(
        owner_id=owner_id,
        action="activation",
        body=body or {"renewal": "same-authority"},
        display={"title": "Activate"},
        binding_digest="a" * 64,
        expires_at=NOW + 60,
        now=NOW,
    )
    store.accept(
        review.review_id,
        owner_id=owner_id,
        expected_binding="a" * 64,
        now=NOW,
    )
    store.begin(review.review_id, owner_id=owner_id, now=NOW)
    completed = store.complete(
        review.review_id,
        owner_id=owner_id,
        result={"status": "active"},
        now=NOW,
    )
    store.enable_renewal(completed.review_id, owner_id=owner_id, now=NOW)
    return completed


def _approved_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if sys.platform != "linux":
        pytest.skip("systemd service fixtures require Linux")
    vault = _private_directory(tmp_path / "vault")
    authority = _private_directory(tmp_path / "authority")
    custody_dir = _private_directory(authority / "custody")
    env_file = tmp_path / "service.env"
    unit = tmp_path / "exomem.service"
    source = {
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_STATE_ROOT": str(tmp_path / "state"),
        "EXOMEM_GITHUB_USER_ID": "123",
        "EXOMEM_VOCABULARY_AUTHORITY_DIR": str(authority),
        "EXOMEM_OWNER_SERVICE_UNIT": str(unit),
    }
    env_file.write_text(
        "".join(f'{key}="{value}"\n' for key, value in source.items()),
        encoding="utf-8",
    )
    unit.write_text(
        "[Service]\n"
        f"EnvironmentFile={env_file}\n"
        f'ExecStart="{Path(sys.prefix) / "bin" / "python"}" -m exomem '
        "--transport streamable-http --port 8765\n",
        encoding="utf-8",
    )
    source_digest = native_owner_maintenance.service_environment_digest(unit)
    custody_environment = {
        authorization_custody.KEYRING_FILE_ENV: str(custody_dir / "keyring.json"),
        authorization_custody.CONTROL_FILE_ENV: str(custody_dir / "control.json"),
        authorization_custody.MEMBERSHIP_FILE_ENV: str(custody_dir / "membership.json"),
        authorization_custody.REPLICA_ID_ENV: "native-reviewed",
    }
    with env_file.open("ab") as handle:
        handle.write(native_owner_maintenance._render_custody_lines(custody_environment))  # noqa: SLF001
    for name, value in {**source, **custody_environment}.items():
        monkeypatch.setenv(name, value)
    keyring = [b"reviewed-keyring"]
    external = SimpleNamespace(
        keyring_path=Path(custody_environment[authorization_custody.KEYRING_FILE_ENV]),
        control_path=Path(custody_environment[authorization_custody.CONTROL_FILE_ENV]),
        keyring=keyring[0],
    )
    monkeypatch.setattr(
        authorization_custody,
        "load_external_custody",
        lambda _root: SimpleNamespace(
            keyring_path=external.keyring_path,
            control_path=external.control_path,
            keyring=keyring[0],
        ),
    )
    monkeypatch.setattr(
        authorization_custody,
        "_standalone_membership_file",
        lambda _root, *, external: (
            Path(custody_environment[authorization_custody.MEMBERSHIP_FILE_ENV]),
            b"membership",
            custody_environment[authorization_custody.REPLICA_ID_ENV],
        ),
    )
    body = {
        "service_unit": str(unit),
        "service_environment_digest": source_digest,
        "runtime_version": "reviewed-version",
        "custody_environment": custody_environment,
        "setup": {"keyring_digest": hashlib.sha256(keyring[0]).hexdigest()},
        "renewal": "same-authority",
    }
    return vault, authority, body, keyring


def test_start_renews_only_with_durable_completed_activation_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _renewal_module()
    vault, _authority, body, _keyring = _approved_installation(tmp_path, monkeypatch)
    store = OwnerReviewStore(vault)
    _completed_activation(store, body=body)
    renewed: list[tuple[Path, int]] = []
    worker = module.OwnerCustodyRenewal(
        vault,
        review_store_factory=lambda _root: store,
        renewer=lambda root, *, now: renewed.append((root, now)),
        clock=lambda: NOW + 100,
    )

    worker.start()
    worker.stop()

    assert renewed == [(vault, NOW + 100)]


@pytest.mark.parametrize("substitution", ["keyring", "settings"])
def test_reviewed_installation_substitution_denies_before_renewal(
    substitution: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _renewal_module()
    vault, _authority, body, keyring = _approved_installation(tmp_path, monkeypatch)
    store = OwnerReviewStore(vault)
    _completed_activation(store, body=body)
    if substitution == "keyring":
        keyring[0] = b"substituted-keyring"
    else:
        binding = native_owner_maintenance.service_binding(Path(body["service_unit"]))
        binding.binding_path.write_text(
            binding.binding_path.read_text().replace(
                "native-reviewed", "native-substituted"
            )
        )
        monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "native-substituted")
    renewed: list[object] = []
    worker = module.OwnerCustodyRenewal(
        vault,
        review_store_factory=lambda _root: store,
        renewer=lambda *_args, **_kwargs: renewed.append(object()),
        clock=lambda: NOW + 100,
    )

    worker.start()
    worker.stop()

    assert renewed == []


def test_unrelated_managed_environment_change_keeps_same_authority_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _renewal_module()
    vault, _authority, body, _keyring = _approved_installation(tmp_path, monkeypatch)
    store = OwnerReviewStore(vault)
    _completed_activation(store, body=body)
    binding = native_owner_maintenance.service_binding(Path(body["service_unit"]))
    with binding.binding_path.open("a", encoding="utf-8") as handle:
        handle.write('EXOMEM_TIMEZONE="Europe/Tallinn"\n')
    monkeypatch.setenv("EXOMEM_TIMEZONE", "Europe/Tallinn")
    renewed: list[int] = []
    worker = module.OwnerCustodyRenewal(
        vault,
        review_store_factory=lambda _root: store,
        renewer=lambda _root, *, now: renewed.append(now),
        clock=lambda: NOW + 100,
    )

    worker.start()
    worker.stop()

    assert renewed == [NOW + 100]


@pytest.mark.parametrize(
    "review",
    [
        None,
        SimpleNamespace(action="activation", state="accepted", body={"renewal": "same-authority"}),
        SimpleNamespace(action="policy", state="completed", body={"renewal": "same-authority"}),
        SimpleNamespace(action="activation", state="completed", body={"renewal": "different"}),
    ],
)
def test_missing_or_nonmatching_owner_consent_never_invokes_renewal(
    review, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(tmp_path / "authority"))
    monkeypatch.setenv("EXOMEM_OWNER_SERVICE_UNIT", str(tmp_path / "owner.service"))
    module = _renewal_module()
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "123")
    renewed: list[object] = []
    store = SimpleNamespace(renewal_review=lambda **_kwargs: review)
    worker = module.OwnerCustodyRenewal(
        tmp_path / "vault",
        review_store_factory=lambda _root: store,
        renewer=lambda *_args, **_kwargs: renewed.append(object()),
        clock=lambda: NOW,
    )
    worker._matches_reviewed_installation = lambda *_args: True  # noqa: SLF001

    worker.start()
    assert worker._thread is not None  # configured installations keep polling for consent
    worker.stop()

    assert renewed == []


def test_each_attempt_revalidates_the_current_pinned_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _renewal_module()
    owners: list[str] = []
    review = SimpleNamespace(
        action="activation", state="completed", body={"renewal": "same-authority"}
    )
    store = SimpleNamespace(
        renewal_review=lambda *, owner_id, now: (
            owners.append(owner_id),
            review,
        )[1]
    )
    worker = module.OwnerCustodyRenewal(
        tmp_path / "vault",
        review_store_factory=lambda _root: store,
        renewer=lambda *_args, **_kwargs: None,
        clock=lambda: NOW,
    )
    worker._matches_reviewed_installation = lambda *_args: True  # noqa: SLF001
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "123")
    worker._renew_if_allowed(NOW)  # noqa: SLF001
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "456")
    worker._renew_if_allowed(NOW + 1)  # noqa: SLF001

    assert owners == ["github:123", "github:456"]


def test_initial_renewal_failure_does_not_abort_runtime_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(tmp_path / "authority"))
    monkeypatch.setenv("EXOMEM_OWNER_SERVICE_UNIT", str(tmp_path / "owner.service"))
    module = _renewal_module()
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "123")
    review = SimpleNamespace(
        action="activation", state="completed", body={"renewal": "same-authority"}
    )
    worker = module.OwnerCustodyRenewal(
        tmp_path / "vault",
        review_store_factory=lambda _root: SimpleNamespace(
            renewal_review=lambda **_kwargs: review
        ),
        renewer=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
        clock=lambda: NOW,
    )
    worker._matches_reviewed_installation = lambda *_args: True  # noqa: SLF001

    worker.start()
    worker.stop()

    assert "private" not in caplog.text


def test_worker_uses_wall_clock_due_time_and_bounded_event_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(tmp_path / "authority"))
    monkeypatch.setenv("EXOMEM_OWNER_SERVICE_UNIT", str(tmp_path / "owner.service"))
    module = _renewal_module()
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "123")
    moment = [float(NOW)]
    renewed: list[int] = []
    second_renewal = threading.Event()
    review = SimpleNamespace(
        action="activation", state="completed", body={"renewal": "same-authority"}
    )

    class Event:
        def __init__(self) -> None:
            self.stopped = False
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            if len(renewed) >= 2:
                self.stopped = True
                return True
            moment[0] += timeout
            return False

    event = Event()
    worker = module.OwnerCustodyRenewal(
        tmp_path / "vault",
        review_store_factory=lambda _root: SimpleNamespace(
            renewal_review=lambda **_kwargs: review
        ),
        renewer=lambda _root, *, now: (
            renewed.append(now),
            second_renewal.set() if len(renewed) == 2 else None,
        ),
        clock=lambda: moment[0],
        shutdown_event=event,
    )
    worker._matches_reviewed_installation = lambda *_args: True  # noqa: SLF001

    worker.start()
    assert second_renewal.wait(timeout=1)
    worker.stop()

    assert renewed == [NOW, NOW + MAX_ATTESTATION_TTL_SECONDS // 4]
    assert event.waits
    assert all(0 < timeout <= 60 for timeout in event.waits)


def test_stop_waits_for_in_flight_publication(tmp_path: Path) -> None:
    module = _renewal_module()
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    worker = module.OwnerCustodyRenewal(tmp_path / "vault")

    def publish() -> None:
        entered.set()
        assert release.wait(timeout=5)

    publisher = threading.Thread(target=publish)
    worker._thread = publisher  # noqa: SLF001
    stopper = threading.Thread(target=lambda: (worker.stop(), returned.set()))
    publisher.start()
    try:
        assert entered.wait(timeout=1)
        stopper.start()
        assert worker._shutdown.wait(timeout=1)  # noqa: SLF001
        assert not returned.is_set()
    finally:
        release.set()
        publisher.join(timeout=2)
        if stopper.ident is not None:
            stopper.join(timeout=2)
    assert not publisher.is_alive()
    assert not stopper.is_alive()
    assert returned.is_set()


def test_local_lifespan_starts_and_joins_renewal_before_ordinary_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _renewal_module()
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    calls: list[str] = []
    ordinary_started = threading.Event()

    class Renewal:
        def __init__(self, _vault_root: Path) -> None:
            pass

        def start(self) -> None:
            calls.append("renewal-start")

        def stop(self) -> None:
            calls.append("renewal-stop")

    monkeypatch.setattr(module, "OwnerCustodyRenewal", Renewal)
    monkeypatch.setattr(
        server_runtime,
        "_start_derived_drain",
        lambda _root: (calls.append("ordinary-start"), ordinary_started.set()),
    )
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(
        tmp_path / "vault", fallback_seconds=60
    )

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            assert ordinary_started.wait(timeout=1)
            assert calls[:2] == ["renewal-start", "ordinary-start"]

    asyncio.run(exercise())

    assert calls[-1] == "renewal-stop"


@pytest.mark.parametrize('missing', ['both', 'authority', 'unit', 'owner'])
def test_unconfigured_native_owner_never_starts_renewal(tmp_path, monkeypatch, missing):
    module = _renewal_module()
    monkeypatch.setenv('EXOMEM_GITHUB_USER_ID', '123')
    monkeypatch.setenv('EXOMEM_VOCABULARY_AUTHORITY_DIR', str(tmp_path / 'authority'))
    monkeypatch.setenv('EXOMEM_OWNER_SERVICE_UNIT', str(tmp_path / 'owner.service'))
    if missing in {'both', 'authority'}:
        monkeypatch.delenv('EXOMEM_VOCABULARY_AUTHORITY_DIR')
    if missing in {'both', 'unit'}:
        monkeypatch.delenv('EXOMEM_OWNER_SERVICE_UNIT')
    if missing == 'owner':
        monkeypatch.setenv('EXOMEM_GITHUB_USER_ID', 'invalid')
    accessed = []
    renewed = []
    worker = module.OwnerCustodyRenewal(tmp_path / 'vault',
        review_store_factory=lambda root: (accessed.append(root), SimpleNamespace(
            renewal_review=lambda **kwargs: None))[1],
        renewer=lambda *args, **kwargs: renewed.append(args), clock=lambda: NOW)
    try:
        worker.start()
        assert accessed == []
        assert renewed == []
        assert worker._thread is None
        assert not worker._started
    finally:
        worker.stop()
