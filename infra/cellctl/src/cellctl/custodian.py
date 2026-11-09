"""The key custodian (contain-cloud-controller-compromise D9, task 1.2).

The only process that creates object-store keys. For each cell namespace that has no Secret
`cell-object-store-key`, it creates exactly one B2 key in the cell-data account, restricted to the
locked backup bucket and `cells/<cell_id>/`, and writes the Secret the cell's backup and restore Jobs
read. cellctl never holds a key-creating credential and cannot write that Secret.

It runs from the cellctl image with `python3 -m cellctl.custodian`: its own Deployment, ServiceAccount
and namespace give it the isolation, and a separate image would add a publish and attestation pipeline
without reducing its power. It takes no inbound traffic.

When the provider lists a key for a prefix whose Secret is gone, that key is deleted (its ID read from
the provider's listing) and a new one is created.

It also purges a deleted cell's backups (automate-cell-storage-deletion D2), and only on a deletion grant
that Substrate signed and that verifies here. Once the cell's namespace is gone, it deletes the cell's
keys. After the grant's `purge_after`, it mints a short-lived purge key that can only list and hide in
the cell's prefix, hides every current file, deletes the purge key and marks the cell done in its own
ConfigMap. The bucket's lifecycle rule deletes the hidden versions once their lock lapses.
It logs cell ids and counts, never a key ID or secret.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes import watch
from kubernetes.client.exceptions import ApiException

from .deletion_grant import (
    DELETIONS_CONFIGMAP,
    GRANTS_KEY,
    DeletionGrant,
    read_hold_list,
    read_public_keys,
    verified_grants,
)
from .errors import describe_error
from .manifests import (
    GATEWAY_NAMESPACE,
    OBJECT_STORE_KEY_ID_FIELD,
    OBJECT_STORE_KEY_SECRET,
    OBJECT_STORE_KEY_SECRET_FIELD,
    cell_id_from_namespace,
    namespace_name,
)
from .storage.b2 import B2Config
from .storage.b2_keys import B2KeyStore, B2KeyStoreConfig, B2PrefixHider
from .storage.interface import (
    ObjectStorageKey,
    PrefixHider,
    PrefixKeyStore,
    cell_object_prefix,
    current_names,
)

logger = logging.getLogger(__name__)

# A full sweep after each watch window covers a missed event and a Secret deleted later.
WATCH_SECONDS = 60
RETRY_SECONDS = 10
DEFAULT_HEARTBEAT_PATH = "/tmp/key-custodian-heartbeat"  # the pod's /tmp emptyDir
# The custodian's record of finished purges, in its own namespace where cellctl cannot write, and its key: a
# JSON array of cell IDs. The chart's admission rule for the custodian names the same two strings.
PURGES_CONFIGMAP = "exomem-key-custodian-purges"
DONE_KEY = "done.json"
# Proposed in the design's open values: one hide pass of the largest cell fits in an hour, and a later pass
# replaces an expired key.
PURGE_KEY_SECONDS = 3600
# A pass lists and hides this many times at most; names that still remain wait for the next sweep.
MAX_HIDE_ROUNDS = 5


def _secret_exists(core: k8s.CoreV1Api, namespace: str) -> bool:
    try:
        core.read_namespaced_secret(OBJECT_STORE_KEY_SECRET, namespace)
    except ApiException as error:
        if error.status == 404:
            return False
        raise
    return True


def ensure_cell_key(
    core: k8s.CoreV1Api, key_store: PrefixKeyStore, namespace: k8s.V1Namespace, granted: Collection[str] = (),
) -> bool:
    """Creates the cell's key and Secret if the Secret is missing. Returns whether it did. A cell that a
    valid deletion grant names gets no key, even when its namespace appears again."""

    name = namespace.metadata.name
    cell_id = cell_id_from_namespace(name)
    # A deleting namespace refuses the Secret, so a key made for it would be stranded.
    if cell_id is None or namespace.metadata.deletion_timestamp is not None or cell_id in granted:
        return False
    if _secret_exists(core, name):
        return False
    # A key listed for this prefix with no Secret to hold it is unusable: nothing has its secret.
    stale = key_store.prefix_key_ids(cell_id)
    for key_id in stale:
        key_store.delete_key(key_id)
    created = key_store.create_prefix_key(cell_id)
    core.create_namespaced_secret(
        name,
        k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(name=OBJECT_STORE_KEY_SECRET, namespace=name),
            type="Opaque",
            string_data={
                OBJECT_STORE_KEY_ID_FIELD: created.key_id,
                OBJECT_STORE_KEY_SECRET_FIELD: created.key_secret,
            },
        ),
    )
    logger.info("key custodian: cell %s has a new object-store key (replaced %d unusable)", cell_id, len(stale))
    return True


def _ensure_each(
    core: k8s.CoreV1Api, key_store: PrefixKeyStore, namespaces, beat: Callable[[], None],
    granted: Collection[str] = (),
) -> None:
    """One namespace's failure never stops the others: the next sweep tries it again. `beat` runs
    after every attempt, so a provider outage that fails each namespace in turn never ages the
    heartbeat; only a call that hangs past its own timeout does."""

    for namespace in namespaces:
        try:
            ensure_cell_key(core, key_store, namespace, granted)
        except Exception as error:  # noqa: BLE001 - a provider or API error is retried by the next sweep
            logger.error("key custodian: cell namespace %s not served: %s",
                         namespace.metadata.name, describe_error(error))
        beat()


@dataclass(frozen=True)
class PurgeConfig:
    # Mounted files of the chart-owned policy ConfigMap, read on each sweep.
    grant_keys_path: Path
    hold_path: Path
    # Where cellctl relays the grants, and the custodian's own namespace for its done marks.
    deletions_namespace: str = GATEWAY_NAMESPACE
    own_namespace: str = "exomem-key-custody"
    purge_key_seconds: int = PURGE_KEY_SECONDS


@dataclass
class Purger:
    """automate-cell-storage-deletion D2: the backup purge of each cell that a valid grant names."""

    core: k8s.CoreV1Api
    key_store: PrefixKeyStore
    hider_for: Callable[[ObjectStorageKey], PrefixHider]
    config: PurgeConfig
    clock: Callable[[], float] = time.time
    # The valid grants from the last relayed list that could be read. A failed read keeps them, so a cell
    # that a grant names never gets a new cell key because one read failed.
    grants: dict[str, DeletionGrant] = field(default_factory=dict)
    # Cells whose keys a listing showed deleted in this process. Only this process creates keys, and it
    # creates none for a granted cell, so the key listing for such a cell repeats only after a restart.
    _cleared: set[str] = field(default_factory=set)

    def refresh(self) -> bool:
        """Reads the relayed grants. Returns whether the read succeeded."""

        keys = read_public_keys(self.config.grant_keys_path)
        if keys is None:
            return False
        try:
            data = self.core.read_namespaced_config_map(DELETIONS_CONFIGMAP, self.config.deletions_namespace).data
        except ApiException as error:
            if error.status != 404:
                logger.error("key custodian: deletion grants not read: %s", describe_error(error))
                return False
            data = None
        self.grants = verified_grants((data or {}).get(GRANTS_KEY), keys)
        return True

    def _namespace_exists(self, cell_id: str) -> bool:
        # A direct GET, never a cache: a namespace that appears again stops the purge at once.
        try:
            self.core.read_namespace(namespace_name(cell_id))
        except ApiException as error:
            if error.status == 404:
                return False
            raise
        return True

    def _delete_prefix_keys(self, cell_id: str) -> int:
        stale = self.key_store.prefix_key_ids(cell_id)
        for key_id in stale:
            self.key_store.delete_key(key_id)
        self._cleared.add(cell_id)
        return len(stale)

    def _read_done(self) -> tuple[set[str], bool]:
        """The cells marked done, and whether the ConfigMap exists."""

        try:
            data = self.core.read_namespaced_config_map(PURGES_CONFIGMAP, self.config.own_namespace).data
        except ApiException as error:
            if error.status == 404:
                return set(), False
            raise
        value = json.loads((data or {}).get(DONE_KEY) or "[]")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("the done marks are malformed")
        return set(value), True

    def _write_done(self, done: set[str], existed: bool) -> None:
        body = k8s.V1ConfigMap(
            metadata=k8s.V1ObjectMeta(name=PURGES_CONFIGMAP, namespace=self.config.own_namespace),
            data={DONE_KEY: json.dumps(sorted(done))},
        )
        if existed:
            self.core.replace_namespaced_config_map(PURGES_CONFIGMAP, self.config.own_namespace, body)
        else:
            self.core.create_namespaced_config_map(self.config.own_namespace, body)

    def _hide_all(self, hider: PrefixHider, cell_id: str) -> bool:
        """Hides every current name under the prefix, listing again until none remains. Returns whether a
        listing found no current name."""

        prefix = cell_object_prefix(cell_id)
        hidden = 0
        for _ in range(MAX_HIDE_ROUNDS):
            names = current_names(hider.iter_object_versions(prefix))
            if not names:
                logger.info("key custodian: cell %s has no current backup file (%d hidden in this pass)",
                            cell_id, hidden)
                return True
            for name in names:
                hider.hide_file(name)
                hidden += 1
        logger.warning("key custodian: cell %s still lists current backup files after %d hides", cell_id, hidden)
        return False

    def _purge(self, grant: DeletionGrant, done: set[str]) -> bool:
        """One cell's step. Returns whether it marked the cell done."""

        cell_id = grant.cell_id
        if self._namespace_exists(cell_id):
            return False
        # The cell's key, and at most one key per prefix: a stale purge key that a crashed pass left goes too.
        if cell_id not in self._cleared:
            deleted = self._delete_prefix_keys(cell_id)
            if deleted:
                logger.info("key custodian: deleted cell %s: %d object-store keys deleted", cell_id, deleted)
        if self.clock() < grant.purge_after or cell_id in done:
            return False
        purge_key = self.key_store.create_purge_key(cell_id, self.config.purge_key_seconds)
        # Until the purge key is deleted, the next sweep lists the prefix's keys again.
        self._cleared.discard(cell_id)
        try:
            finished = self._hide_all(self.hider_for(purge_key), cell_id)
        finally:
            self.key_store.delete_key(purge_key.key_id)
        self._cleared.add(cell_id)
        if finished:
            done.add(cell_id)
        return finished

    def sweep(self, beat: Callable[[], None], *, grants_read: bool) -> None:
        """One purge step for each granted cell that is not held. One cell's failure never stops another."""

        held = read_hold_list(self.config.hold_path)
        if held is None:
            logger.error("key custodian: the hold list cannot be read; no cell is purged until it can")
            return
        try:
            done, existed = self._read_done()
        except (ApiException, ValueError) as error:
            # A lost mark costs one more check, but an unread one could mint a key for a finished purge.
            logger.error("key custodian: done marks not read; no cell is purged: %s", describe_error(error))
            return
        before = set(done)
        for grant in self.grants.values():
            if grant.cell_id in held:
                continue
            try:
                self._purge(grant, done)
            except Exception as error:  # noqa: BLE001 - a provider or API error is retried by the next sweep
                logger.error("key custodian: deleted cell %s not purged: %s", grant.cell_id, describe_error(error))
            beat()
        if grants_read:
            # A mark goes with its grant: cellctl drops a grant only once the provider shows no data left.
            done &= set(self.grants)
        if done != before:
            try:
                self._write_done(done, existed)
            except ApiException as error:
                logger.error("key custodian: done marks not written: %s", describe_error(error))


