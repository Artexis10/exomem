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
(`~/.exomem/config.json`, or `EXOMEM_CONFIG_PATH`) → the legacy `EXOMEM_QUIET_MODE`
boolean → the `normal` default. The config file is read explicitly (never injected
into the environment) so an exported `EXOMEM_MODE` always wins over it — and it is a
fixed home path, not `.env`, because a Windows service launched via `sc.exe` has an
unpredictable cwd and CLI subcommands never `load_dotenv`, yet both must read the
same mode.

Torch-free by design: importable in keyword-mode / lean CLI dispatch without paying
a torch import. `accel` imports this; this never imports `accel`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CANON = ("quiet", "normal", "performance")
_ALIASES = {"gpu": "performance", "turbo": "performance"}
DEFAULT_MODE = "normal"

_MODE_ENV = "EXOMEM_MODE"
_QUIET_ALIAS_ENV = "EXOMEM_QUIET_MODE"
_CONFIG_PATH_ENV = "EXOMEM_CONFIG_PATH"
_RELEASE_ENV = "EXOMEM_RELEASE_GPU_WHEN_IDLE"


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
    """Per-machine config file. `EXOMEM_CONFIG_PATH` overrides (tests/multi-instance)."""
    override = os.environ.get(_CONFIG_PATH_ENV)
    if override:
        return Path(override)
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
