"""No-allocation resource posture and residency diagnostics.

This module is intentionally conservative: status collection must not import
`torch`, load models, create sidecars, read vector matrices, or initialize CUDA.
It reports loaded state only from modules already present in `sys.modules`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import mode


def _mode_source() -> str:
    if os.environ.get("EXOMEM_MODE"):
        return "env"
    if mode.read_config().get("mode"):
        return "config"
    if os.environ.get("EXOMEM_QUIET_MODE"):
        return "quiet-alias"
    return "default"


def _model_residency() -> dict[str, Any]:
    embeddings = sys.modules.get("exomem.embeddings")
    if embeddings is None:
        return {
            "module_loaded": False,
            "embeddings": False,
            "reranker": False,
            "clip": False,
        }
    return {
        "module_loaded": True,
        "embeddings": getattr(embeddings, "_MODEL", None) is not None,
        "reranker": getattr(embeddings, "_RERANKER", None) is not None,
        "clip": getattr(embeddings, "_CLIP_MODEL", None) is not None,
    }


def _cache_residency() -> dict[str, Any]:
    caches: dict[str, Any] = {}
    embeddings = sys.modules.get("exomem.embeddings")
    if embeddings is not None and hasattr(embeddings, "index_cache_status"):
        caches["vector_matrices"] = embeddings.index_cache_status()
    else:
        caches["vector_matrices"] = {
            "embedding": {"loaded": 0, "indexes": 0, "rows": 0, "bytes": 0},
            "clip": {"loaded": 0, "indexes": 0, "rows": 0, "bytes": 0},
        }

    bm25 = sys.modules.get("exomem.bm25")
    if bm25 is not None and hasattr(bm25, "cache_status"):
        caches["bm25"] = bm25.cache_status()
    else:
        caches["bm25"] = {
            "loaded": False,
            "corpora": 0,
            "documents": 0,
            "tokenized_documents": 0,
            "tokens": 0,
        }

    find = sys.modules.get("exomem.find")
    if find is not None and hasattr(find, "cache_status"):
        caches["find"] = find.cache_status()
    else:
        caches["find"] = {
            "pages": {"entries": 0, "body_chars": 0},
            "resolver": {"entries": 0},
            "hot_results": {"entries": 0, "hits": 0},
        }
    return caches


def _deferred_work(vault_root: Path | None) -> dict[str, Any]:
    index_sync = sys.modules.get("exomem.index_sync")
    if index_sync is None or not hasattr(index_sync, "deferred_work_status"):
        return {
            "semantic_upserts": {
                "count": 0,
                "paths": [],
                "truncated": False,
                "roots": 0,
            }
        }
    return index_sync.deferred_work_status(vault_root)


def cuda_accounting_if_initialized() -> dict[str, Any]:
    """CUDA accounting without importing torch or creating a context."""
    torch = sys.modules.get("torch")
    if torch is None:
        return {"torch_imported": False, "initialized": False, "memory": None}
    cuda = getattr(torch, "cuda", None)
    if cuda is None:
        return {"torch_imported": True, "initialized": False, "memory": None}
    try:
        initialized = bool(cuda.is_initialized())
    except Exception:  # noqa: BLE001
        return {
            "torch_imported": True,
            "initialized": "unknown",
            "memory": None,
        }
    if not initialized:
        return {"torch_imported": True, "initialized": False, "memory": None}
    try:
        memory = {
            "allocated_mb": round(cuda.memory_allocated() / 2**20, 1),
            "reserved_mb": round(cuda.memory_reserved() / 2**20, 1),
        }
    except Exception:  # noqa: BLE001
        memory = None
    return {"torch_imported": True, "initialized": True, "memory": memory}


def runtime_info() -> dict[str, Any]:
    """Runtime shape without shelling out or importing heavy modules."""
    variant = (os.environ.get("EXOMEM_CONTAINER_VARIANT") or "").strip() or None
    marker = None
    for candidate in (Path("/.dockerenv"), Path("/run/.containerenv")):
        try:
            if candidate.exists():
                marker = str(candidate)
                break
        except OSError:
            continue
    in_container = bool(variant or marker)
    return {
        "kind": "container" if in_container else "native",
        "container": in_container,
        "variant": variant,
        "marker": marker,
    }


def collect(vault_root: Path | None = None) -> dict[str, Any]:
    """Collect process-local resource status without allocating heavy resources."""
    policy = mode.resolved()
    return {
        "runtime": runtime_info(),
        "mode": policy["mode"],
        "source": _mode_source(),
        "config_path": str(mode.config_path()),
        "policy": policy,
        "models": _model_residency(),
        "caches": _cache_residency(),
        "deferred_work": _deferred_work(vault_root),
        "cuda": cuda_accounting_if_initialized(),
    }


def _min_free_vram_mb() -> int:
    try:
        gb = float(os.environ.get("EXOMEM_GPU_MIN_FREE_GB") or "2.0")
    except ValueError:
        gb = 2.0
    return int(gb * 1024)


def _probe_nvidia_smi() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"status": "unavailable", "reason": "nvidia-smi not found"}
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=memory.free,memory.total,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "reason": str(e)}
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "nvidia-smi failed").strip()
        return {"status": "unavailable", "reason": reason}
    line = (proc.stdout or "").splitlines()[0].strip() if proc.stdout else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return {"status": "unavailable", "reason": "unexpected nvidia-smi output"}
    try:
        free_mb = int(float(parts[0]))
        total_mb = int(float(parts[1]))
    except ValueError:
        return {"status": "unavailable", "reason": "unparseable nvidia-smi output"}
    return {
        "status": "ok",
        "free_mb": free_mb,
        "total_mb": total_mb,
        "name": parts[2] if len(parts) > 2 else None,
    }


def gpu_headroom() -> dict[str, Any]:
    """Non-torch GPU headroom posture for doctor/setup messaging."""
    if os.environ.get("CUDA_VISIBLE_DEVICES") == "":
        return {
            "status": "disabled",
            "usable": False,
            "reason": "CUDA_VISIBLE_DEVICES is empty",
            "min_free_mb": _min_free_vram_mb(),
        }
    probe = _probe_nvidia_smi()
    min_free = _min_free_vram_mb()
    if probe.get("status") != "ok":
        return {
            "status": "unknown",
            "usable": None,
            "reason": probe.get("reason"),
            "min_free_mb": min_free,
        }
    free_mb = int(probe["free_mb"])
    usable = free_mb >= min_free
    return {
        "status": "capable" if usable else "marginal",
        "usable": usable,
        "reason": None if usable else "free VRAM below policy threshold",
        "free_mb": free_mb,
        "total_mb": int(probe["total_mb"]),
        "name": probe.get("name"),
        "min_free_mb": min_free,
    }


def resource_posture() -> dict[str, Any]:
    policy = mode.resolved()
    return {
        "runtime": runtime_info(),
        "mode": policy["mode"],
        "source": _mode_source(),
        "policy": policy,
        "cpu_baseline": True,
        "gpu": gpu_headroom(),
        "cuda": cuda_accounting_if_initialized(),
    }
