"""add-cloud-media-store D4-D6: the media key bundle, the edge credential and the active set."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import math
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import asyncpg
import jwt
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kubernetes.client.rest import ApiException

from cellctl import db, main, manifests, media_edge, reconcile
from cellctl.decide import GROWTH_THRESHOLD_PERCENT
from cellctl.manifests import MEDIA_EDGE_ACCESS_LABEL, MEDIA_STORE_CONFIG_KEY, namespace_name
from cellctl.state import ClusterObservation, MediaCensus
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .test_reconcile import (
    IMAGE_A,
    FakeClusterGateway,
    _cluster_config,
    _converged_cell,
    _secrets_config,
    _seed_cell,
)

CANARY = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"
URL = "http://10.43.0.30:8769"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
ROOT = b"r" * 32


def _signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _edge(**overrides: object) -> media_edge.MediaEdgeConfig:
    settings: dict[str, object] = dict(
        url=URL, max_object_bytes=1 << 30, signing_key=_signing_key(), cell_ids=(CANARY,)
    )
    settings.update(overrides)
    return media_edge.MediaEdgeConfig(**settings)


# -- the bundle --


def test_the_cell_root_wrap_binds_the_cell_the_purpose_and_the_version() -> None:
    raw = media_edge.mint_bundle()
    wrapped = media_edge.wrap_bundle(ROOT, raw, cell_id=CANARY)

    assert media_edge.unwrap_bundle(ROOT, wrapped, cell_id=CANARY, key_version=1) == raw
    with pytest.raises(InvalidTag):
        media_edge.unwrap_bundle(ROOT, wrapped, cell_id=OTHER, key_version=1)
    with pytest.raises(InvalidTag):
        media_edge.unwrap_bundle(ROOT, wrapped, cell_id=CANARY, key_version=2)
    with pytest.raises(InvalidTag):
        # The same bytes read as another column's value, as a copied row would present them.
        from cellctl.secrets import unwrap_secret

        unwrap_secret(ROOT, wrapped, cell_id=CANARY, column="backup_key_wrapped", key_version=1)


def test_age_spells_its_keys_in_bech32() -> None:
    # BIP 173's valid empty-data string pins the checksum; the bundle's keys use the same encoder.
    assert media_edge._bech32_encode("a", b"") == "a12uel5l"
    bundle = media_edge.open_bundle(b"\x01" * 32 + b"\x02" * 32)
    assert bundle.recipient.startswith("age1") and bundle.identity.startswith("AGE-SECRET-KEY-1")


@pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age is not installed")
def test_age_itself_accepts_the_minted_keys(tmp_path) -> None:
    bundle = media_edge.open_bundle(media_edge.mint_bundle())
    identity = tmp_path / "identity"
    identity.write_text(bundle.identity + "\n")

    derived = subprocess.run(["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True)
    assert derived.stdout.strip() == bundle.recipient
    sealed = subprocess.run(["age", "-r", bundle.recipient], input=b"original bytes", check=True, capture_output=True)
    opened = subprocess.run(["age", "-d", "-i", str(identity)], input=sealed.stdout, check=True, capture_output=True)
    assert opened.stdout == b"original bytes"


# -- the credential --


def test_the_credential_verifies_with_a_standard_jwt_library() -> None:
    key = _signing_key()
    token = media_edge.sign_credential(key, cell_id=CANARY, version=1700)

    claims = jwt.decode(token, key.public_key(), algorithms=["EdDSA"], audience="exomem-media-edge",
                        options={"require": ["sub", "aud"]})
    assert claims == {"v": 1, "aud": "exomem-media-edge", "sub": CANARY, "ver": 1700, "issued_ms": 1700}
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, _signing_key().public_key(), algorithms=["EdDSA"], audience="exomem-media-edge")


# -- the active set --


def test_a_removed_cell_leaves_the_set_and_the_generation_rises() -> None:
    first = media_edge.plan_active_set(media_edge.ActiveSet(), {CANARY: 0, OTHER: 0}, NOW_MS)
    second = media_edge.plan_active_set(first, {CANARY: 0}, NOW_MS)

    assert second.cells == {CANARY: first.cells[CANARY]}
    assert second.generation > first.generation


def test_a_cell_added_again_gets_a_version_no_earlier_credential_carries() -> None:
    first = media_edge.plan_active_set(media_edge.ActiveSet(), {CANARY: 0}, NOW_MS)
    removed = media_edge.plan_active_set(first, {}, NOW_MS)
    back = media_edge.plan_active_set(removed, {CANARY: 0}, NOW_MS)

    assert back.cells[CANARY] > first.cells[CANARY]


def test_an_epoch_bump_reissues_one_cell_and_leaves_the_others() -> None:
    first = media_edge.plan_active_set(media_edge.ActiveSet(), {CANARY: 0, OTHER: 0}, NOW_MS)
    bumped = media_edge.plan_active_set(first, {CANARY: 1, OTHER: 0}, NOW_MS)

    assert bumped.cells[CANARY] > first.cells[CANARY]
    assert bumped.cells[OTHER] == first.cells[OTHER]


def test_an_unchanged_selection_plans_the_published_set() -> None:
    first = media_edge.plan_active_set(media_edge.ActiveSet(), {CANARY: 2}, NOW_MS)
    assert media_edge.plan_active_set(first, {CANARY: 2}, NOW_MS + 60_000) is first


@pytest.mark.parametrize("selected", [{}, {CANARY: 0}], ids=["empty", "selected"])
def test_an_unreadable_published_set_is_replaced_with_a_newer_generation(selected) -> None:
    unreadable = media_edge.read_active_set({media_edge.ACTIVE_SET_KEY: "{broken"})
    assert unreadable == media_edge.ActiveSet()
    planned = media_edge.plan_active_set(unreadable, selected, NOW_MS)
    assert planned.generation > 0
    assert set(planned.cells) == set(selected)
    assert media_edge.plan_active_set(planned, selected, NOW_MS + 60_000).generation == planned.generation


# -- cellctl's settings --


def test_unusable_edge_settings_are_refused_at_load() -> None:
    for overrides in (
        {"url": ""},
        {"url": "http://10.43.0.30:8768"},
        {"url": "http://example.com:8769"},
        {"max_object_bytes": 0},
        {"cell_ids": ("not-a-cell",)},
        {"cell_ids": (CANARY, CANARY)},
        {"credential_epochs": {CANARY: -1}},
        {"store_policy": {"recipient": "age1other"}},
    ):
        with pytest.raises(ValueError):
            _edge(**overrides)


def test_the_edge_settings_load_from_the_chart_environment_and_default_to_none(monkeypatch, tmp_path) -> None:
    assert main.build_media_edge_config() is None
    key = _signing_key()
    path = tmp_path / "signing-key"
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_URL", URL)
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_SIGNING_KEY_FILE", str(path))
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_MAX_OBJECT_BYTES", "1073741824")
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_CELL_IDS", json.dumps([CANARY]))
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_CREDENTIAL_EPOCHS", json.dumps({CANARY: 3}))

    config = main.build_media_edge_config()

    assert config.selects(CANARY) and not config.selects(OTHER) and config.epoch(CANARY) == 3
    assert config.max_object_bytes == 1 << 30
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_ALL_CELLS", '"yes"')
    with pytest.raises(ValueError, match="all-cells switch"):
        main.build_media_edge_config()


# -- the cell's own render --


def _spec(**overrides: object) -> manifests.CellManifestSpec:
    bundle = media_edge.open_bundle(media_edge.mint_bundle())
    settings: dict[str, object] = dict(
        cell_id=CANARY, image="registry.example/cell@sha256:" + "0" * 64, replicas=1, read_only=False,
        bearer_current="bearer", backup_password="backup", b2_key_id="id", b2_key_secret="secret",
        media_edge_url=URL, media_edge_credential="credential", media_age_identity=bundle.identity,
        media_recipient=bundle.recipient, media_name_key=bundle.name_key, media_key_version=1,
        media_max_object_bytes=1 << 30)
    settings.update(overrides)
    return manifests.CellManifestSpec(**settings)


def test_a_selected_cell_reaches_only_the_edge_and_mounts_only_the_media_keys() -> None:
    rendered = manifests.render_cell_manifests(_spec())
    policy = next(m for m in rendered if m["metadata"]["name"] == "runtime-ingress")
    pod = next(m for m in rendered if m["kind"] == "StatefulSet")["spec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    volume = next(v for v in pod["volumes"] if v["name"] == "media")

    assert [(r["to"][0]["podSelector"]["matchLabels"], r["ports"][0]["port"]) for r in policy["spec"]["egress"]] == [
        ({"app.kubernetes.io/name": "exomem-media-edge"}, 8769)]
    assert env["EXOMEM_MEDIA_EDGE_URL"] == URL
    assert env["EXOMEM_MEDIA_STORE_CONFIG"] == "/run/exomem/media/store.json"
    assert env["EXOMEM_MEDIA_EDGE_CREDENTIAL_FILE"] == "/run/exomem/media/edge-credential"
    assert {item["path"] for item in volume["secret"]["items"]} == {
        "edge-credential", "age-identity", "name-key", "store.json"}


def test_a_cell_the_edge_does_not_serve_renders_as_it_did_before_the_edge() -> None:
    plain = manifests.render_cell_manifests(_spec(
        media_edge_url="", media_edge_credential="", media_age_identity="", media_recipient="",
        media_name_key=b"", media_key_version=0, media_max_object_bytes=0))
    rendered = json.dumps(plain)

    assert "media" not in rendered.replace("media_engines", "")
    assert next(m for m in plain if m["metadata"]["name"] == "runtime-ingress")["spec"]["egress"] == []


def test_the_store_policy_carries_the_growth_threshold_cellctl_grows_the_volume_at() -> None:
    # The cell sizes import windows below this threshold. A second copy in the cell would drift from
    # cellctl's, and a window sized on a higher one grows the volume at the next backup.
    secret = manifests.render_secret(_spec(media_store_policy={"max_temporary_bytes": 5 << 30, "max_hydrations": 1}))
    store = json.loads(base64.b64decode(secret["data"][MEDIA_STORE_CONFIG_KEY]))["vaults"]["/data/vault"]

    assert store["growth_threshold"] == GROWTH_THRESHOLD_PERCENT / 100
    assert (store["max_temporary_bytes"], store["max_hydrations"]) == (5 << 30, 1)


@pytest.mark.parametrize("policy", [
    {"max_temporary_bytes": 5 << 30},
    {"max_temporary_bytes": 5 << 30, "max_hydrations": True},
    {"max_temporary_bytes": 5 << 30, "max_hydrations": 1.5},
    {"max_temporary_bytes": math.inf, "max_hydrations": 1},
    {"max_temporary_bytes": 5 << 30, "max_hydrations": 0},
], ids=["half set", "a boolean", "a fraction", "infinity", "zero"])
def test_a_hydration_capacity_the_cell_would_read_as_none_is_a_render_error(policy) -> None:
    # The cell reads such a pair as no capacity, so every owner read of an evicted original would fail.
    with pytest.raises(ValueError, match="hydration"):
        _spec(media_store_policy=policy)


# -- reconcile --


class EdgeCluster(FakeClusterGateway):
    def __init__(self) -> None:
        super().__init__()
        self.active_set: dict[str, str] | None = None
        self.publishes: list[dict] = []
        self.reported: int | Exception = 0
        self.fail_publish = False

    def read_media_edge_active_set(self) -> dict[str, str] | None:
        return self.active_set

    def apply_media_edge_active_set(self, manifest: dict) -> None:
        if self.fail_publish:
            raise RuntimeError("the API server is away")
        self.publishes.append(manifest)
        self.active_set = manifest["data"]

    def media_edge_generation(self, url: str) -> int:
        if isinstance(self.reported, Exception):
            raise self.reported
        return self.reported

    def published(self) -> dict:
        return json.loads(self.active_set[media_edge.ACTIVE_SET_KEY])

    def secret(self, cell_id: str) -> dict[str, bytes]:
        data = self.applied[(namespace_name(cell_id), "Secret", "cell-credentials")]["data"]
        return {key: base64.b64decode(value) for key, value in data.items()}


class Fleet:
    """One database, one fake cluster and the passes over them."""

    def __init__(self, cell_db, config: reconcile.ClusterConfig) -> None:
        self.cell_db, self.config = cell_db, config
        self.cluster = EdgeCluster()
        self.memory = reconcile.LoopMemory()
        self.connection: asyncpg.Connection | None = None
        self.b2, self.hetzner = FakeB2(), FakeHetznerVolumeProvider()

    async def seed(self, *cell_ids: str) -> None:
        for cell_id in cell_ids:
            await _seed_cell(self.cell_db, cell_id, cell_id)
        self.connection = await asyncpg.connect(self.cell_db.dsn(role="exomem_cellctl"))

    async def run(self, now: datetime = NOW) -> bool:
        return await reconcile.reconcile_once(
            self.connection, self.cluster, self.b2, self.hetzner, _secrets_config(), self.config,
            now=now, memory=self.memory)

    async def rows(self) -> dict[str, object]:
        return {row.cell_id: row for row in await db.select_all_rows(self.connection, media_keys=True)}

    async def close(self) -> None:
        await self.connection.close()


async def _fleet(cell_db, *cell_ids: str, **edge: object) -> Fleet:
    config = dataclasses.replace(_cluster_config(), media_edge=_edge(**edge))
    fleet = Fleet(cell_db, config)
    await fleet.seed(*cell_ids)
    return fleet


async def test_a_selected_cell_receives_its_bundle_and_credential_and_another_receives_none(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY, OTHER)
    try:
        await fleet.run()
        rows = await fleet.rows()
        selected, other = fleet.cluster.secret(CANARY), fleet.cluster.secret(OTHER)

        assert rows[CANARY].media_key_wrapped is not None and rows[CANARY].media_key_version == 1
        assert rows[OTHER].media_key_wrapped is None and rows[OTHER].media_key_version is None
        claims = jwt.decode(selected["media-edge-credential"].decode(),
                            fleet.config.media_edge.signing_key.public_key(), algorithms=["EdDSA"],
                            audience="exomem-media-edge")
        assert claims["sub"] == CANARY and claims["ver"] == fleet.cluster.published()["cells"][CANARY]
        store = json.loads(selected[MEDIA_STORE_CONFIG_KEY])["vaults"]["/data/vault"]
        assert store["adapter"] == "media-edge" and store["store"] == f"media-edge:{CANARY}"
        assert not {"media-edge-credential", "media-age-identity", "media-name-key"} & other.keys()
    finally:
        await fleet.close()


async def test_a_standard_mode_re_render_gives_the_same_bundle_and_credential(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        first = fleet.cluster.secret(CANARY)
        await fleet.run(NOW + timedelta(minutes=5))
        second = fleet.cluster.secret(CANARY)

        assert {key: second[key] for key in first} == first
        assert len(fleet.cluster.publishes) == 1  # an unchanged selection publishes nothing
    finally:
        await fleet.close()


async def test_the_edge_render_holds_neither_key(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        secret = fleet.cluster.secret(CANARY)
        published = json.dumps(fleet.cluster.publishes[0])
        edge_key = fleet.config.media_edge.signing_key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())

        for held in (secret["media-age-identity"], secret["media-name-key"], secret["media-edge-credential"],
                     base64.b64encode(secret["media-name-key"]), base64.b64encode(edge_key)):
            assert held.decode("latin-1") not in published
    finally:
        await fleet.close()


async def test_half_a_bundle_is_refused_and_never_minted_over(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            await owner.execute("UPDATE exomem_cloud_cells SET media_key_version = NULL WHERE cell_id=$1", CANARY)
        finally:
            await owner.close()
        before = (await fleet.rows())[CANARY].media_key_wrapped

        with pytest.raises(ValueError, match="incomplete media key state"):
            await reconcile._resolve_secret_material(
                fleet.connection, (await fleet.rows())[CANARY], _secrets_config(), media=True)
        assert (await fleet.rows())[CANARY].media_key_wrapped == before
    finally:
        await fleet.close()


async def test_a_cell_the_pass_could_not_observe_keeps_its_place_in_the_set(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY, OTHER, cell_ids=(CANARY, OTHER))
    try:
        await fleet.run()
        generation = fleet.cluster.published()["generation"]

        original = fleet.cluster.observe

        def observe(cell_id: str, namespace: str):
            if cell_id == OTHER:
                raise RuntimeError("observe failed")
            return original(cell_id, namespace)

        fleet.cluster.observe = observe
        await fleet.run(NOW + timedelta(minutes=5))

        assert set(fleet.cluster.published()["cells"]) == {CANARY, OTHER}
        assert fleet.cluster.published()["generation"] == generation
    finally:
        await fleet.close()


async def test_a_drained_cell_leaves_the_set_and_a_later_return_is_a_new_version(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY, OTHER, cell_ids=(CANARY, OTHER))
    try:
        await fleet.run()
        first = fleet.cluster.published()
        fleet.config = dataclasses.replace(fleet.config, media_edge=_edge(
            cell_ids=(CANARY,), signing_key=fleet.config.media_edge.signing_key))
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, OTHER, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=6))
        await fleet.run(NOW + timedelta(minutes=7))
        await fleet.run(NOW + timedelta(minutes=8))
        revoked = fleet.cluster.published()
        fleet.config = dataclasses.replace(fleet.config, media_edge=_edge(
            cell_ids=(CANARY, OTHER), signing_key=fleet.config.media_edge.signing_key))
        await fleet.run(NOW + timedelta(minutes=10))
        returned = fleet.cluster.published()

        assert OTHER not in revoked["cells"] and revoked["generation"] > first["generation"]
        assert returned["cells"][OTHER] > first["cells"][OTHER]
        assert returned["cells"][CANARY] == first["cells"][CANARY]
    finally:
        await fleet.close()


async def test_a_revocation_is_not_settled_until_the_edge_reports_its_generation(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY)
    try:
        assert await fleet.run() is True
        generation = fleet.cluster.published()["generation"]
        assert fleet.memory.media_edge_confirmed_generation == 0

        fleet.cluster.reported = generation - 1  # still draining the earlier set
        await fleet.run(NOW + timedelta(minutes=1))
        assert fleet.memory.media_edge_confirmed_generation == 0

        fleet.cluster.reported = RuntimeError("the edge is away")
        await fleet.run(NOW + timedelta(minutes=2))
        assert fleet.memory.media_edge_confirmed_generation == 0

        fleet.cluster.reported = generation
        await fleet.run(NOW + timedelta(minutes=3))
        assert fleet.memory.media_edge_confirmed_generation == generation
    finally:
        await fleet.close()


async def test_a_selected_cell_waits_when_the_active_set_cannot_be_published(cell_db) -> None:
    fleet = await _fleet(cell_db, CANARY)
    try:
        fleet.cluster.fail_publish = True
        assert await fleet.run() is True

        # Rendering without the credential would strip a running cell of its store.
        assert (namespace_name(CANARY), "Secret", "cell-credentials") not in fleet.cluster.applied
        fleet.cluster.fail_publish = False
        await fleet.run(NOW + timedelta(minutes=1))
        assert "media-edge-credential" in fleet.cluster.secret(CANARY)
    finally:
        await fleet.close()


# -- the retained store (D12): a turned-off cell keeps its key until a later report shows none evicted --


def _turn_off(fleet: Fleet) -> None:
    """The operator takes the canary off the edge's selection; the edge itself stays on."""
    fleet.config = dataclasses.replace(fleet.config, media_edge=_edge(
        cell_ids=(), signing_key=fleet.config.media_edge.signing_key))