def serve(
    core: k8s.CoreV1Api, key_store: PrefixKeyStore, heartbeat_path: str, *, purger: Purger | None = None,
    stop=lambda: False,
) -> None:
    """Sweeps every namespace and every deletion grant, then watches for new namespaces until the window
    ends, forever."""

    def beat() -> None:
        Path(heartbeat_path).touch()

    def granted() -> Collection[str]:
        return purger.grants if purger is not None else ()

    while not stop():
        try:
            listing = core.list_namespace()
            beat()
            grants_read = purger.refresh() if purger is not None else False
            _ensure_each(core, key_store, listing.items, beat, granted())
            if purger is not None:
                purger.sweep(beat, grants_read=grants_read)
            for event in watch.Watch().stream(
                core.list_namespace,
                resource_version=listing.metadata.resource_version,
                timeout_seconds=WATCH_SECONDS,
            ):
                beat()
                if event["type"] == "ADDED":
                    _ensure_each(core, key_store, [event["object"]], beat, granted())
        except Exception as error:  # noqa: BLE001 - a lost watch or API outage restarts the sweep
            logger.error("key custodian: sweep failed, retrying: %s", describe_error(error))
            time.sleep(RETRY_SECONDS)


def run() -> None:
    logging.basicConfig(level=os.environ.get("KEY_CUSTODIAN_LOG_LEVEL", "INFO"))
    # SR-L6: at DEBUG the kubernetes client logs response bodies, including the Secret this process creates.
    logging.getLogger("kubernetes.client.rest").setLevel(logging.WARNING)
    k8s_config.load_incluster_config()
    bucket_id = os.environ["KEY_CUSTODIAN_BACKUP_BUCKET_ID"]
    key_store = B2KeyStore(
        B2KeyStoreConfig(
            key_id=os.environ["KEY_CUSTODIAN_B2_KEY_ID"],
            application_key=os.environ["KEY_CUSTODIAN_B2_APPLICATION_KEY"],
            bucket_id=bucket_id,
        )
    )
    core = k8s.CoreV1Api()
    purger = Purger(
        core,
        key_store,
        lambda key: B2PrefixHider(B2Config(key_id=key.key_id, application_key=key.key_secret, bucket_id=bucket_id)),
        PurgeConfig(
            grant_keys_path=Path(os.environ["KEY_CUSTODIAN_GRANT_PUBLIC_KEYS_FILE"]),
            hold_path=Path(os.environ["KEY_CUSTODIAN_HOLD_LIST_FILE"]),
            deletions_namespace=os.environ["KEY_CUSTODIAN_DELETIONS_NAMESPACE"],
            own_namespace=os.environ["KEY_CUSTODIAN_NAMESPACE"],
        ),
    )
    serve(core, key_store, os.environ.get("KEY_CUSTODIAN_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH), purger=purger)


if __name__ == "__main__":
    run()
