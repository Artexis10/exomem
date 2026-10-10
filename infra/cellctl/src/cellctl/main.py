"""cellctl's process entrypoint: reads configuration from the pod
environment (no .env file, matching the cloud-mode convention) and runs the
D4 reconcile loop against a real cluster."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from datetime import timedelta

from kubernetes import client as k8s
from kubernetes import config as k8s_config

from .backup_window import parse_backup_window
from .capacity import CapacityConfig, SharedWorkerPolicy
from .decide import DEFAULT_RECONCILE_CONFIG, ReconcileConfig
from .k8s_client import ClusterClient
from .manifests import ResourceSettings, check_model_env
from .reconcile import ClusterConfig, SecretsConfig, run_loop
from .storage.b2 import B2Config, B2ObjectStorage
from .storage.hetzner import HetznerVolumeProvider
from .storage_config import DEFAULT_STORAGE, LEGACY_CLASS, LocalStorage, StorageConfig

# cellctl's own cell resources when CELLCTL_CELL_* is unset (the chart always
# sets them); manifests.py owns the values.
DEFAULT_RESOURCES = ResourceSettings()


def _versioned_keys_env(name: str) -> dict[int, bytes]:
    """`name` holds JSON: {"1": "<base64>", "2": "<base64>", ...}."""

    raw = json.loads(os.environ[name])
    return {int(version): base64.b64decode(value) for version, value in raw.items()}


_CELL_TOKEN_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")


def _cell_token_key(name: str, raw: str) -> bytes:
    """D7: the cell token key is 32 bytes as 64 hex characters, the encoding
    the Substrate gateway reads from the same Secret entry. Refused at
    settings load otherwise, never silently re-encoded."""

    value = raw.strip()
    if not _CELL_TOKEN_KEY_RE.fullmatch(value):
        raise ValueError(f"{name} must be 64 hex characters (a 32-byte key)")
    return bytes.fromhex(value)


def build_secrets_config() -> SecretsConfig:
    previous_raw = os.environ.get("CELLCTL_CELL_TOKEN_KEY_PREVIOUS")
    previous_version_raw = os.environ.get("CELLCTL_CELL_TOKEN_KEY_PREVIOUS_VERSION")
    return SecretsConfig(
        cell_token_key_current=_cell_token_key(
            "CELLCTL_CELL_TOKEN_KEY_CURRENT", os.environ["CELLCTL_CELL_TOKEN_KEY_CURRENT"]
        ),
        cell_token_key_previous=(
            _cell_token_key("CELLCTL_CELL_TOKEN_KEY_PREVIOUS", previous_raw) if previous_raw else None
        ),
        cell_token_key_version=int(os.environ["CELLCTL_CELL_TOKEN_KEY_VERSION"]),
        cell_token_key_previous_version=int(previous_version_raw) if previous_version_raw else None,
        backup_master_keys=_versioned_keys_env("CELLCTL_BACKUP_MASTER_KEYS"),
        backup_master_key_current_version=int(os.environ["CELLCTL_BACKUP_MASTER_KEY_CURRENT_VERSION"]),
    )


def build_reconcile_config() -> ReconcileConfig:
    # D3/D6: the init/upgrade-readiness deadline stays a 10-minute default
    # (not pinned exactly by the design), but is configurable per the
    # coordinator's ruling rather than hardcoded.
    kwargs: dict[str, object] = {}
    minutes_raw = os.environ.get("CELLCTL_INIT_DEADLINE_MINUTES")
    if minutes_raw is not None:
        kwargs["init_deadline"] = timedelta(minutes=int(minutes_raw))
    window_raw = os.environ.get("CELLCTL_BACKUP_WINDOW")  # "2-5"
    if window_raw is not None:
        kwargs["backup_window"] = parse_backup_window(window_raw)
    concurrency_raw = os.environ.get("CELLCTL_BACKUP_CONCURRENCY")
    if concurrency_raw is not None:
        kwargs["backup_concurrency"] = int(concurrency_raw)
    if not kwargs:
        return DEFAULT_RECONCILE_CONFIG
    return ReconcileConfig(**kwargs)


def build_storage_config() -> StorageConfig:
    """CELLCTL_CELL_STORAGE holds {"domain": <class>, "local": {LocalStorage fields}};
    unset means Hetzner volumes only, as before local storage."""

    raw = os.environ.get("CELLCTL_CELL_STORAGE")
    if not raw:
        return DEFAULT_STORAGE
    value = json.loads(raw)
    if not isinstance(value, dict) or not set(value) <= {"domain", "local"} or not isinstance(value.get("local"), dict):
        raise ValueError("cell storage must be a JSON object with a local object and an optional domain")
    return StorageConfig(domain=value.get("domain", LEGACY_CLASS), local=LocalStorage(**value["local"]))


def build_alert_delivery_secret() -> tuple[str, str, str] | None:
    """CELLCTL_ALERT_DELIVERY_SECRET is `namespace/name/key` of the platform's
    alert-delivery Secret (task 2.8); unset leaves the backup-age alert to the log."""

    raw = os.environ.get("CELLCTL_ALERT_DELIVERY_SECRET")
    if not raw:
        return None
    parts = tuple(raw.split("/"))
    if len(parts) != 3 or not all(parts):
        raise ValueError("the alert delivery secret must be namespace/name/key")
    return parts


def build_cluster_config() -> ClusterConfig:
    model_env_raw = os.environ.get("CELLCTL_CELL_MODEL_ENV")
    job_egress_except_raw = os.environ.get("CELLCTL_JOB_EGRESS_EXCEPT")
    attachments_limit_fallback_raw = os.environ.get("CELLCTL_ATTACHMENTS_LIMIT_FALLBACK")
    model_env = json.loads(model_env_raw) if model_env_raw else {}
    artifact_cell_ids = json.loads(os.environ.get("CELLCTL_ARTIFACT_BROKER_CELL_IDS", "[]"))
    if not isinstance(artifact_cell_ids, list):
        raise ValueError("artifact broker cell IDs must be a JSON array")
    dedicated_cell_ids = json.loads(os.environ.get("CELLCTL_DEDICATED_CELL_IDS", "[]"))
    if not isinstance(dedicated_cell_ids, list):
        raise ValueError("dedicated cell IDs must be a JSON array")
    media_raw = json.loads(os.environ.get("CELLCTL_MEDIA_ENGINE_CELL_IDS", "{}"))
    if not isinstance(media_raw, dict) or not all(isinstance(ids, list) for ids in media_raw.values()):
        raise ValueError("media engine cell IDs must be a JSON object of engine to cell ID array")
    media_engine_cell_ids = {engine: tuple(ids) for engine, ids in media_raw.items()}
    shared_raw = json.loads(os.environ.get("CELLCTL_SHARED_WORKER", '{"mode":"off"}'))
    if not isinstance(shared_raw, dict):
        raise ValueError("shared worker policy must be a JSON object")
    shared = None
    if shared_raw.get("mode") != "off":
        if not isinstance(shared_raw.get("cell_ids", []), list):
            raise ValueError("shared worker cell IDs must be a JSON array")
        shared_raw["resources"] = ResourceSettings(**shared_raw["resources"])
        shared_raw["cell_ids"] = tuple(shared_raw.get("cell_ids", []))
        shared = SharedWorkerPolicy(**shared_raw)
    elif set(shared_raw) != {"mode"}:
        raise ValueError("off shared worker policy accepts only mode")
    # A bad chart value fails cellctl at startup, not on every render.
    check_model_env(model_env)
    return ClusterConfig(
        object_storage_bucket=os.environ["CELLCTL_B2_BUCKET_NAME"],
        object_storage_endpoint=os.environ["CELLCTL_B2_ENDPOINT"],
        resources=ResourceSettings(
            cpu_request=os.environ.get("CELLCTL_CELL_CPU_REQUEST", DEFAULT_RESOURCES.cpu_request),
            cpu_limit=os.environ.get("CELLCTL_CELL_CPU_LIMIT", DEFAULT_RESOURCES.cpu_limit),
            memory_request=os.environ.get("CELLCTL_CELL_MEMORY_REQUEST", DEFAULT_RESOURCES.memory_request),
            memory_limit=os.environ.get("CELLCTL_CELL_MEMORY_LIMIT", DEFAULT_RESOURCES.memory_limit),
        ),
        model_env=model_env,
        dedicated_cell_ids=tuple(dedicated_cell_ids),
        shared_worker=shared,
        artifact_broker_url=os.environ.get("CELLCTL_ARTIFACT_BROKER_URL", ""),
        artifact_broker_cell_ids=tuple(artifact_cell_ids),
        media_engine_cell_ids=media_engine_cell_ids,
        job_egress_except=tuple(job_egress_except_raw.split(",")) if job_egress_except_raw else (),
        storage=build_storage_config(),
        alert_delivery_secret=build_alert_delivery_secret(),
        capacity=CapacityConfig(
            csi_driver=os.environ.get("CELLCTL_CSI_DRIVER", "csi.hetzner.cloud"),
            headroom=int(os.environ.get("CELLCTL_ATTACHMENTS_HEADROOM", "5")),
            attachments_limit_fallback=(
                int(attachments_limit_fallback_raw) if attachments_limit_fallback_raw else None
            ),
        ),
    )


def run() -> None:
    logging.basicConfig(level=os.environ.get("CELLCTL_LOG_LEVEL", "INFO"))
    # SR-L6: at DEBUG, the kubernetes client logs full response bodies,
    # including the server-side apply response for a cell Secret -- which
    # carries that cell's bearer, restic password and B2 secret. Pinned to
    # WARNING regardless of the root level.
    logging.getLogger("kubernetes.client.rest").setLevel(logging.WARNING)

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        k8s_config.load_incluster_config()
    else:
        k8s_config.load_kube_config()

    cluster_config = build_cluster_config()
    cluster = ClusterClient(k8s.ApiClient(), storage_config=cluster_config.storage)
    object_storage = B2ObjectStorage(
        B2Config(
            key_management_key_id=os.environ["CELLCTL_B2_KEY_MANAGEMENT_KEY_ID"],
            key_management_application_key=os.environ["CELLCTL_B2_KEY_MANAGEMENT_APPLICATION_KEY"],
            bucket_id=os.environ["CELLCTL_B2_BUCKET_ID"],
            account_id=os.environ["CELLCTL_B2_ACCOUNT_ID"],
        )
    )
    volume_provider = HetznerVolumeProvider(read_only_token=os.environ["CELLCTL_HETZNER_READ_TOKEN"])

    asyncio.run(
        run_loop(
            os.environ["CELLCTL_DATABASE_DSN"],
            cluster,
            object_storage,
            volume_provider,
            build_secrets_config(),
            cluster_config,
            config=build_reconcile_config(),
            heartbeat_path=os.environ.get("CELLCTL_HEARTBEAT_PATH", "/tmp/cellctl-heartbeat"),
        )
    )


if __name__ == "__main__":
    run()