def _report(fleet: Fleet, cell_id: str = CANARY, **census: object) -> None:
    """A backup Job's media report, as the cluster observation carries it."""
    fleet.cluster.observations[cell_id] = ClusterObservation(backup_job_media_census=MediaCensus(**census))


def _media_posture(fleet: Fleet) -> dict[str, bool]:
    """What the canary holds of the store after the last pass."""
    secret = fleet.cluster.secret(CANARY)
    policy = fleet.cluster.applied[(namespace_name(CANARY), "NetworkPolicy", "runtime-ingress")]
    return {
        "key": "media-age-identity" in secret,
        "credential": "media-edge-credential" in secret,
        "egress": policy["spec"]["egress"] != [],
        "active set": CANARY in fleet.cluster.published()["cells"],
        "offload": json.loads(secret[MEDIA_STORE_CONFIG_KEY])["vaults"]["/data/vault"]["enabled"],
    }


async def test_a_turned_off_cell_with_evicted_originals_keeps_its_key_credential_egress_and_set_entry(cell_db) -> None:
    # Removing any of them leaves every evicted original unreadable.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, evicted=3, evicted_bytes=3 << 30, captured_at=NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        await fleet.run(NOW + timedelta(minutes=20))

        assert _media_posture(fleet) == {
            "key": True, "credential": True, "egress": True, "active set": True, "offload": False}
    finally:
        await fleet.close()


