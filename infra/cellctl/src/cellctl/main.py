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

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from kubernetes import client as k8s
from kubernetes import config as k8s_config

from .alerts import AlertThresholds
from .backup_window import parse_backup_window
from .capacity import CapacityConfig, SharedWorkerPolicy
from .decide import ReconcileConfig
from .k8s_client import ClusterClient
from .manifests import ResourceSettings, check_model_env
from .media_edge import MediaEdgeConfig
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
        staging_key_id=os.environ["CELLCTL_CELL_DATA_STAGING_KEY_ID"],
        staging_application_key=os.environ["CELLCTL_CELL_DATA_STAGING_APPLICATION_KEY"],
    )


def build_reconcile_config() -> ReconcileConfig:
    # D6 step 4: the upgrade-readiness deadline is required configuration, rendered by the
    # chart (cellctl.upgradeReadyDeadlineMinutes). A first start that rebuilds a large
    # vault's catalogue can answer "warming" for many minutes, and a deadline missed
    # restores the cell and pauses the rollout, so a guessed value would do harm. The
    # chart owns the number and its bounds; cellctl refuses to start without it.
    kwargs: dict[str, object] = {
        "upgrade_ready_deadline": timedelta(minutes=_positive_integer_env("CELLCTL_UPGRADE_READY_DEADLINE_MINUTES"))
    }
    # D3: the init deadline stays a 10-minute default (not pinned exactly by the
    # design), but is configurable per the coordinator's ruling rather than hardcoded.
    minutes_raw = os.environ.get("CELLCTL_INIT_DEADLINE_MINUTES")
    if minutes_raw is not None:
        kwargs["init_deadline"] = timedelta(minutes=int(minutes_raw))
    window_raw = os.environ.get("CELLCTL_BACKUP_WINDOW")  # "2-5"
    if window_raw is not None:
        kwargs["backup_window"] = parse_backup_window(window_raw)
    concurrency_raw = os.environ.get("CELLCTL_BACKUP_CONCURRENCY")
    if concurrency_raw is not None:
        kwargs["backup_concurrency"] = int(concurrency_raw)
    for field, variable, unit in (
        ("import_deadline", "CELLCTL_IMPORT_DEADLINE_MINUTES", "minutes"),
        ("import_growth_deadline", "CELLCTL_IMPORT_GROWTH_DEADLINE_MINUTES", "minutes"),
        ("import_cleanup_deadline", "CELLCTL_IMPORT_CLEANUP_DEADLINE_MINUTES", "minutes"),
        ("import_cleanup_retry", "CELLCTL_IMPORT_CLEANUP_RETRY_MINUTES", "minutes"),
        ("import_cleanup_owner_retry", "CELLCTL_IMPORT_CLEANUP_OWNER_RETRY_MINUTES", "minutes"),
        ("export_deadline", "CELLCTL_EXPORT_DEADLINE_MINUTES", "minutes"),
        # C6 names this setting. Substrate reads the same variable to state the time limit, so set both alike.
        ("export_ttl", "EXOMEM_EXPORT_TTL_HOURS", "hours"),
    ):
        if variable in os.environ:
            amount = int(os.environ[variable])
            if amount <= 0:
                raise ValueError(f"{variable} must be positive")
            kwargs[field] = timedelta(**{unit: amount})
    return ReconcileConfig(**kwargs)


def _retention_sources() -> tuple[object, ...]:
    raw = os.environ.get("CELLCTL_KEY_RETENTION_SOURCES", "[]")
    try:
        parsed = json.loads(raw)
        json.dumps(parsed, allow_nan=False)
    except ValueError:
        return (raw,)
    return tuple(parsed) if isinstance(parsed, list) else (parsed,)


def build_storage_config() -> StorageConfig:
    """Read the shared cap and optional local class; accept the earlier nested cap."""

    raw = os.environ.get("CELLCTL_CELL_STORAGE")
    if not raw:
        return DEFAULT_STORAGE
    value = json.loads(raw)
    # These are the closed keys of the controller's storage configuration schema.
    if not isinstance(value, dict) or not set(value) <= {"domain", "local", "max_cell_gib", "per_cell_class"}:
        raise ValueError("cell storage must be a JSON object with optional domain, local and max_cell_gib")
    local = value.get("local")
    if local is not None and not isinstance(local, dict):
        raise ValueError("cell storage local must be an object")
    local = dict(local) if local is not None else None
    legacy_cap = local.pop("max_cell_gib", None) if local is not None else None
    cap = value.get("max_cell_gib", legacy_cap if legacy_cap is not None else DEFAULT_STORAGE.max_cell_gib)
    if legacy_cap is not None and cap != legacy_cap:
        raise ValueError("max_cell_gib and local.max_cell_gib must agree")
    return StorageConfig(domain=value.get("domain", LEGACY_CLASS),
                         local=LocalStorage(**local) if local is not None else None, max_cell_gib=cap,
                         per_cell_class=value.get("per_cell_class"))


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


