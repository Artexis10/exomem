"""The upload handoff's public key reaches exactly the cells the chart's one list selects."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import asyncpg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from cellctl import main, reconcile
from cellctl.manifests import render_statefulset
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .test_artifact_transport import BROKER_URL, CELL_ID, OTHER_CELL_ID, _digest
from .test_manifests import _spec
from .test_reconcile import FakeClusterGateway, _cluster_config, _secrets_config, _seed_cell

UNLISTED_CELL_ID = "cccccccccccccccc"
KEY_ENV = "EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY"


def _public_pem(private_key=None) -> str:
    """A throwaway signing pair's public half, as the Secret holds it."""
    key = private_key or ed25519.Ed25519PrivateKey.generate()
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def _uploads(public_key: str, cell_ids=(CELL_ID, OTHER_CELL_ID), **overrides) -> reconcile.ClusterConfig:
    return dataclasses.replace(_cluster_config(), upload_cell_ids=tuple(cell_ids),
                               upload_public_key=public_key, **overrides)


def test_upload_settings_are_off_when_the_environment_omits_them(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    config = main.build_cluster_config()
    assert config.upload_cell_ids == ()
    assert config.upload_public_key == ""


def test_upload_settings_load_from_the_environment(monkeypatch) -> None:
    public_key = _public_pem()
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    monkeypatch.setenv("CELLCTL_UPLOAD_CELL_IDS", f'["{CELL_ID}","{OTHER_CELL_ID}"]')
    monkeypatch.setenv("CELLCTL_UPLOAD_PUBLIC_KEY", public_key)
    config = main.build_cluster_config()
    assert config.upload_cell_ids == (CELL_ID, OTHER_CELL_ID)
    assert config.upload_public_key == public_key


@pytest.mark.parametrize("cell_ids", [("bad",), ("A" * 16,), ("1" * 16,), (CELL_ID, CELL_ID), CELL_ID])
def test_upload_activation_refuses_invalid_cell_lists(cell_ids) -> None:
    with pytest.raises(ValueError, match="upload activation"):
        _uploads(_public_pem(), cell_ids=cell_ids)


def test_upload_activation_refuses_a_listed_cell_with_no_public_key() -> None:
    with pytest.raises(ValueError, match="upload activation"):
        _uploads("")
    with pytest.raises(ValueError, match="upload activation"):
        _uploads(" \n")


def test_upload_activation_refuses_a_key_the_cell_would_refuse_without_echoing_it() -> None:
    private = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    wrong_algorithm = _public_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    for bad in ("not a key", private, wrong_algorithm):
        with pytest.raises(ValueError, match="Ed25519 public key") as raised:
            _uploads(bad)
        assert bad not in str(raised.value)
        with pytest.raises(ValueError, match="Ed25519 public key"):
            _spec(upload_public_key=bad)


def test_a_selected_runtime_gets_the_public_key_and_the_init_container_does_not() -> None:
    public_key = _public_pem()
    spec = _spec(upload_public_key=public_key)
    pod = render_statefulset(spec)["spec"]["template"]["spec"]
    env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
    assert env[KEY_ENV] == public_key
    assert not any("UPLOAD" in entry["name"] for entry in pod["initContainers"][0]["env"])
    unselected = render_statefulset(_spec())["spec"]["template"]["spec"]["containers"][0]["env"]
    assert KEY_ENV not in {entry["name"] for entry in unselected}


def test_only_listed_cells_move_their_digest_and_a_new_signing_pair_moves_them_again() -> None:
    public_key = _public_pem()
    legacy = _cluster_config()
    listed = _uploads(public_key)
    for cell_id in (CELL_ID, OTHER_CELL_ID):
        assert _digest(cell_id, listed) != _digest(cell_id, legacy)
    # The key never reaches a cell the list does not name, so its pod keeps running.
    assert _digest(UNLISTED_CELL_ID, listed) == _digest(UNLISTED_CELL_ID, legacy)
    rotated = _uploads(_public_pem())
    assert _digest(CELL_ID, rotated) != _digest(CELL_ID, listed)
    assert _digest(UNLISTED_CELL_ID, rotated) == _digest(UNLISTED_CELL_ID, legacy)


def test_uploads_and_the_broker_fleet_switch_select_cells_independently() -> None:
    config = _uploads(_public_pem(), cell_ids=(CELL_ID,),
                      artifact_broker_url=BROKER_URL, artifact_broker_all_cells=True)
    assert config.artifact_broker_for_cell(UNLISTED_CELL_ID) == BROKER_URL
    assert config.upload_public_key_for_cell(UNLISTED_CELL_ID) == ""
    assert config.upload_public_key_for_cell(CELL_ID) == config.upload_public_key


async def test_reconcile_gives_only_the_listed_cells_the_public_key(cell_db) -> None:
    public_key = _public_pem()
    cell_ids = (CELL_ID, OTHER_CELL_ID, UNLISTED_CELL_ID)
    for cell_id in cell_ids:
        await _seed_cell(cell_db, cell_id, cell_id)
    config = _uploads(public_key)
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    try:
        await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(),
                                       _secrets_config(), config, now=datetime(2026, 1, 1, tzinfo=UTC))
        for cell_id in cell_ids:
            pod = cluster.applied[("exo-cell-" + cell_id, "StatefulSet", "cell")]["spec"]["template"]["spec"]
            env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
            if cell_id == UNLISTED_CELL_ID:
                assert KEY_ENV not in env
            else:
                assert env[KEY_ENV] == public_key
    finally:
        await connection.close()