async def test_a_turned_off_cell_with_no_report_keeps_its_key_credential_egress_and_set_entry(cell_db) -> None:
    # Without a report cellctl cannot know that every original is back on the volume.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        await fleet.run(NOW + timedelta(days=1))

        assert _media_posture(fleet) == {
            "key": True, "credential": True, "egress": True, "active set": True, "offload": False}
    finally:
        await fleet.close()


# The turn-off is recorded at NOW + 5 minutes; a capture at that instant may still predate it.
@pytest.mark.parametrize("captured_minutes", [1, 5], ids=["before the turn-off", "at the turn-off"])
async def test_a_report_captured_before_or_at_the_turn_off_keeps_them_even_with_no_evicted_original(
    cell_db, captured_minutes: int,
) -> None:
    # Eviction ran on until the store was turned off, so an earlier census can miss what it evicted.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=captured_minutes))
        await fleet.run(NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))

        assert _media_posture(fleet) == {
            "key": True, "credential": True, "egress": True, "active set": True, "offload": False}
    finally:
        await fleet.close()


async def test_an_incomplete_census_is_recorded_as_unknown_never_0_and_keeps_them(cell_db) -> None:
    # A census that skipped a sidecar may have skipped an evicted original.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, evicted=None, evicted_bytes=None, captured_at=NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        await fleet.run(NOW + timedelta(minutes=20))

        record = json.loads((await fleet.rows())[CANARY].custody_observation)["media_store"]
        assert record == {"turned_off_at": (NOW + timedelta(minutes=5)).isoformat(), "evicted": "unknown",
                          "evicted_bytes": "unknown", "captured_at": (NOW + timedelta(minutes=10)).isoformat()}
        assert _media_posture(fleet) == {
            "key": True, "credential": True, "egress": True, "active set": True, "offload": False}
    finally:
        await fleet.close()


