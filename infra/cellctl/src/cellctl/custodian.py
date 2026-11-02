"""The key custodian (contain-cloud-controller-compromise D9, task 1.2).

The only process that creates object-store keys. For each cell namespace that has no Secret
`cell-object-store-key`, it creates exactly one B2 key in the cell-data account, restricted to the
locked backup bucket and `cells/<cell_id>/`, and writes the Secret the cell's backup and restore Jobs
read. cellctl never holds a key-creating credential and cannot write that Secret.

It runs from the cellctl image with `python3 -m cellctl.custodian`: its own Deployment, ServiceAccount
and namespace give it the isolation, and a separate image would add a publish and attestation pipeline
without reducing its power. It takes no inbound traffic.

The one deletion it makes: when the provider lists a key for a prefix whose Secret is gone, that key
is deleted (its ID read from the provider's listing) and a new one is created. It deletes no key and
hides no file for any other reason; removing a deleted cell's key is the operator's script.
It logs cell ids and counts, never a key ID or secret.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes import watch
from kubernetes.client.exceptions import ApiException

from .errors import describe_error
from .manifests import (
    OBJECT_STORE_KEY_ID_FIELD,
    OBJECT_STORE_KEY_SECRET,
    OBJECT_STORE_KEY_SECRET_FIELD,
    cell_id_from_namespace,
)
from .storage.b2_keys import B2KeyStore, B2KeyStoreConfig
from .storage.interface import PrefixKeyStore

logger = logging.getLogger(__name__)

# A full sweep after each watch window covers a missed event and a Secret deleted later.
WATCH_SECONDS = 60
RETRY_SECONDS = 10
DEFAULT_HEARTBEAT_PATH = "/tmp/key-custodian-heartbeat"  # the pod's /tmp emptyDir


def _secret_exists(core: k8s.CoreV1Api, namespace: str) -> bool:
    try:
        core.read_namespaced_secret(OBJECT_STORE_KEY_SECRET, namespace)
    except ApiException as error:
        if error.status == 404:
            return False
        raise
    return True


def ensure_cell_key(core: k8s.CoreV1Api, key_store: PrefixKeyStore, namespace: k8s.V1Namespace) -> bool:
    """Creates the cell's key and Secret if the Secret is missing. Returns whether it did."""

    name = namespace.metadata.name
    cell_id = cell_id_from_namespace(name)
    # A deleting namespace refuses the Secret, so a key made for it would be stranded.
    if cell_id is None or namespace.metadata.deletion_timestamp is not None:
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


def _ensure_each(core: k8s.CoreV1Api, key_store: PrefixKeyStore, namespaces, beat: Callable[[], None]) -> None:
    """One namespace's failure never stops the others: the next sweep tries it again. `beat` runs
    after every attempt, so a provider outage that fails each namespace in turn never ages the
    heartbeat; only a call that hangs past its own timeout does."""

    for namespace in namespaces:
        try:
            ensure_cell_key(core, key_store, namespace)
        except Exception as error:  # noqa: BLE001 - a provider or API error is retried by the next sweep
            logger.error("key custodian: cell namespace %s not served: %s",
                         namespace.metadata.name, describe_error(error))
        beat()


def serve(core: k8s.CoreV1Api, key_store: PrefixKeyStore, heartbeat_path: str, *, stop=lambda: False) -> None:
    """Sweeps every namespace, then watches for new ones until the window ends, forever."""

    def beat() -> None:
        Path(heartbeat_path).touch()

    while not stop():
        try:
            listing = core.list_namespace()
            beat()
            _ensure_each(core, key_store, listing.items, beat)
            for event in watch.Watch().stream(
                core.list_namespace,
                resource_version=listing.metadata.resource_version,
                timeout_seconds=WATCH_SECONDS,
            ):
                beat()
                if event["type"] == "ADDED":
                    _ensure_each(core, key_store, [event["object"]], beat)
        except Exception as error:  # noqa: BLE001 - a lost watch or API outage restarts the sweep
            logger.error("key custodian: sweep failed, retrying: %s", describe_error(error))
            time.sleep(RETRY_SECONDS)


def run() -> None:
    logging.basicConfig(level=os.environ.get("KEY_CUSTODIAN_LOG_LEVEL", "INFO"))
    # SR-L6: at DEBUG the kubernetes client logs response bodies, including the Secret this process creates.
    logging.getLogger("kubernetes.client.rest").setLevel(logging.WARNING)
    k8s_config.load_incluster_config()
    key_store = B2KeyStore(
        B2KeyStoreConfig(
            key_id=os.environ["KEY_CUSTODIAN_B2_KEY_ID"],
            application_key=os.environ["KEY_CUSTODIAN_B2_APPLICATION_KEY"],
            bucket_id=os.environ["KEY_CUSTODIAN_BACKUP_BUCKET_ID"],
        )
    )
    serve(
        k8s.CoreV1Api(),
        key_store,
        os.environ.get("KEY_CUSTODIAN_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH),
    )


if __name__ == "__main__":
    run()
