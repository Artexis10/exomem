"""automate-cell-storage-deletion D1, D2, D5: the key custodian purges a deleted cell's backups only on a
verified deletion grant, only once the cell's namespace is gone, and only after the grant's purge_after."""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from cellctl.custodian import PurgeConfig, Purger, _ensure_each
from cellctl.deletion_grant import load_public_keys, verify_grant
from cellctl.storage.fake_b2_keys import FakeBucket, FakeKeyStore
from cellctl.storage.interface import PURGE_KEY_CAPABILITIES

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[3] / "infra/contracts/cell-deletion-grants-v1.json").read_text(encoding="utf-8"))
CELL = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"
SUBSTRATE = Ed25519PrivateKey.generate()
DAY = 86_400
IAT = int(time.time()) - 10 * DAY


def _grant(cell_id: str, *, purge_after: int = IAT + 7 * DAY, key: Ed25519PrivateKey = SUBSTRATE) -> str:
    return jwt.encode({"aud": "exomem-cell-delete", "iat": IAT, "jti": f"grant-{cell_id}", "cell_id": cell_id,
                       "purge_after": purge_after}, key, algorithm="EdDSA")


class FakeCore:
    """The CoreV1Api calls of the custodian, over in-memory namespaces, Secrets and ConfigMaps."""

    def __init__(self) -> None:
        self.namespaces: set[str] = set()
        self.secrets: dict[tuple[str, str], k8s.V1Secret] = {}
        self.config_maps: dict[tuple[str, str], dict[str, str]] = {}

    def read_namespace(self, name: str) -> k8s.V1Namespace:
        if name not in self.namespaces:
            raise ApiException(status=404, reason="Not Found")
        return k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name=name))

    def read_namespaced_secret(self, name: str, namespace: str) -> k8s.V1Secret:
        if (namespace, name) not in self.secrets:
            raise ApiException(status=404, reason="Not Found")
        return self.secrets[(namespace, name)]

    def create_namespaced_secret(self, namespace: str, body: k8s.V1Secret) -> k8s.V1Secret:
        self.secrets[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_config_map(self, name: str, namespace: str) -> k8s.V1ConfigMap:
        if (namespace, name) not in self.config_maps:
            raise ApiException(status=404, reason="Not Found")
        return k8s.V1ConfigMap(data=dict(self.config_maps[(namespace, name)]))

    def create_namespaced_config_map(self, namespace: str, body: k8s.V1ConfigMap) -> k8s.V1ConfigMap:
        assert (namespace, body.metadata.name) not in self.config_maps
        self.config_maps[(namespace, body.metadata.name)] = dict(body.data)
        return body

    def replace_namespaced_config_map(self, name: str, namespace: str, body: k8s.V1ConfigMap) -> k8s.V1ConfigMap:
        assert (namespace, name) in self.config_maps
        self.config_maps[(namespace, name)] = dict(body.data)
        return body

    # -- test helpers --

    def relay(self, *grants: object) -> None:
        self.config_maps[("exomem-cloud", "exomem-cell-deletions")] = {"grants.json": json.dumps(list(grants))}

    def done(self) -> list[str]:
        return json.loads(self.config_maps.get(("exomem-key-custody", "exomem-key-custodian-purges"),
                                               {"done.json": "[]"})["done.json"])


class Cluster:
    """One custodian process over a fake API server, a fake key store and the fake backup bucket."""

    def __init__(self, tmp_path: Path, *, held: list[str] | None = None, now: float | None = None) -> None:
        self.core = FakeCore()
        self.keys = FakeKeyStore(bucket_id="backup-bucket")
        self.bucket = FakeBucket(self.keys)
        self.hold_file = tmp_path / "hold-cells.json"
        self.hold_file.write_text(json.dumps(held or []))
        self.keys_file = tmp_path / "grant-public-keys.json"
        self.keys_file.write_text(json.dumps([SUBSTRATE.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()]))
        self.now = now if now is not None else float(IAT + 8 * DAY)
        self.purger = self.restart()

    def restart(self) -> Purger:
        self.purger = Purger(self.core, self.keys, self.bucket.hider,
                             PurgeConfig(grant_keys_path=self.keys_file, hold_path=self.hold_file),
                             clock=lambda: self.now)
        return self.purger

    def sweep(self, *, namespaces: tuple[str, ...] = ()) -> None:
        grants_read = self.purger.refresh()
        listed = [k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name=name)) for name in namespaces]
        _ensure_each(self.core, self.keys, listed, lambda: None, self.purger.grants)
        self.purger.sweep(lambda: None, grants_read=grants_read)

    def purge_keys(self) -> list:
        return [key for key in self.keys.created if key.capabilities == frozenset(PURGE_KEY_CAPABILITIES)]


