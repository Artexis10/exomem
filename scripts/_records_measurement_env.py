"""Isolate a Records measurement process from any live Exomem configuration.

The Records measurement scripts import this before they import ``exomem``. It
removes every inherited Exomem setting, because an inherited endpoint or state
root could reach a live writer lease, and points the state, configuration and
vault at a temporary tree. It also turns off the models and watchers that a
measurement of the Records path does not exercise.
"""

from __future__ import annotations

import os
from pathlib import Path

INHERITED_PREFIXES = ("EXOMEM_", "KB_MCP_")


def clear_inherited() -> None:
    for name in tuple(os.environ):
        if name.startswith(INHERITED_PREFIXES):
            del os.environ[name]


def isolate(tmp: Path, vault: Path) -> None:
    """Point every state, configuration and vault setting at ``tmp`` and ``vault``."""
    os.environ.update(
        {
            "XDG_STATE_HOME": str(tmp / "xdg"),
            "EXOMEM_STATE_ROOT": str(tmp / "state"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp / "state" / "writer-lease"),
            "EXOMEM_CONFIG_PATH": str(tmp / "no-such-config.json"),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "EXOMEM_VAULT_PATH": str(vault),
        }
    )