async def test_a_later_report_with_no_evicted_original_removes_key_credential_and_egress_and_keeps_the_bundle(
    cell_db,
) -> None:
    # Kept on, a drained cell keeps edge access it no longer needs. Retiring the bundle would make a
    # later return to the edge mint a key that no stored object was written with.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        bundle = (await fleet.rows())[CANARY].media_key_wrapped
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        assert _media_posture(fleet)["key"] is True
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        await fleet.run(NOW + timedelta(minutes=20))

        secret = fleet.cluster.secret(CANARY)
        policy = fleet.cluster.applied[(namespace_name(CANARY), "NetworkPolicy", "runtime-ingress")]
        assert json.loads(secret[MEDIA_STORE_CONFIG_KEY]) == {"vaults": {"/data/vault": {
            "store": f"media-edge:{CANARY}", "enabled": False, "adapter": "media-edge"}}}
        assert not {"media-edge-credential", "media-age-identity", "media-name-key"} & secret.keys()
        assert policy["spec"]["egress"] == []
        assert CANARY not in fleet.cluster.published()["cells"]
        assert (await fleet.rows())[CANARY].media_key_wrapped == bundle
    finally:
        await fleet.close()


async def test_the_edge_access_label_stays_while_served_or_draining_and_leaves_with_the_credential(cell_db) -> None:
    # The edge admits a runtime by this label. Dropped before the credential, a draining cell could no longer
    # read its evicted originals; the set entry leaves last, so the edge honours the credential while it is held.
    fleet = await _fleet(cell_db, CANARY)

    def held() -> dict[str, bool]:
        namespace = fleet.cluster.applied[("", "Namespace", namespace_name(CANARY))]["metadata"]["labels"]
        return {"credential": "media-edge-credential" in fleet.cluster.secret(CANARY),
                "label": namespace.get(MEDIA_EDGE_ACCESS_LABEL) == "true",
                "active set": CANARY in fleet.cluster.published()["cells"]}

    try:
        await fleet.run()
        served = held()
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        draining = held()
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        drained = held()
        await fleet.run(NOW + timedelta(minutes=20))
        retained = held()

        assert served == draining == {"credential": True, "label": True, "active set": True}
        assert drained == {"credential": False, "label": False, "active set": True}
        assert retained == {"credential": False, "label": False, "active set": False}
    finally:
        await fleet.close()