def _cell_key(cluster: Cluster, cell_id: str) -> str:
    return cluster.keys.create_prefix_key(cell_id).key_id


@pytest.mark.parametrize("case", FIXTURE["grants"], ids=[case["case"] for case in FIXTURE["grants"]])
def test_the_custodian_verifier_accepts_and_refuses_the_shared_grants(case: dict) -> None:
    """Defect: the custodian accepts a grant that the edge refuses, or the reverse, so one bucket purges alone."""
    keys = load_public_keys(json.dumps(FIXTURE["public_keys"]).encode())

    grant = verify_grant(case["token"], keys)

    assert (None if grant is None else vars(grant)) == case["accepted"]


def test_a_grant_that_the_custodian_cannot_verify_causes_no_action(tmp_path: Path) -> None:
    """Defect: an identity holding cellctl's power gets a live cell's key deleted or its backups hidden."""
    cluster = Cluster(tmp_path)
    key_id = _cell_key(cluster, CELL)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    forger = Ed25519PrivateKey.generate()
    cluster.core.relay(_grant(CELL, key=forger), _grant(OTHER))

    cluster.sweep()

    assert key_id in cluster.keys.keys
    assert [key for key in cluster.purge_keys() if key.name_prefix == f"cells/{CELL}/"] == []
    assert cluster.bucket.hidden == []


def test_a_malformed_entry_is_skipped_and_the_grant_beside_it_acts(tmp_path: Path) -> None:
    """Defect: one bad entry stops every purge, or stops the keys of new cells."""
    cluster = Cluster(tmp_path)
    _cell_key(cluster, CELL)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay({"not": "a token"}, "garbage", _grant(CELL))

    cluster.sweep(namespaces=(f"exo-cell-{OTHER}",))

    assert cluster.bucket.hidden == [f"cells/{CELL}/data/1"]
    assert ("exo-cell-" + OTHER, "cell-object-store-key") in cluster.core.secrets


def test_nothing_happens_while_the_namespace_exists(tmp_path: Path) -> None:
    """Defect: the custodian deletes the key of a cell whose Jobs still run, on cellctl's say-so."""
    cluster = Cluster(tmp_path)
    key_id = _cell_key(cluster, CELL)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.namespaces.add(f"exo-cell-{CELL}")
    cluster.core.relay(_grant(CELL))

    cluster.sweep()

    assert key_id in cluster.keys.keys
    assert cluster.bucket.hidden == []


def test_a_held_cell_is_left_alone_and_a_released_hold_resumes_on_the_same_grant(tmp_path: Path) -> None:
    """Defect: the operator's hold does not stop the purge, or a release never finishes it."""
    cluster = Cluster(tmp_path, held=[CELL])
    key_id = _cell_key(cluster, CELL)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay(_grant(CELL))

    cluster.sweep()
    held_keys, held_hides = set(cluster.keys.keys), list(cluster.bucket.hidden)
    cluster.hold_file.write_text("[]")
    cluster.sweep()

    assert held_keys == {key_id} and held_hides == []
    assert cluster.bucket.hidden == [f"cells/{CELL}/data/1"]


def test_an_unreadable_hold_list_purges_nothing(tmp_path: Path) -> None:
    """Defect: a bad edit of the hold list drops a hold the operator set, and the cell's backups are hidden."""
    cluster = Cluster(tmp_path)
    _cell_key(cluster, CELL)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay(_grant(CELL))
    cluster.hold_file.write_text("[not json")

    cluster.sweep()

    assert cluster.bucket.hidden == []


def test_the_key_goes_at_once_but_no_file_is_hidden_before_purge_after(tmp_path: Path) -> None:
    """Defect: the purge skips the deletion grace, or a deleted cell's key stays usable through it."""
    cluster = Cluster(tmp_path, now=float(IAT + DAY))
    key_id = _cell_key(cluster, CELL)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay(_grant(CELL))

    cluster.sweep()

    assert key_id not in cluster.keys.keys
    assert cluster.purge_keys() == []
    assert cluster.bucket.hidden == []


