"""collect-released-media D3, D5: the backup Job's collector credential and steps, apart from the runtime's."""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta

import asyncpg
import jwt
import pytest

from cellctl import backup_source, db, manifests, media_edge
from cellctl.capacity import GIB
from cellctl.exports import PACK, PAUSE, UPLOAD, ExportRow, export_size
from cellctl.manifests import MEDIA_COLLECTOR_CREDENTIAL_KEY, MEDIA_CREDENTIAL_KEY, namespace_name
from cellctl.state import MediaCollection

from .test_local_storage import SNAPSHOT_ID, _run_backup_job
from .test_media_edge import (
    CANARY,
    NOW,
    NOW_MS,
    OTHER,
    URL,
    _delete,
    _fleet,
    _report,
    _spec,
    _turn_off,
)
from .test_reconcile import FakeClusterGateway, _observed_from_applied, _pass, _seed_cell

RUNTIME_KEYS = {MEDIA_CREDENTIAL_KEY, manifests.MEDIA_IDENTITY_KEY, manifests.MEDIA_NAME_KEY_KEY}
SECRET = "cell-credentials"


def test_an_epoch_bump_revokes_the_collector_credential_with_the_runtime_one() -> None:
    """The operator's one revocation lever: a stolen backup Job credential must stop working with the runtime's."""
    first = media_edge.plan_active_set(media_edge.ActiveSet(), {CANARY: 0}, NOW_MS, collectors={CANARY: 0})
    bumped = media_edge.plan_active_set(first, {CANARY: 1}, NOW_MS, collectors={CANARY: 1})

    assert bumped.collectors[CANARY] > first.collectors[CANARY]
    assert bumped.cells[CANARY] > first.cells[CANARY]


async def test_a_retained_cell_keeps_collecting_and_a_deleted_one_stops(cell_db) -> None:
    """A turned-off store's backups must keep condemning what the cell released, without its runtime credential."""
    fleet = await _fleet(cell_db, CANARY, collect_cell_ids=(CANARY,))
    try:
        await fleet.run()
        served = fleet.cluster.secret(CANARY)
        signing = fleet.config.media_edge.signing_key.public_key()
        claims = jwt.decode(served[MEDIA_COLLECTOR_CREDENTIAL_KEY].decode(), signing, algorithms=["EdDSA"],
                            audience=media_edge.COLLECTOR_AUDIENCE)
        assert claims["sub"] == CANARY and claims["ver"] == fleet.cluster.published()["collectors"][CANARY]

        _turn_off(fleet)
        await fleet.run(NOW + timedelta(minutes=5))
        _report(fleet, evicted=0, evicted_bytes=0, captured_at=NOW + timedelta(minutes=6))
        await fleet.run(NOW + timedelta(minutes=7))
        await fleet.run(NOW + timedelta(minutes=8))
        retained, published = fleet.cluster.secret(CANARY), fleet.cluster.published()
        job_egress = fleet.cluster.applied[(namespace_name(CANARY), "NetworkPolicy", "job-egress")]

        assert CANARY not in published["cells"] and not RUNTIME_KEYS & retained.keys()
        assert published["collectors"] == {CANARY: claims["ver"]}
        assert retained[MEDIA_COLLECTOR_CREDENTIAL_KEY] == served[MEDIA_COLLECTOR_CREDENTIAL_KEY]
        assert job_egress["spec"]["egress"][-1]["ports"] == [{"protocol": "TCP", "port": manifests.MEDIA_EDGE_PORT}]

        await _delete(fleet, CANARY, NOW + timedelta(minutes=10))
        await fleet.run(NOW + timedelta(minutes=15))
        assert "collectors" not in fleet.cluster.published()
    finally:
        await fleet.close()