async def test_a_cell_served_again_forgets_its_earlier_turn_off(cell_db) -> None:
    # A report from while it was served again must not count for its next turn-off.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        fleet.config = dataclasses.replace(fleet.config, media_edge=_edge(
            cell_ids=(CANARY,), signing_key=fleet.config.media_edge.signing_key))
        await fleet.run(NOW + timedelta(minutes=10))
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=12))
        await fleet.run(NOW + timedelta(minutes=15))
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=20))
        await fleet.run(NOW + timedelta(minutes=25))

        assert _media_posture(fleet) == {
            "key": True, "credential": True, "egress": True, "active set": True, "offload": False}
    finally:
        await fleet.close()


@pytest.mark.parametrize("turned_off", [True, False], ids=["turned off", "still selected"])
async def test_a_deleted_cell_leaves_the_set_and_retires_its_keys_without_a_report(cell_db, turned_off: bool) -> None:
    # Waiting for a report that a deleted cell never sends would keep its keys and edge access forever.
    # A cell the edge still selects is the case where only the deletion keeps it out of the set.
    fleet = await _fleet(cell_db, CANARY)
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    try:
        await fleet.run()
        if turned_off:
            _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        assert CANARY in fleet.cluster.published()["cells"]
        await app.execute("UPDATE exomem_cloud_cells SET desired_state='deleted' WHERE cell_id=$1", CANARY)
        fleet.cluster.observations[CANARY] = ClusterObservation(namespace_exists=True, namespace_cell_label=CANARY)
        await fleet.run(NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))

        retired = (await fleet.rows())[CANARY]
        assert CANARY not in fleet.cluster.published()["cells"]
        assert retired.media_key_wrapped is None and retired.content_keys_retired_at is not None
        # Deletion removes the namespace first (D9), and the edge access label with it.
        assert namespace_name(CANARY) in fleet.cluster.deleted_namespaces
    finally:
        await app.close()
        await fleet.close()


