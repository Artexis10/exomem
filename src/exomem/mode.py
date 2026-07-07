"""Compute mode — one per-machine knob that governs device + footprint policy.

The mode answers "how much of this machine may exomem use?" and is the single
setting ~90% of users ever touch. It resolves to concrete policy the rest of the
code reads: which torch device steady-state models load on (via `accel`), whether
boot preloads models, and whether idle models are released.

Three canonical modes (aliases in `_ALIASES` so whatever a user types works):

- **quiet**      — don't touch my machine. CPU everywhere, no boot preload, release
                   models when idle. Idle VRAM ~0. ("I'm gaming / low power.")
- **normal**     — the safe default. CPU steady-state (MPS on Apple Silicon), never
                   auto-selects CUDA; bulk index may still use the GPU in a separate
                   process. Boot preloads onto CPU RAM.
- **performance** — use my GPU for speed. Steady-state on CUDA when a capable GPU is
                   present; release when idle. Aliases: `gpu`, `turbo`.

Resolution precedence: `EXOMEM_MODE` env → the per-machine config file
(`%PROGRAMDATA%\\exomem\\config.json` on Windows, `~/.exomem/config.json` on POSIX, or
`EXOMEM_CONFIG_PATH`) → the legacy `EXOMEM_QUIET_MODE` boolean → the `normal` default.
The config file is read explicitly (never injected into the environment) so an exported
`EXOMEM_MODE` always wins over it — and it is a fixed, machine-wide path (not `.env`,
whose cwd-relative discovery a `sc.exe`-launched service can't rely on, and which CLI
subcommands never `load_dotenv`) so the service and the CLI — often different OS users —
read the SAME mode. See `config_path` for the Windows LocalSystem-vs-user rationale.

Torch-free by design: importable in keyword-mode / lean CLI dispatch without paying
a torch import. `accel` imports this; this never imports `accel`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CANON = ("quiet", "normal", "performance")
_ALIASES = {
    "gpu": "performance",
    "turbo": "performance",
    "resource-saver": "quiet",
    "low-resource": "quiet",
}
DEFAULT_MODE = "normal"

_DEFAULT_DEBOUNCE_SECONDS = 0.5
_QUIET_DEBOUNCE_SECONDS = 2.0
_DEFAULT_RECONCILE_INTERVAL_SECONDS = 300.0
_QUIET_RECONCILE_INTERVAL_SECONDS = 900.0
_DEFAULT_RECONCILE_MAX_EMBED_FILES = 500
_QUIET_EXPENSIVE_INDEX_CAP = 0

_MODE_ENV = "EXOMEM_MODE"
_QUIET_ALIAS_ENV = "EXOMEM_QUIET_MODE"
_CONFIG_PATH_ENV = "EXOMEM_CONFIG_PATH"
_RELEASE_ENV = "EXOMEM_RELEASE_GPU_WHEN_IDLE"


@dataclass(frozen=True)
class WatcherPolicy:
    """Mode-derived watcher/reconcile limits, kept torch-free for lean status paths."""

    debounce_seconds: float
    reconcile_interval_seconds: float
    max_embed_files_per_batch: int | None
    max_reconcile_embed_files: int | None
    defer_expensive_indexes: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _truthy(value: str | None) -> bool:
    """Shared truthiness convention (mirrors `freshness._truthy`)."""
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def normalize(value: str | None) -> str | None:
    """Canonical mode for a raw string (accepting aliases), or None if unknown."""
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v in CANON:
        return v
    return _ALIASES.get(v)


def config_path() -> Path:
    """Per-machine config file. `EXOMEM_CONFIG_PATH` overrides (tests/multi-instance).

    On Windows the default is machine-wide (`%PROGRAMDATA%\\exomem\\config.json`), NOT
    the user home: a service commonly runs as LocalSystem while the `exomem` CLI runs as
    the logged-in user, and `~` resolves to two different profiles — so a home-relative
    file would let the CLI write one config the service never reads, silently breaking the
    live mode switch. ProgramData is shared (the CLI user creates+writes it; the service
    reads it). On POSIX, home is kept — services there usually run as the user, and the
    override covers the rest.
    """
    override = os.environ.get(_CONFIG_PATH_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData"
        return Path(base) / "exomem" / "config.json"
    return Path.home() / ".exomem" / "config.json"


def read_config() -> dict:
    """Parsed config dict; `{}` when missing or corrupt. Never raises."""
    try:
        data = json.loads(config_path().read_text("utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt config must degrade to default
        return {}
    return data if isinstance(data, dict) else {}


def resolve_mode() -> str:
    """The effective mode: env → config file → legacy quiet alias → default."""
    env = normalize(os.environ.get(_MODE_ENV))
    if env:
        return env
    cfg = normalize(read_config().get("mode"))
    if cfg:
        return cfg
    if _truthy(os.environ.get(_QUIET_ALIAS_ENV)):
        return "quiet"
    return DEFAULT_MODE


def preload_models() -> bool:
    """Whether boot should eagerly preload models. Off only in quiet mode."""
    return resolve_mode() != "quiet"


def preload_cpu_caches() -> bool:
    """Whether startup warm-up may materialize O(vault) CPU caches."""
    return resolve_mode() != "quiet"


def retain_cpu_caches() -> bool:
    """Whether large CPU caches may stay resident after use."""
    return resolve_mode() != "quiet"


def defer_expensive_indexes() -> bool:
    """Whether semantic/visual indexing should be queued or capped."""
    return resolve_mode() == "quiet"


def watcher_policy() -> WatcherPolicy:
    """Mode-derived low-interrupt watcher/reconcile policy."""
    if defer_expensive_indexes():
        return WatcherPolicy(
            debounce_seconds=_QUIET_DEBOUNCE_SECONDS,
            reconcile_interval_seconds=_QUIET_RECONCILE_INTERVAL_SECONDS,
            max_embed_files_per_batch=_QUIET_EXPENSIVE_INDEX_CAP,
            max_reconcile_embed_files=_QUIET_EXPENSIVE_INDEX_CAP,
            defer_expensive_indexes=True,
        )
    return WatcherPolicy(
        debounce_seconds=_DEFAULT_DEBOUNCE_SECONDS,
        reconcile_interval_seconds=_DEFAULT_RECONCILE_INTERVAL_SECONDS,
        max_embed_files_per_batch=None,
        max_reconcile_embed_files=_DEFAULT_RECONCILE_MAX_EMBED_FILES,
        defer_expensive_indexes=False,
    )


def release_when_idle() -> bool:
    """Whether the idle-unload reaper should run.

    `EXOMEM_RELEASE_GPU_WHEN_IDLE` (truthy/falsy) overrides; otherwise on for quiet
    and performance (where a model may become resident and idle), off for normal.
    """
    override = os.environ.get(_RELEASE_ENV)
    if override is not None and override.strip() != "":
        return _truthy(override)
    return resolve_mode() in ("quiet", "performance")


def bulk_gpu_opted() -> bool:
    """Whether an in-server bulk index (rebuild_all) may use the GPU.

    Only in performance mode, where the server already holds a CUDA context — so
    an in-server GPU rebuild adds no new idle-context floor. Normal/quiet keep
    in-server rebuilds on CPU; the separate `exomem index` CLI process is where
    normal-mode onboarding gets the GPU (it frees the context on exit).
    """
    return resolve_mode() == "performance"


def resolved() -> dict:
    """Diagnostic snapshot of the effective policy (for `exomem mode` / logs)."""
    m = resolve_mode()
    return {
        "mode": m,
        "preload_models": preload_models(),
        "preload_cpu_caches": preload_cpu_caches(),
        "retain_cpu_caches": retain_cpu_caches(),
        "defer_expensive_indexes": defer_expensive_indexes(),
        "watcher_policy": watcher_policy().as_dict(),
        "release_when_idle": release_when_idle(),
        "bulk_gpu": bulk_gpu_opted(),
    }


def write_mode(value: str) -> Path:
    """Persist a mode to the config file (atomic). Accepts aliases. Raises on unknown."""
    canonical = normalize(value)
    if canonical is None:
        raise ValueError(
            f"unknown mode: {value!r} (expected one of {CANON} or an alias {tuple(_ALIASES)})"
        )
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_config()
    data.update(schema=1, mode=canonical)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    os.replace(tmp, path)  # atomic swap
    return path


# --------------------------------------------------------------- live application
#
# The server watches the config file and reconciles the running process to a mode
# change WITHOUT a restart (the user's chosen UX). Reconciliation = unload the model
# singletons so they lazily reload on the new device, and start/stop the idle-unload
# reaper to match. Lazy imports keep this module torch-free at import time.

_watch_thread: threading.Thread | None = None
_watch_stop = threading.Event()
_applied_mode: str | None = None


def apply_live() -> dict:
    """Reconcile the running process to the currently-resolved mode. Returns the policy.

    Unloads the model singletons (skipping any mid-encode — they reload on the new
    device next use) and starts/stops the idle-unload reaper. Safe to call repeatedly.
    Caveat (CUDA-context-floor note): a performance→quiet live switch frees weights but
    the CUDA context floor persists until the process restarts; the default normal mode
    never holds a context, so ordinary use is unaffected.
    """
    global _applied_mode
    _applied_mode = resolve_mode()
    from . import embeddings, model_reaper

    for unload in (embeddings.unload_model, embeddings.unload_reranker, embeddings.unload_clip_model):
        try:
            unload()
        except Exception:  # noqa: BLE001 — a live switch must never crash the caller
            log.warning("model unload during mode switch failed", exc_info=True)
    if _applied_mode == "quiet":
        from . import bm25, find

        for unload in (embeddings.unload_index_caches, bm25.unload_cache, find.unload_ram_caches):
            try:
                unload()
            except Exception:  # noqa: BLE001 — quiet entry must remain best-effort
                log.warning("cache unload during quiet mode switch failed", exc_info=True)
    if release_when_idle():
        model_reaper.start()
    else:
        model_reaper.stop()
    return resolved()


def _config_watch_disabled() -> bool:
    return _truthy(os.environ.get("EXOMEM_DISABLE_MODE_WATCH"))


def start_config_watch(interval: float = 10.0) -> threading.Thread | None:
    """Daemon that applies a config-file mode change live (idempotent).

    Polls `resolve_mode()` every `interval`s and calls `apply_live()` when it changes,
    so `exomem mode <name>` takes effect on the running server within one interval — no
    restart. Off via `EXOMEM_DISABLE_MODE_WATCH`. Returns None when disabled/already up.
    """
    global _watch_thread, _applied_mode
    if _config_watch_disabled():
        return None
    if _watch_thread is not None and _watch_thread.is_alive():
        return _watch_thread
    _watch_stop.clear()
    _applied_mode = resolve_mode()  # baseline; don't reconcile on startup

    def _run() -> None:
        while not _watch_stop.wait(interval):
            try:
                if resolve_mode() != _applied_mode:
                    log.info("compute mode changed to %s; applying live", resolve_mode())
                    apply_live()
            except Exception:  # noqa: BLE001 — the watch must never die on a bad tick
                log.warning("mode-watch tick failed", exc_info=True)

    t = threading.Thread(target=_run, name="exomem-mode-watch", daemon=True)
    _watch_thread = t
    t.start()
    return t


def stop_config_watch() -> None:
    _watch_stop.set()