async def test_only_a_cell_on_the_collect_list_collects_and_taking_it_off_stops_it(cell_db) -> None:
    """The operator's one stop for collection, which the edge-off check waits on: a cell off the list holds none."""
    fleet = await _fleet(cell_db, CANARY, OTHER, cell_ids=(CANARY, OTHER), collect_cell_ids=(CANARY,))
    try:
        await fleet.run()
        assert fleet.cluster.published()["collectors"].keys() == {CANARY}
        assert MEDIA_COLLECTOR_CREDENTIAL_KEY not in fleet.cluster.secret(OTHER)

        fleet.config = dataclasses.replace(fleet.config, media_edge=dataclasses.replace(
            fleet.config.media_edge, collect_cell_ids=()))
        await fleet.run(NOW + timedelta(minutes=5))

        assert "collectors" not in fleet.cluster.published()
        assert MEDIA_COLLECTOR_CREDENTIAL_KEY not in fleet.cluster.secret(CANARY)
        assert CANARY in fleet.cluster.published()["cells"]
    finally:
        await fleet.close()


def _secret_keys(pod: dict) -> set[str]:
    """Every key of the cell's Secret that a pod can read, through its environment or its volumes."""
    keys: set[str] = set()
    for container in [*pod.get("initContainers", []), *pod["containers"]]:
        for entry in container.get("env", []):
            reference = entry.get("valueFrom", {}).get("secretKeyRef", {})
            if reference.get("name") == SECRET:
                keys.add(reference["key"])
        if any(source.get("secretRef", {}).get("name") == SECRET for source in container.get("envFrom", [])):
            keys.add("the whole Secret")
    sources = [volume.get("secret") for volume in pod.get("volumes", [])]
    sources += [source.get("secret") for volume in pod.get("volumes", [])
                for source in volume.get("projected", {}).get("sources", [])]
    for source in sources:
        if source and source.get("secretName", source.get("name")) == SECRET:
            keys |= {item["key"] for item in source["items"]} if "items" in source else {"the whole Secret"}
    return keys


@pytest.mark.parametrize("store", ["served", "retained"])
def test_each_edge_credential_reaches_only_its_own_workload(store: str) -> None:
    """The runtime must never hold the condemn credential, and no Job may hold the runtime's write credential."""
    retained = dict(media_edge_url="", media_edge_credential="", media_age_identity="", media_recipient="",
                    media_name_key=b"", media_key_version=0, media_max_object_bytes=0, media_store_retained=True)
    spec = _spec(media_collector_url=URL, media_collector_credential="collector", hold_kind="backup",
                 hold_started_at=NOW.isoformat(), **(retained if store == "retained" else {}))
    rendered = manifests.render_cell_manifests(spec)
    secret = next(m for m in rendered if m["kind"] == "Secret")["data"]
    runtime = next(m for m in rendered if m["kind"] == "StatefulSet")["spec"]["template"]["spec"]
    store_args = dict(bucket_name="b", endpoint="https://s3.example")
    backup = manifests.render_backup_job(spec, **store_args)["spec"]["template"]["spec"]
    restore = manifests.render_restore_job(spec, **store_args, snapshot_id=SNAPSHOT_ID)["spec"]["template"]["spec"]

    runtime_keys = _secret_keys(runtime)
    assert MEDIA_COLLECTOR_CREDENTIAL_KEY in secret
    # The served runtime does read its own credential, so the reader finds its mounts.
    assert (MEDIA_CREDENTIAL_KEY in runtime_keys) is (store == "served")
    assert not {MEDIA_COLLECTOR_CREDENTIAL_KEY, "the whole Secret"} & runtime_keys
    assert _secret_keys(backup) & (RUNTIME_KEYS | {MEDIA_COLLECTOR_CREDENTIAL_KEY, "the whole Secret"}) == {
        MEDIA_COLLECTOR_CREDENTIAL_KEY}
    assert not _secret_keys(restore) & (RUNTIME_KEYS | {MEDIA_COLLECTOR_CREDENTIAL_KEY, "the whole Secret"})
    backup_env = {entry["name"] for entry in backup["containers"][0]["env"]}
    assert manifests.MEDIA_EDGE_CREDENTIAL_ENV not in backup_env
    assert manifests.MEDIA_COLLECTOR_CREDENTIAL_ENV in backup_env


