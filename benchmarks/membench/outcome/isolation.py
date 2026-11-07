"""One disposable Exomem: a fresh vault and state root that no live service can share.

Every outcome suite starts its Exomem arms from here, so the pins live in one
place. The pins come from ``collection_ingest.mcp_session.isolated_env``; this
module adds the two that keep a hook or writer from reaching a running service.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from collection_ingest.mcp_session import isolated_env
from epistemic.journeys.f27_replay import custom_instructions_block, environment_floor

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The actor model and effort every suite runs unless the operator names another.
ACTOR_MODEL = "claude-sonnet-5-5"
ACTOR_EFFORT = "high"

#: The prominence the plugin and its docs ship as the default. The documented block for it is
#: appended to the system prompt of an arm that plays Exomem as its users get it.
SHIPPED_PROMINENCE = "balanced"

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


def server_pins(root: Path, product_python: Path | None = None) -> dict[str, str]:
    """The environment a disposable server needs, and nothing inherited: vault and state under ``root``.

    ``product_python`` names an interpreter with its own Exomem installed (a release under
    test). Without it the product is this checkout, which ``PYTHONPATH`` puts on the path.
    """
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
    kept = {k: v for k, v in pins.items() if k.startswith("EXOMEM_") or k in _SERVER_KEYS}
    if product_python is not None:
        # An installed release imports itself; a path to this checkout would shadow it.
        kept.pop("PYTHONPATH", None)
    return kept


def child_env(root: Path, product_python: Path | None = None) -> dict[str, str]:
    """This process's environment without any inherited Exomem setting, plus the pins."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("EXOMEM_", "KB_"))}
    if product_python is not None:
        base.pop("PYTHONPATH", None)
    return {**base, **server_pins(root, product_python)}


def claude_env() -> tuple[dict[str, str], tuple[str, ...]]:
    """The environment of every Claude actor and judge: no Exomem or KB setting, no nested-session marker.

    Returns the names the nested-session strip removed, so a run record can state them.
    Claude Code's own auto memory would be a second, unmeasured store in every arm.
    """
    floor, removed = environment_floor(os.environ)
    env = {k: v for k, v in floor.items() if not k.startswith(("EXOMEM_", "KB_"))}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env, removed


def hook_env(root: Path, session: str | None = None) -> dict[str, str]:
    """What the shipped Claude hooks inherit: the arm's own pins and hook home, no live service route.

    The hook keeps its cooldowns in the hook home. ``session`` gives one session its own home,
    for sessions that must each start from the hook's initial state.
    """
    pins = {k: v for k, v in server_pins(root).items() if k.startswith("EXOMEM_")}
    home = root / "hook-home"
    pins["EXOMEM_HOOK_HOME"] = str(home / session if session else home)
    return pins


def lay_instruction_block(path: Path) -> dict:
    """Write the documented custom-instruction block for the shipped prominence to ``path``.

    Read out of the doc that publishes it at run time, so the arm cannot drift from what users
    are told. Returns the record a manifest keeps: the prominence, where it was read, its digest.
    """
    block, citation = custom_instructions_block(SHIPPED_PROMINENCE)
    path.write_text(block, encoding="utf-8")
    return {
        "prominence": SHIPPED_PROMINENCE,
        "source": citation,
        "sha256": hashlib.sha256(block.encode()).hexdigest(),
    }


def product_python_of(product_python: Path | None) -> str:
    return str(product_python) if product_python is not None else sys.executable


def product_version(root: Path, product_python: Path | None = None) -> str:
    """``exomem --version`` as the product interpreter reports it, run under the pins."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        [product_python_of(product_python), "-m", "exomem", "--version"],
        env=child_env(root, product_python),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise SystemExit(
            f"{product_python_of(product_python)} -m exomem --version failed: {done.stderr.strip()[-300:]}"
        )
    return done.stdout.strip()


def fresh_vault(root: Path, product_python: Path | None = None) -> Path:
    """Lay the shipped starter scaffold into ``root/vault`` once, under the pins."""
    vault = root / "vault"
    if not vault.exists():
        vault.mkdir(parents=True)
        subprocess.run(  # noqa: S603 - fixed argv
            [
                product_python_of(product_python),
                "-c",
                "import sys; from pathlib import Path; "
                "from exomem.init import init_vault; init_vault(Path(sys.argv[1]))",
                str(vault),
            ],
            env=child_env(root, product_python),
            check=True,
            capture_output=True,
        )
    return vault


def mcp_server(root: Path, product_python: Path | None = None) -> dict:
    """An ``--mcp-config`` server entry for the disposable Exomem under ``root``."""
    fresh_vault(root, product_python)
    return {
        "command": product_python_of(product_python),
        "args": ["-m", "exomem", "--transport", "stdio"],
        "env": server_pins(root, product_python),
    }