async def test_a_refused_offload_off_secret_records_no_turn_off(cell_db) -> None:
    # The cell still offloads under its old Secret, so a report it captures next must not count for a drain.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        _turn_off(fleet)
        # Inside a hold cellctl applies the StatefulSet even when the cluster refuses the Secret.
        fleet.cluster.observations[CANARY] = ClusterObservation(
            statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_hold_kind="import",
            statefulset_hold_started_at=NOW)
        apply_all = fleet.cluster.apply_all

        def refuse_the_secret(manifests: list[dict]) -> None:
            if (manifests[0]["kind"], manifests[0]["metadata"]["name"]) == ("Secret", "cell-credentials"):
                raise ApiException(status=422, reason="refused by the test")
            apply_all(manifests)

        fleet.cluster.apply_all = refuse_the_secret
        before = len(fleet.cluster.events)
        await fleet.run(NOW + timedelta(minutes=5))

        assert [event[0] for event in fleet.cluster.events[before:]] == ["apply_statefulset"]
        custody = json.loads((await fleet.rows())[CANARY].custody_observation or "{}")
        assert "media_store" not in custody
    finally:
        await fleet.close()


async def test_one_unreadable_custody_observation_does_not_stop_the_pass_for_every_cell(cell_db) -> None:
    # The active set reads every cell's record before any row's own error boundary.
    fleet = await _fleet(cell_db, CANARY, OTHER, cell_ids=(CANARY, OTHER))
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await fleet.run()
        await owner.execute("UPDATE exomem_cloud_cells SET custody_observation='null'::jsonb WHERE cell_id=$1", OTHER)
        fleet.config = dataclasses.replace(fleet.config, media_edge=_edge(
            cell_ids=(CANARY,), signing_key=fleet.config.media_edge.signing_key))
        await fleet.run(NOW + timedelta(minutes=5))

        # OTHER keeps its entry, as a cell with no readable record does.
        assert set(fleet.cluster.published()["cells"]) == {CANARY, OTHER}
    finally:
        await owner.close()
        await fleet.close()


