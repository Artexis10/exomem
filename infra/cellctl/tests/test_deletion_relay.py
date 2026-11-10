"""automate-cell-storage-deletion D1: cellctl relays each deletion grant into the ConfigMap that the key
custodian and the media edge read, and drops a grant only once the list-only views of both buckets show no
`upload` and no `start` entry under the cell's prefix."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cellctl import reconcile
from cellctl.deletion_grant import GRANTS_KEY
from cellctl.deletion_relay import RELAY_RECHECK, RelayMemory, relay_grants
from cellctl.manifests import namespace_name
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .conftest import CellDatabase
from .test_reconcile import FakeClusterGateway, _cluster_config, _secrets_config, _seed_cell

CELL = "aaaaaaaaaaaaaaaa"
LIVE = "bbbbbbbbbbbbbbbb"
SUBSTRATE = Ed25519PrivateKey.generate()
IAT = datetime(2026, 1, 1, tzinfo=UTC)
PURGE_AFTER = IAT + timedelta(days=7)


def _grant(cell_id: str, *, iat: datetime = IAT, purge_after: datetime = PURGE_AFTER) -> str:
    return jwt.encode({"aud": "exomem-cell-delete", "iat": int(iat.timestamp()), "jti": f"grant-{cell_id}",
                       "cell_id": cell_id, "purge_after": int(purge_after.timestamp())}, SUBSTRATE, algorithm="EdDSA")


def _relayed(cluster: FakeClusterGateway) -> list[str]:
    """The cells whose grants the ConfigMap holds, read from each token's own claims."""
    if cluster.cell_deletions is None:
        return []
    return [jwt.decode(token, options={"verify_signature": False})["cell_id"]
            for token in json.loads(cluster.cell_deletions[GRANTS_KEY])]


class Relay:
    """The relay alone, after Substrate stored a grant for CELL, on a platform with both buckets."""

    def __init__(self, *, stored: list[object] | None = None, media_bucket: bool = True) -> None:
        self.cluster = FakeClusterGateway()
        self.memory = RelayMemory()
        self.backups, self.media = FakeB2(), FakeB2()
        self.media_listing: FakeB2 | None = self.media
        self.stored = [_grant(CELL)] if stored is None else stored
        self.media_bucket = media_bucket

    def run(self, now: datetime) -> None:
        relay_grants(self.cluster, self.stored, now, self.memory, backup_listing=self.backups,
                     media_listing=self.media_listing, media_bucket=self.media_bucket)


@pytest.mark.parametrize(("bucket", "action"), [
    ("backups", "upload"), ("backups", "start"), ("media", "upload"), ("media", "start"),
])
def test_a_grant_stays_while_either_bucket_shows_an_upload_or_start_entry_and_goes_once_neither_does(
    bucket: str, action: str,
) -> None:
    """Defect: the relay drops a grant while a bucket still holds the cell's data, so its executor stops before
    the purge is done; or it reads only current names, and an unfinished large file's `start` entry is missed."""
    relay = Relay()
    getattr(relay, bucket).seed_object(f"cells/{CELL}/data/1", action=action)

    relay.run(PURGE_AFTER)
    assert _relayed(relay.cluster) == [CELL]

    # The provider deleted the version; only the hide marker is left.
    setattr(relay, bucket, FakeB2())
    getattr(relay, bucket).seed_object(f"cells/{CELL}/data/1", action="hide")
    relay.media_listing = relay.media
    relay.run(PURGE_AFTER + RELAY_RECHECK)
    assert _relayed(relay.cluster) == []


def test_a_grant_stays_through_the_grace_even_while_both_buckets_are_empty() -> None:
    """Defect: the relay drops the grant of a cell that never stored a file before `purge_after`, and the
    custodian never deletes the cell's key, which it does on the grant once the namespace is gone."""
    relay = Relay()

    relay.run(PURGE_AFTER - timedelta(seconds=1))

    assert _relayed(relay.cluster) == [CELL]
    assert relay.backups.listed == relay.media.listed == []