def _selects(selector: dict, labels: dict) -> bool:
    """A NetworkPolicy podSelector, for matchLabels and the expression operators the cell's policies use."""
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        value = labels.get(expression["key"])
        assert expression["operator"] in ("Exists", "NotIn"), "a selector operator this check does not read"
        if value is None if expression["operator"] == "Exists" else value in expression["values"]:
            return False
    return True


def test_the_media_identity_never_shares_a_pod_with_public_egress() -> None:
    """Defect: the key that decrypts every stored original sits in a pod that can reach the internet, so one
    compromised step sends the store out; the pause beside the live vault gets public egress; or the pack
    loses the key and cannot read evicted originals."""
    spec = _spec(**COLLECTING, hold_kind="export", hold_started_at=NOW.isoformat())
    policies = manifests.render_network_policies(spec)
    item = ExportRow("00000000-0000-4000-8000-000000000001", spec.cell_id, "requested", NOW)
    templates = {
        "runtime": manifests.render_statefulset(spec)["spec"]["template"],
        "backup": manifests.render_backup_job(spec, bucket_name="b", endpoint="https://s3.example")["spec"]["template"],
        **{step: manifests.render_export_job(spec, item, step, deadline_seconds=60, size=export_size(GIB),
                                             part_urls=["https://storage.example/part"])["spec"]["template"]
           for step in (PAUSE, PACK, UPLOAD)},
    }

    def public_peers(name: str) -> list[dict]:
        # A rule without `to`, or with an IP block, reaches beyond the cluster's own pods.
        labels = templates[name]["metadata"]["labels"]
        return [peer for policy in policies if "Egress" in policy["spec"]["policyTypes"]
                and _selects(policy["spec"]["podSelector"], labels)
                for rule in policy["spec"].get("egress", []) for peer in rule.get("to", [{}])
                if not peer or "ipBlock" in peer]

    holders = {name for name, template in templates.items()
               if _secret_keys(template["spec"]) & {manifests.MEDIA_IDENTITY_KEY, "the whole Secret"}}
    assert holders == {"runtime", PACK}
    assert [name for name in templates if public_peers(name)] == ["backup", UPLOAD]
    assert manifests.MEDIA_NAME_KEY_KEY not in _secret_keys(templates[PACK]["spec"])  # the pack names no new object


COLLECTING = dict(media_collector_url=URL, media_collector_credential="collector")
PRUNING = ["--keep-daily", "7"]


def _exomem(tmp_path, *, manifest: str, collect: str) -> str:
    """The cell image's `exomem`, stubbed: it logs each call beside restic's and runs the given shell for two commands."""
    return (f'echo "exomem $*" >> {tmp_path / "calls"}\n'
            f'case "$1" in cell-media-manifest) {manifest} ;; cell-media-collect) {collect} ;; esac')


def test_a_pruning_backup_stores_the_manifest_of_its_source_and_collects_after_the_prune(tmp_path) -> None:
    """A manifest taken after the backup could list a reference the snapshot lacks; a collection before the prune
    would still count the snapshots that the prune removes, and one that misses the snapshot ID condemns nothing."""
    observed, data = _run_backup_job(tmp_path, _exomem(tmp_path, manifest="echo manifest", collect="true"),
                                     retention=PRUNING, **COLLECTING)
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    manifest = tmp_path / "media-manifest.json"
    backup = next(index for index, call in enumerate(calls) if call.startswith("restic backup"))
    forget = next(index for index, call in enumerate(calls) if call.startswith("restic forget"))

    assert observed.backup_job_snapshot_id == SNAPSHOT_ID
    assert calls[0] == f"exomem cell-media-manifest --vault {data}/vault"
    assert calls[backup].split()[-3:] == [f"{data}/vault", f"{data}/host", str(manifest)]
    assert manifest.read_text(encoding="utf-8") == "manifest\n"
    assert backup < forget < len(calls) - 1
    assert calls[-1] == f"exomem cell-media-collect --snapshot {SNAPSHOT_ID} --manifest-path {manifest}"


