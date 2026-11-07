"""One disposable Exomem: a fresh vault and state root that no live service can share.

Every outcome suite starts its Exomem arms from here, so the pins live in one
place. The pins come from ``collection_ingest.mcp_session.isolated_env``; this
module adds the two that keep a hook or writer from reaching a running service.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from collection_ingest.mcp_session import isolated_env

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Non-EXOMEM keys the server itself needs from the pins.
_SERVER_KEYS = frozenset(
    {
        "XDG_STATE_HOME",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "HF_HUB_OFFLINE",
        "FASTMCP_CHECK_FOR_UPDATES",
        "FASTMCP_SHOW_SERVER_BANNER",
    }
)


def server_pins(root: Path) -> dict[str, str]:
    """The environment a disposable server needs, and nothing inherited: vault and state under ``root``."""
    state = root / "exomem-state"
    pins = isolated_env(
        state=state,
        vault=root / "vault",
        server_root=REPO_ROOT,
        extra={
            # No service credential and a private lease coordinator: nothing reaches a running service.
            "EXOMEM_SERVICE_ENV": str(state / "no-service.env"),
            "EXOMEM_LEASE_COORDINATOR_DB": str(state / "lease-coordinator.sqlite"),
        },
    )
    return {k: v for k, v in pins.items() if k.startswith("EXOMEM_") or k in _SERVER_KEYS}


def child_env(root: Path) -> dict[str, str]:
    """This process's environment without any inherited Exomem setting, plus the pins."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("EXOMEM_", "KB_"))}
    return {**base, **server_pins(root)}


def fresh_vault(root: Path) -> Path:
    """Lay the shipped starter scaffold into ``root/vault`` once, under the pins."""
    vault = root / "vault"
    if not vault.exists():
        vault.mkdir(parents=True)
        subprocess.run(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from exomem.init import init_vault; init_vault(Path(sys.argv[1]))",
                str(vault),
            ],
            env=child_env(root),
            check=True,
            capture_output=True,
        )
    return vault


def mcp_server(root: Path) -> dict:
    """An ``--mcp-config`` server entry for the disposable Exomem under ``root``."""
    fresh_vault(root)
    return {
        "command": sys.executable,
        "args": ["-m", "exomem", "--transport", "stdio"],
        "env": server_pins(root),
    }
