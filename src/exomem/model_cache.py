"""Where model weights live locally, and whether a load may skip the network.

Torch-free and network-free by construction: everything here is a directory
check or a keyword-argument decision, so `status` and `doctor` can consult it
without importing a runtime or risking a download.

Why it exists: `sentence-transformers` and `hf_hub_download` revalidate a repo's
files against the hub on every load, even when the snapshot is already on disk.
For bge-base that is ~30 HTTP round trips, and because the model loads lazily it
is paid *inside* whichever user request happens to arrive first — making the
first write after a restart depend on network reachability for files that never
left the disk.

The policy is offline-first, never offline-only. When the snapshot is resident we
ask for it by name alone (`local_files_only=True`); when it is not resident, or
when that attempt fails for any reason at all, we fall back to the ordinary
networked load. A first-run user with an empty cache still downloads, a partial
snapshot still repairs itself, and a runtime that does not accept the keyword
still loads — the fallback covers all three without needing to tell them apart.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Opt out of offline-first loading (``0``/``false``/``no``/``off``). Set this if you
#: deliberately want every load to revalidate against the hub — a weight-file swap
#: upstream is then picked up on the next load, at the cost of putting the network
#: back on the request path.
OFFLINE_ENV = "EXOMEM_MODEL_OFFLINE"

#: huggingface_hub's own global offline switch. Honoured here so a load stays offline
#: even with a cold cache: the operator has said the network is not to be used, and a
#: hopeful fallback would just fail slowly instead of failing fast.
HF_OFFLINE_ENV = "HF_HUB_OFFLINE"

#: sentence-transformers resolves an unqualified model name under its own org, and the
#: hub cache records the *resolved* id. `clip-ViT-B-32` therefore lands in
#: `models--sentence-transformers--clip-ViT-B-32`.
_IMPLICIT_ORG = "sentence-transformers"


def _truthy(value: str | None) -> bool:
    """Shared truthiness convention (mirrors `mode._truthy`)."""
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def hub_dir() -> Path:
    """The local HuggingFace hub cache directory (honors HF_HUB_CACHE / HF_HOME).

    Directory resolution only — never touches the network.
    """
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def snapshot_dirname(model_name: str) -> str:
    """The hub-cache directory name a model's snapshots live under."""
    qualified = model_name if "/" in model_name else f"{_IMPLICIT_ORG}/{model_name}"
    return "models--" + qualified.replace("/", "--")


def snapshot_populated(hub: Path, dirname: str) -> bool:
    """True if `dirname`'s snapshot dir exists under `hub` and is non-empty.

    A pure directory check, so a caller can gate model-loading work on it WITHOUT
    risking a download — which is what lets `doctor` report residency while keeping
    its "never fetches anything" guarantee.
    """
    snapshots = hub / dirname / "snapshots"
    try:
        return snapshots.is_dir() and any(snapshots.iterdir())
    except OSError:
        return False


def is_cached(model_name: str, hub: Path | None = None) -> bool:
    """True if `model_name`'s weights are already resident in the local hub cache."""
    return snapshot_populated(hub or hub_dir(), snapshot_dirname(model_name))


def offline_enabled() -> bool:
    """Whether offline-first loading is switched on at all. Default: yes."""
    override = os.environ.get(OFFLINE_ENV)
    if override is not None and override.strip() != "":
        return _truthy(override)
    return True


def should_load_offline(model_name: str) -> bool:
    """Whether the next load of `model_name` may skip the hub entirely."""
    if _truthy(os.environ.get(HF_OFFLINE_ENV)):
        return True
    return offline_enabled() and is_cached(model_name)


def load_offline_first(model_name: str, loader: Callable[..., T]) -> T:
    """Load `model_name` from the local cache when it is resident, else normally.

    `loader` is called with ``local_files_only=True`` for the offline attempt and
    with no arguments at all for the networked one — so a runtime that has never
    heard of the keyword raises `TypeError` on the first call and is served
    correctly by the second, with no version probing.
    """
    if not should_load_offline(model_name):
        return loader()
    try:
        return loader(local_files_only=True)
    except Exception as e:  # noqa: BLE001 — any offline failure is a reason to go online
        log.info(
            "local-only load of %s failed (%s); retrying with hub access",
            model_name,
            e,
        )
        return loader()
