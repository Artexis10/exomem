"""The key custodian creates exactly one object-store key per cell namespace that lacks its Secret."""

from __future__ import annotations

from datetime import UTC, datetime

from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from cellctl.custodian import _ensure_each, ensure_cell_key
from cellctl.storage.fake_b2_keys import FakeKeyStore
from cellctl.storage.interface import CELL_KEY_CAPABILITIES

CELL = "aaaaaaaaaaaaaaaa"
SECRET = "cell-object-store-key"


class FakeCore:
    """The three CoreV1Api calls the custodian makes, over in-memory Secrets."""

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], k8s.V1Secret] = {}

    def read_namespaced_secret(self, name: str, namespace: str) -> k8s.V1Secret:
        if (namespace, name) not in self.secrets:
            raise ApiException(status=404, reason="Not Found")
        return self.secrets[(namespace, name)]

    def create_namespaced_secret(self, namespace: str, body: k8s.V1Secret) -> k8s.V1Secret:
        self.secrets[(namespace, body.metadata.name)] = body
        return body


def _namespace(name: str, *, deleting: bool = False) -> k8s.V1Namespace:
    return k8s.V1Namespace(metadata=k8s.V1ObjectMeta(
        name=name, deletion_timestamp=datetime.now(UTC) if deleting else None))


def test_a_cell_namespace_without_the_secret_gets_one_key_of_the_exact_shape() -> None:
    core, keys = FakeCore(), FakeKeyStore(bucket_id="backup-bucket")

    assert ensure_cell_key(core, keys, _namespace(f"exo-cell-{CELL}")) is True

    [(key_id, key)] = keys.keys.items()
    assert key.name_prefix == f"cells/{CELL}/"
    assert key.bucket_ids == ("backup-bucket",)
    assert key.capabilities == frozenset(CELL_KEY_CAPABILITIES)
    secret = core.secrets[(f"exo-cell-{CELL}", SECRET)]
    assert secret.type == "Opaque"
    assert secret.string_data["keyId"] == key_id
    assert secret.string_data["applicationKey"]


def test_a_cell_namespace_that_has_the_secret_is_left_alone() -> None:
    core, keys = FakeCore(), FakeKeyStore()
    namespace = _namespace(f"exo-cell-{CELL}")
    ensure_cell_key(core, keys, namespace)
    [first_key] = keys.keys

    assert ensure_cell_key(core, keys, namespace) is False

    assert list(keys.keys) == [first_key]


def test_a_lost_secret_deletes_the_listed_key_and_creates_a_new_one() -> None:
    core, keys = FakeCore(), FakeKeyStore()
    namespace = _namespace(f"exo-cell-{CELL}")
    ensure_cell_key(core, keys, namespace)
    [lost_key] = keys.keys
    core.secrets.clear()

    assert ensure_cell_key(core, keys, namespace) is True

    [new_key] = keys.keys
    assert new_key != lost_key
    assert core.secrets[(f"exo-cell-{CELL}", SECRET)].string_data["keyId"] == new_key


def test_a_namespace_that_is_not_a_cell_gets_no_key() -> None:
    core, keys = FakeCore(), FakeKeyStore()
    for name in ("kube-system", f"exo-cell-{CELL}-extra", "exo-cell-short", f"other-exo-cell-{CELL}"):
        assert ensure_cell_key(core, keys, _namespace(name)) is False

    assert keys.keys == {}
    assert core.secrets == {}


def test_a_deleting_cell_namespace_gets_no_key() -> None:
    core, keys = FakeCore(), FakeKeyStore()

    assert ensure_cell_key(core, keys, _namespace(f"exo-cell-{CELL}", deleting=True)) is False

    assert keys.keys == {}


def test_one_namespace_failing_does_not_stop_the_next() -> None:
    class FailsOnceKeyStore(FakeKeyStore):
        def create_prefix_key(self, cell_id: str):
            if cell_id == CELL:
                raise RuntimeError("provider unavailable")
            return super().create_prefix_key(cell_id)

    other = "bbbbbbbbbbbbbbbb"
    core, keys = FakeCore(), FailsOnceKeyStore()

    _ensure_each(core, keys, [_namespace(f"exo-cell-{CELL}"), _namespace(f"exo-cell-{other}")], lambda: None)

    assert [key.name_prefix for key in keys.keys.values()] == [f"cells/{other}/"]


def test_a_provider_outage_that_fails_every_namespace_still_beats_after_each_attempt() -> None:
    # The liveness probe reads the heartbeat's age: one beat per sweep would let N slow failures
    # outlast it and restart a pod that the provider's outage, not the pod, broke.
    class DownKeyStore(FakeKeyStore):
        def create_prefix_key(self, cell_id: str):
            raise RuntimeError("provider unavailable")

    beats: list[None] = []
    namespaces = [_namespace(f"exo-cell-{cell}") for cell in (CELL, "bbbbbbbbbbbbbbbb", "cccccccccccccccc")]

    _ensure_each(FakeCore(), DownKeyStore(), namespaces, lambda: beats.append(None))

    assert len(beats) == 3