async def test_with_the_edge_off_cellctl_stops_at_startup_until_every_keyed_cell_has_drained(cell_db, tmp_path) -> None:
    # Started with the edge off, cellctl would take an undrained cell to its store identity alone and
    # leave every evicted original unreadable. Stopping fails the deploy, so the edge stays on.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        edge_off = dataclasses.replace(fleet.config, media_edge=None)

        with pytest.raises(reconcile.MediaStoreNotDrained, match=CANARY):
            await asyncio.wait_for(reconcile.run_loop(
                cell_db.dsn(role="exomem_cellctl"), EdgeCluster(), FakeB2(), FakeHetznerVolumeProvider(),
                _secrets_config(), edge_off, heartbeat_path=str(tmp_path / "heartbeat")), timeout=20)

        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        assert (await reconcile._observe_media_key_columns(fleet.connection, edge_off)).media_edge is None
    finally:
        await fleet.close()


async def test_with_the_edge_turned_off_after_a_drain_a_cell_keeps_its_store_identity_alone(cell_db) -> None:
    # Without its store identity the runtime reads every evicted original as deleted, and its
    # reconcile then removes their derived data. With no edge settings cellctl can sign no credential.
    fleet = await _fleet(cell_db, CANARY)
    try:
        await fleet.run()
        assert "media-edge-credential" in fleet.cluster.secret(CANARY)
        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        # The chart's mediaEdge.enabled=false: no edge settings, on a schema that has the columns.
        fleet.config = dataclasses.replace(fleet.config, media_edge=None, media_key_columns=True)
        await fleet.run(NOW + timedelta(minutes=20))

        namespace = namespace_name(CANARY)
        secret = fleet.cluster.secret(CANARY)
        pod = fleet.cluster.applied[(namespace, "StatefulSet", "cell")]["spec"]["template"]["spec"]
        env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
        mounted = [v["secret"]["items"] for v in pod["volumes"] if v["name"] == "media"]
        policy = fleet.cluster.applied[(namespace, "NetworkPolicy", "runtime-ingress")]

        assert json.loads(secret.get(MEDIA_STORE_CONFIG_KEY, b"null")) == {"vaults": {"/data/vault": {
            "store": f"media-edge:{CANARY}", "enabled": False, "adapter": "media-edge"}}}
        assert not {"media-edge-credential", "media-age-identity", "media-name-key"} & secret.keys()
        assert env.get("EXOMEM_MEDIA_STORE_CONFIG") == "/run/exomem/media/store.json"
        assert not {"EXOMEM_MEDIA_EDGE_URL", "EXOMEM_MEDIA_EDGE_CREDENTIAL_FILE"} & env.keys()
        assert mounted == [[{"key": MEDIA_STORE_CONFIG_KEY, "path": "store.json"}]]
        assert policy["spec"]["egress"] == []
        assert (await fleet.rows())[CANARY].media_key_wrapped is not None
    finally:
        await fleet.close()