def test_a_backup_that_does_not_prune_still_stores_the_manifest_and_collects_nothing(tmp_path) -> None:
    """Every retained snapshot needs a manifest: one hourly snapshot without it would stop all collection."""
    _observed, data = _run_backup_job(tmp_path, _exomem(tmp_path, manifest="echo manifest", collect="true"),
                                      **COLLECTING)
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()

    assert next(call for call in calls if call.startswith("restic backup")).endswith(str(tmp_path / "media-manifest.json"))
    assert not [call for call in calls if "cell-media-collect" in call]


@pytest.mark.parametrize(("manifest", "collect"), [
    pytest.param("printf '{\"v\":1'; exit 1", "exit 1", id="the manifest breaks off and the collector fails"),
    pytest.param("echo manifest", "exec sleep 60", id="the collector does not finish"),
])
def test_a_failed_manifest_or_collection_leaves_the_backup_successful(tmp_path, monkeypatch, manifest, collect) -> None:
    """Collection is a side task: a backup that failed with it would leave the cell without a restore point."""
    import time

    monkeypatch.setattr(backup_source, "MEDIA_COLLECT_BY_SECONDS", 5)
    started = time.monotonic()
    observed, _data = _run_backup_job(tmp_path, _exomem(tmp_path, manifest=manifest, collect=collect),
                                      retention=PRUNING, **COLLECTING)
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()

    assert observed.backup_job_snapshot_id == SNAPSHOT_ID
    assert calls[-1].startswith("exomem cell-media-collect")
    assert time.monotonic() - started < 30
    # A collector that wrote no result still leaves a line, with every count unknown.
    assert observed.backup_job_media_collection == MediaCollection("stopped", "no-result", None, None, None)


def test_a_collection_line_that_does_not_parse_is_logged_as_a_warning(tmp_path, caplog) -> None:
    """A cell image that writes the result in another format would otherwise drop the collection without a trace, and
    the operator would read every later hold as one that ran no collection."""
    with caplog.at_level(logging.WARNING, logger="cellctl"):
        observed, _data = _run_backup_job(tmp_path, _exomem(tmp_path, manifest="echo manifest",
                                                            collect="echo 'done: hid two names'"),
                                          retention=PRUNING, **COLLECTING)

    assert observed.backup_job_media_collection is None
    assert "media collection line that does not parse" in caplog.text
    assert "hid two names" not in caplog.text


async def test_the_collectors_result_reaches_cellctls_log_when_the_hold_ends(cell_db, caplog) -> None:
    """The Job's pod, and the collector's own log with it, is gone minutes after the hold. Without this line a stopped
    or unknown collection would leave no trace that the operator can read."""
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    window = datetime(2026, 1, 1, 2, tzinfo=UTC)
    try:
        for _ in range(3):
            await _pass(connection, cluster, window - timedelta(hours=3))
            cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _pass(connection, cluster, window)
        cluster.observations[cell_id] = _observed_from_applied(
            cluster, cell_id, pod_exists=False, pod_uses_volume=False, pod_ready=False,
            backup_job_succeeded=True, backup_job_snapshot_id=SNAPSHOT_ID,
            backup_job_media_collection=MediaCollection("stopped", "edge-outage", 2, 5, None),
        )

        with caplog.at_level(logging.INFO, logger="cellctl"):
            await _pass(connection, cluster, window + timedelta(minutes=1))

        assert (await db.select_all_rows(connection))[0].last_backup_snapshot == SNAPSHOT_ID
        assert (f"cell {cell_id}'s media collection stopped (edge-outage): 2 name(s) hidden of 5 candidate(s), "
                "an unknown number of refused by the edge") in caplog.text
    finally:
        await connection.close()