@pytest.mark.parametrize(("case", "stays"), [
    ("backup listing fails", True),
    ("media list key missing", True),
    ("no media bucket", False),
])
def test_a_grant_goes_only_on_listings_that_prove_both_buckets_empty(case: str, stays: bool) -> None:
    """Defect: a failed listing, or a missing media list key, reads as an empty bucket and drops a grant
    whose purge is not done; or a platform without a media bucket keeps every grant for ever."""
    relay = Relay(media_bucket=case != "no media bucket")
    relay.run(PURGE_AFTER - timedelta(days=1))
    if case == "backup listing fails":
        relay.backups.fail_listing_with = OSError("provider down")
    else:
        relay.media_listing = None

    relay.run(PURGE_AFTER)

    assert _relayed(relay.cluster) == ([CELL] if stays else [])


def test_a_lost_config_map_gets_back_the_grant_of_a_cell_that_still_holds_data_and_only_that_one() -> None:
    """Defect: once the ConfigMap is lost, a cell whose purge is not done is never relayed again; or every
    finished deletion is relayed again and its executor lists it for nothing."""
    finished = "cccccccccccccccc"
    relay = Relay(stored=[_grant(CELL), _grant(finished)])
    relay.backups.seed_object(f"cells/{CELL}/restic/data/1", action="upload")

    relay.run(PURGE_AFTER)

    assert _relayed(relay.cluster) == [CELL]


def test_a_stored_grant_that_no_executor_could_use_is_skipped_and_the_one_beside_it_is_relayed() -> None:
    """Defect: one malformed grant in the database stops the relay of every grant."""
    unsigned_cell = jwt.encode({"purge_after": int(PURGE_AFTER.timestamp())}, SUBSTRATE, algorithm="EdDSA")
    relay = Relay(stored=["not-a-token", unsigned_cell, _grant(CELL)])

    relay.run(PURGE_AFTER - timedelta(days=1))

    assert _relayed(relay.cluster) == [CELL]


async def _store_grant(cell_db: CellDatabase, cell_id: str, grant: str) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("UPDATE exomem_cloud_cells SET deletion_grant = $1 WHERE cell_id = $2", grant, cell_id)
    finally:
        await owner.close()


async def test_a_pass_relays_a_deleted_cells_grant_until_its_prefix_is_empty_and_none_for_a_live_cell(
    cell_db: CellDatabase,
) -> None:
    """Defect: the relay misses the database's grants, relays one for a live cell, or never drops a grant
    whose purge is done."""
    await _seed_cell(cell_db, LIVE, "tenant-live")
    await _seed_cell(cell_db, CELL, "tenant-deleted", desired_state="deleted")
    await _store_grant(cell_db, CELL, _grant(CELL))
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster, memory, backups = FakeClusterGateway(), reconcile.LoopMemory(), FakeB2()
    config = dataclasses.replace(_cluster_config(), deletion_grant_column=True)
    backups.seed_object(f"cells/{CELL}/restic/data/1", action="upload")

    async def pass_at(now: datetime) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config,
                                       now=now, memory=memory, backup_listing=backups)

    try:
        await pass_at(IAT + timedelta(days=1))
        assert _relayed(cluster) == [CELL]
        assert (namespace_name(LIVE), "StatefulSet", "cell") in cluster.applied

        await pass_at(PURGE_AFTER)
        assert _relayed(cluster) == [CELL]

        backups = FakeB2()  # the provider deleted the hidden version; its hide marker is left
        backups.seed_object(f"cells/{CELL}/restic/data/1", action="hide")
        await pass_at(PURGE_AFTER + RELAY_RECHECK)
        assert _relayed(cluster) == []
    finally:
        await connection.close()


async def test_run_loop_finds_the_grant_column_and_relays_with_the_listings_main_passes(cell_db: CellDatabase) -> None:
    """Defect: cellctl never sees Substrate's grant column, so no grant is relayed; or run_loop refuses the
    media listing that main passes, and cellctl stops at start."""
    await _seed_cell(cell_db, CELL, "tenant-deleted", desired_state="deleted")
    now = datetime.now(UTC)
    await _store_grant(cell_db, CELL, _grant(CELL, iat=now, purge_after=now + timedelta(days=7)))
    cluster = FakeClusterGateway()
    task = asyncio.create_task(reconcile.run_loop(
        cell_db.dsn(role="exomem_cellctl"), cluster, FakeHetznerVolumeProvider(), _secrets_config(),
        _cluster_config(), backup_listing=FakeB2(), media_listing=FakeB2()))
    try:
        deadline = time.monotonic() + 5
        while cluster.cell_deletions is None and time.monotonic() < deadline and not task.done():
            await asyncio.sleep(0.05)
        if task.done():
            task.result()
        assert _relayed(cluster) == [CELL]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