def _positive_integer_env(name: str) -> int:
    raw = os.environ.get(name)
    if raw is None:
        raise ValueError(f"{name} is required: the chart renders it")
    # Digits only: Helm prints a large number from JSON as 1.073741824e+09, which must not pass as a guess.
    if not (raw.isascii() and raw.isdecimal()) or int(raw) <= 0:
        raise ValueError(f"{name} must be a positive whole number, not {raw!r}")
    return int(raw)


def build_alert_thresholds() -> AlertThresholds:
    """The cell alerts' thresholds, which the chart renders from the observability
    contract (`alerts.pending_operation_warn_seconds`, `alerts.pvc_reserved_free_bytes`,
    `alerts.recall_lag_warn_seconds`). A missing or invalid value fails cellctl at
    startup: with no default in code, the contract is the only place each number
    lives, and a guessed one would alert wrongly."""

    return AlertThresholds(
        unavailable_after=timedelta(seconds=_positive_integer_env("CELLCTL_PENDING_OPERATION_WARN_SECONDS")),
        disk_free_floor_bytes=_positive_integer_env("CELLCTL_PVC_RESERVED_FREE_BYTES"),
        recall_lag_after=timedelta(seconds=_positive_integer_env("CELLCTL_RECALL_LAG_WARN_SECONDS")),
    )


def _json_env(name: str, default: str, kind: type, what: str):
    value = json.loads(os.environ.get(name, default))
    if not isinstance(value, kind):
        raise ValueError(f"{what} must be a JSON {kind.__name__}")
    return value


def build_media_edge_config() -> MediaEdgeConfig | None:
    """add-cloud-media-store: None while the chart leaves the media edge off. A half-set
    configuration refuses at startup, so a cell never renders without the keys it needs."""

    url = os.environ.get("CELLCTL_MEDIA_EDGE_URL", "")
    if not url:
        return None
    with open(os.environ["CELLCTL_MEDIA_EDGE_SIGNING_KEY_FILE"], "rb") as handle:
        signing_key = load_pem_private_key(handle.read(), password=None)
    return MediaEdgeConfig(
        url=url,
        max_object_bytes=int(os.environ["CELLCTL_MEDIA_EDGE_MAX_OBJECT_BYTES"]),
        signing_key=signing_key,
        cell_ids=tuple(_json_env("CELLCTL_MEDIA_EDGE_CELL_IDS", "[]", list, "media edge cell IDs")),
        all_cells=_json_env("CELLCTL_MEDIA_EDGE_ALL_CELLS", "false", bool, "media edge all-cells switch"),
        credential_epochs=_json_env("CELLCTL_MEDIA_EDGE_CREDENTIAL_EPOCHS", "{}", dict, "media edge credential epochs"),
        store_policy=_json_env("CELLCTL_MEDIA_EDGE_STORE_POLICY", "{}", dict, "media store policy"),
    )


