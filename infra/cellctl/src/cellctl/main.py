"""cellctl's process entrypoint: reads configuration from the pod
environment (no .env file, matching the cloud-mode convention) and runs the
D4 reconcile loop against a real cluster."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import timedelta

from kubernetes import client as k8s
from kubernetes import config as k8s_config

from .capacity import CapacityConfig
from .decide import DEFAULT_RECONCILE_CONFIG, ReconcileConfig
from .k8s_client import ClusterClient
from .manifests import ResourceSettings, check_model_env
from .reconcile import ClusterConfig, SecretsConfig, run_loop
from .storage.b2 import B2Config, B2ObjectStorage
from .storage.hetzner import HetznerVolumeProvider


def _b64_env(name: str) -> bytes:
    return base64.b64decode(os.environ[name])


def _versioned_keys_env(name: str) -> dict[int, bytes]:
    """`name` holds JSON: {"1": "<base64>", "2": "<base64>", ...}."""

    raw = json.loads(os.environ[name])
    return {int(version): base64.b64decode(value) for version, value in raw.items()}


def build_secrets_config() -> SecretsConfig:
    previous_raw = os.environ.get("CELLCTL_CELL_TOKEN_KEY_PREVIOUS")
    previous_version_raw = os.environ.get("CELLCTL_CELL_TOKEN_KEY_PREVIOUS_VERSION")
    return SecretsConfig(
        cell_token_key_current=_b64_env("CELLCTL_CELL_TOKEN_KEY_CURRENT"),
        cell_token_key_previous=base64.b64decode(previous_raw) if previous_raw else None,
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
        start, end = window_raw.split("-")
        kwargs["backup_window"] = (int(start), int(end))
    concurrency_raw = os.environ.get("CELLCTL_BACKUP_CONCURRENCY")
    if concurrency_raw is not None:
        kwargs["backup_concurrency"] = int(concurrency_raw)
    if not kwargs:
        return DEFAULT_RECONCILE_CONFIG
    return ReconcileConfig(**kwargs)


def build_cluster_config() -> ClusterConfig:
    model_env_raw = os.environ.get("CELLCTL_CELL_MODEL_ENV")
    job_egress_except_raw = os.environ.get("CELLCTL_JOB_EGRESS_EXCEPT")
    attachments_limit_fallback_raw = os.environ.get("CELLCTL_ATTACHMENTS_LIMIT_FALLBACK")
    model_env = json.loads(model_env_raw) if model_env_raw else {}
    # A bad chart value fails cellctl at startup, not on every render.
    check_model_env(model_env)
    return ClusterConfig(
        object_storage_bucket=os.environ["CELLCTL_B2_BUCKET_NAME"],
        object_storage_endpoint=os.environ["CELLCTL_B2_ENDPOINT"],
        resources=ResourceSettings(
            cpu_request=os.environ.get("CELLCTL_CELL_CPU_REQUEST", "250m"),
            cpu_limit=os.environ.get("CELLCTL_CELL_CPU_LIMIT", "2"),
            memory_request=os.environ.get("CELLCTL_CELL_MEMORY_REQUEST", "1Gi"),
            memory_limit=os.environ.get("CELLCTL_CELL_MEMORY_LIMIT", "1536Mi"),
        ),
        model_env=model_env,
        job_egress_except=tuple(job_egress_except_raw.split(",")) if job_egress_except_raw else (),
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

    cluster = ClusterClient(k8s.ApiClient())
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
            build_cluster_config(),
            config=build_reconcile_config(),
            heartbeat_path=os.environ.get("CELLCTL_HEARTBEAT_PATH", "/tmp/cellctl-heartbeat"),
        )
    )


if __name__ == "__main__":
    run()