def test_after_purge_after_a_purge_key_of_the_exact_shape_hides_every_current_name_and_goes(tmp_path: Path) -> None:
    """Defect: the purge key can delete or reach beyond the cell, a current file stays, or the key outlives it."""
    cluster = Cluster(tmp_path)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload", "upload")
    cluster.bucket.seed(f"cells/{CELL}/data/2", "start", "upload")
    cluster.bucket.seed(f"cells/{CELL}/data/3", "hide", "upload")
    cluster.bucket.seed(f"cells/{OTHER}/data/1", "upload")
    cluster.core.relay(_grant(CELL))

    cluster.sweep()

    [purge_key] = cluster.purge_keys()
    assert purge_key.name_prefix == f"cells/{CELL}/"
    assert purge_key.bucket_ids == ("backup-bucket",)
    assert purge_key.valid_seconds == 3600
    assert sorted(cluster.bucket.hidden) == [f"cells/{CELL}/data/1", f"cells/{CELL}/data/2"]
    assert cluster.keys.keys == {}


def test_a_namespace_that_appears_before_the_hide_pass_stops_it(tmp_path: Path) -> None:
    """Defect: the custodian hides the files of a cell whose namespace exists again."""
    cluster = Cluster(tmp_path, now=float(IAT + DAY))
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay(_grant(CELL))
    cluster.sweep()
    cluster.core.namespaces.add(f"exo-cell-{CELL}")
    cluster.now = float(IAT + 8 * DAY)

    cluster.sweep()

    assert cluster.purge_keys() == []
    assert cluster.bucket.hidden == []


def test_a_finished_purge_is_marked_done_and_mints_no_further_key(tmp_path: Path) -> None:
    """Defect: every sweep and every restart mints a purge key for a finished cell."""
    cluster = Cluster(tmp_path)
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay(_grant(CELL))

    cluster.sweep()
    cluster.sweep()
    cluster.restart()
    cluster.sweep()

    assert cluster.core.done() == [CELL]
    assert len(cluster.purge_keys()) == 1


def test_a_done_mark_goes_with_its_grant(tmp_path: Path) -> None:
    """Defect: the done marks grow without end, one per deleted cell."""
    cluster = Cluster(tmp_path)
    cluster.core.relay(_grant(CELL))
    cluster.sweep()
    cluster.core.relay()

    cluster.sweep()

    assert cluster.core.done() == []


def test_a_bad_grant_key_file_keeps_the_grants_and_their_done_marks(tmp_path: Path) -> None:
    """Defect: a bad key value during a rotation reads as "no grants", so the done marks go and the cell's
    recreated namespace gets a key."""
    cluster = Cluster(tmp_path)
    cluster.core.relay(_grant(CELL))
    cluster.sweep()
    cluster.keys_file.write_text("not a key list")

    cluster.sweep(namespaces=(f"exo-cell-{CELL}",))

    assert cluster.core.done() == [CELL]
    assert (f"exo-cell-{CELL}", "cell-object-store-key") not in cluster.core.secrets


def test_a_stale_purge_key_is_deleted_before_a_new_one_is_minted(tmp_path: Path) -> None:
    """Defect: a crashed pass leaves a second key on the prefix, and it stays usable."""
    cluster = Cluster(tmp_path)
    stale = cluster.keys.create_purge_key(CELL, 3600).key_id
    cluster.bucket.seed(f"cells/{CELL}/data/1", "upload")
    cluster.core.relay(_grant(CELL))

    cluster.sweep()

    assert stale not in cluster.keys.keys
    assert cluster.bucket.hidden == [f"cells/{CELL}/data/1"]


def test_a_recreated_namespace_of_a_granted_cell_gets_no_key(tmp_path: Path) -> None:
    """Defect: a compromised cellctl recreates a deleted cell's namespace and receives a key to its backups."""
    cluster = Cluster(tmp_path, now=float(IAT + DAY))
    cluster.core.relay(_grant(CELL))
    cluster.core.namespaces.add(f"exo-cell-{CELL}")

    cluster.sweep(namespaces=(f"exo-cell-{CELL}",))

    assert cluster.keys.keys == {}
    assert (f"exo-cell-{CELL}", "cell-object-store-key") not in cluster.core.secrets