async def _drop_media_key_columns(cell_db) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("ALTER TABLE exomem_cloud_cells DROP COLUMN media_key_wrapped, DROP COLUMN media_key_version")
    finally:
        await owner.close()


async def test_with_the_edge_off_a_schema_without_the_media_key_columns_still_serves_and_retires_cells(cell_db) -> None:
    # A Substrate that predates migration 0081 must not stop cellctl while the edge is off.
    await _drop_media_key_columns(cell_db)
    await _seed_cell(cell_db, CANARY, CANARY)
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    cluster, b2 = FakeClusterGateway(), FakeB2()
    try:
        config = await reconcile._observe_media_key_columns(connection, _cluster_config())
        assert config.reads_media_keys is False
        await _converged_cell(connection, cluster, b2, NOW)
        assert (await db.select_all_rows(connection))[0].cell_root_key_wrapped is not None

        await app.execute("UPDATE exomem_cloud_cells SET desired_state='deleted'")
        await reconcile.reconcile_once(connection, cluster, b2, FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=1))
        retired = (await db.select_all_rows(connection))[0]
        assert retired.cell_root_key_wrapped is retired.backup_key_wrapped is None
        assert retired.content_keys_retired_at is not None
    finally:
        await app.close()
        await connection.close()


async def test_with_the_edge_on_cellctl_stops_at_startup_on_a_schema_without_the_media_key_columns(cell_db, tmp_path) -> None:
    # Retrying would leave the fleet unreconciled behind a log line; stopping fails the rollout.
    await _drop_media_key_columns(cell_db)
    config = dataclasses.replace(_cluster_config(), media_edge=_edge())

    with pytest.raises(reconcile.MediaKeyColumnsMissing, match="migration 0081_exomem_cloud_media_keys"):
        await asyncio.wait_for(reconcile.run_loop(
            cell_db.dsn(role="exomem_cellctl"), EdgeCluster(), FakeB2(), FakeHetznerVolumeProvider(),
            _secrets_config(), config, heartbeat_path=str(tmp_path / "heartbeat")), timeout=20)