def build_cluster_config() -> ClusterConfig:
    model_env_raw = os.environ.get("CELLCTL_CELL_MODEL_ENV")
    job_egress_except_raw = os.environ.get("CELLCTL_JOB_EGRESS_EXCEPT")
    attachments_limit_fallback_raw = os.environ.get("CELLCTL_ATTACHMENTS_LIMIT_FALLBACK")
    model_env = json.loads(model_env_raw) if model_env_raw else {}
    artifact_cell_ids = json.loads(os.environ.get("CELLCTL_ARTIFACT_BROKER_CELL_IDS", "[]"))
    if not isinstance(artifact_cell_ids, list):
        raise ValueError("artifact broker cell IDs must be a JSON array")
    artifact_all_cells = json.loads(os.environ.get("CELLCTL_ARTIFACT_BROKER_ALL_CELLS", "false"))
    if not isinstance(artifact_all_cells, bool):
        raise ValueError("artifact broker all-cells switch must be a JSON boolean")
    media_slot_cell_ids = json.loads(os.environ.get("CELLCTL_MEDIA_SLOT_CELL_IDS", "[]"))
    if not isinstance(media_slot_cell_ids, list):
        raise ValueError("media slot cell IDs must be a JSON array")
    media_slot_all_cells = json.loads(os.environ.get("CELLCTL_MEDIA_SLOT_ALL_CELLS", "false"))
    if not isinstance(media_slot_all_cells, bool):
        raise ValueError("media slot all-cells switch must be a JSON boolean")
    upload_cell_ids = json.loads(os.environ.get("CELLCTL_UPLOAD_CELL_IDS", "[]"))
    if not isinstance(upload_cell_ids, list):
        raise ValueError("upload cell IDs must be a JSON array")
    dedicated_cell_ids = json.loads(os.environ.get("CELLCTL_DEDICATED_CELL_IDS", "[]"))
    if not isinstance(dedicated_cell_ids, list):
        raise ValueError("dedicated cell IDs must be a JSON array")
    media_raw = json.loads(os.environ.get("CELLCTL_MEDIA_ENGINE_CELL_IDS", "{}"))
    if not isinstance(media_raw, dict) or not all(isinstance(ids, list) for ids in media_raw.values()):
        raise ValueError("media engine cell IDs must be a JSON object of engine to cell ID array")
    media_engine_cell_ids = {engine: tuple(ids) for engine, ids in media_raw.items()}
    media_default_raw = json.loads(os.environ.get("CELLCTL_MEDIA_ENGINE_FLEET_DEFAULT", "[]"))
    if not isinstance(media_default_raw, list):
        raise ValueError("media engine fleet default must be a JSON array of engine names")
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
        cell_data_backup_bucket=os.environ["CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME"],
        cell_data_staging_bucket=os.environ["CELLCTL_CELL_DATA_STAGING_BUCKET_NAME"],
        cell_data_endpoint=os.environ["CELLCTL_CELL_DATA_ENDPOINT"],
        key_retention_sources=_retention_sources(),
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
        artifact_broker_all_cells=artifact_all_cells,
        media_edge=build_media_edge_config(),
        media_slot_url=os.environ.get("CELLCTL_MEDIA_SLOT_URL", ""),
        media_slot_cell_ids=tuple(media_slot_cell_ids),
        media_slot_all_cells=media_slot_all_cells,
        upload_cell_ids=tuple(upload_cell_ids),
        upload_public_key=os.environ.get("CELLCTL_UPLOAD_PUBLIC_KEY", ""),
        grant_public_key=os.environ.get("CELLCTL_GRANT_PUBLIC_KEY", ""),
        media_engine_cell_ids=media_engine_cell_ids,
        media_engine_fleet_default=tuple(media_default_raw),
        job_egress_except=tuple(job_egress_except_raw.split(",")) if job_egress_except_raw else (),
        storage=build_storage_config(),
        alert_delivery_secret=build_alert_delivery_secret(),
        alert_thresholds=build_alert_thresholds(),
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
    # D9: cellctl holds no key that creates, lists or deletes an application key, and no credential in
    # the founder account. The cell-data key only lists the locked backup bucket.
    backup_listing = B2ObjectStorage(
        B2Config(
            key_id=os.environ["CELLCTL_CELL_DATA_BACKUP_LIST_KEY_ID"],
            application_key=os.environ["CELLCTL_CELL_DATA_BACKUP_LIST_APPLICATION_KEY"],
            bucket_id=os.environ["CELLCTL_CELL_DATA_BACKUP_BUCKET_ID"],
        )
    )
    volume_provider = HetznerVolumeProvider(read_only_token=os.environ["CELLCTL_HETZNER_READ_TOKEN"])

    asyncio.run(
        run_loop(
            os.environ["CELLCTL_DATABASE_DSN"],
            cluster,
            volume_provider,
            build_secrets_config(),
            cluster_config,
            config=build_reconcile_config(),
            heartbeat_path=os.environ.get("CELLCTL_HEARTBEAT_PATH", "/tmp/cellctl-heartbeat"),
            backup_listing=backup_listing,
        )
    )


if __name__ == "__main__":
    run()
