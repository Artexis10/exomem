"""One disposable Exomem: a fresh vault and state root that no live service can share.

Every outcome suite starts its Exomem arms from here, so the pins live in one
place. The pins come from ``collection_ingest.mcp_session.isolated_env``; this
module adds the two that keep a hook or writer from reaching a running service.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from collection_ingest.mcp_session import isolated_env
from epistemic.journeys.f27_replay import PLUGIN_DIR, custom_instructions_block, environment_floor

from . import billing

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The actor model and effort every suite runs unless the operator names another.
ACTOR_MODEL = "claude-sonnet-5-5"
ACTOR_EFFORT = "high"

#: The prominence the plugin and its docs ship as the default. The documented block for it is
#: appended to the system prompt of an arm that plays Exomem as its users get it.
SHIPPED_PROMINENCE = "balanced"


def plugin_read_allow(plugin_dir: Path) -> str:
    """The permission rule that lets a skill read its own ``references/`` files in ``plugin_dir``.

    The plugin directory is readable and nothing else outside the workspace is. A protected-path
    disallow still wins over this allow.
    """
    return f"Read(/{plugin_dir}/**)"


#: The rule for this checkout's own plugin.
PLUGIN_READ_ALLOW = plugin_read_allow(PLUGIN_DIR)

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


def server_pins(root: Path, checkout: Path = REPO_ROOT) -> dict[str, str]:
    """The environment a disposable server needs, and nothing inherited: vault and state under ``root``.

    The product is ``checkout`` (this one by default), which ``PYTHONPATH`` puts on the path. A
    release is tested by running from a clean checkout of its tag.
    """
    state = root / "exomem-state"
    pins = isolated_env(
        state=state,
        vault=root / "vault",
        server_root=checkout,
        extra={
            # No service credential and a private lease coordinator: nothing reaches a running service.
            "EXOMEM_SERVICE_ENV": str(state / "no-service.env"),
            "EXOMEM_LEASE_COORDINATOR_DB": str(state / "lease-coordinator.sqlite"),
        },
    )
    return {k: v for k, v in pins.items() if k.startswith("EXOMEM_") or k in _SERVER_KEYS}


def child_env(root: Path, checkout: Path = REPO_ROOT) -> dict[str, str]:
    """This process's environment without any inherited Exomem setting, plus the pins."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("EXOMEM_", "KB_"))}
    return {**base, **server_pins(root, checkout)}


def claude_env() -> tuple[dict[str, str], tuple[str, ...]]:
    """The environment of every Claude actor and judge: no Exomem or KB setting, no nested-session marker,
    and the credential the run's billing declaration allows (``billing.claude_session_env``).

    Returns the names the nested-session strip removed, so a run record can state them.
    Claude Code's own auto memory would be a second, unmeasured store in every arm.
    """
    floor, removed = environment_floor(os.environ)
    env = billing.claude_session_env(
        {k: v for k, v in floor.items() if not k.startswith(("EXOMEM_", "KB_"))}
    )
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


def lay_instruction_block(path: Path, checkout: Path = REPO_ROOT) -> dict:
    """Write the documented custom-instruction block for the shipped prominence to ``path``.

    Read out of the doc that ``checkout`` publishes it in at run time, so the arm cannot drift from
    what users are told. Returns the record a manifest keeps: the prominence, where it was read, its digest.
    """
    block, citation = custom_instructions_block(SHIPPED_PROMINENCE, checkout)
    path.write_text(block, encoding="utf-8")
    return {
        "prominence": SHIPPED_PROMINENCE,
        "source": citation,
        "sha256": hashlib.sha256(block.encode()).hexdigest(),
    }


def product_version(root: Path) -> str:
    """``exomem --version`` as this checkout reports it, run under the pins."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "exomem", "--version"],
        env=child_env(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise SystemExit(
            f"{sys.executable} -m exomem --version failed: {done.stderr.strip()[-300:]}"
        )
    return done.stdout.strip()


def fresh_vault(root: Path, checkout: Path = REPO_ROOT) -> Path:
    """Lay the starter scaffold that ``checkout`` ships into ``root/vault`` once, under the pins."""
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
            env=child_env(root, checkout),
            check=True,
            capture_output=True,
        )
    return vault


def mcp_server(root: Path, checkout: Path = REPO_ROOT) -> dict:
    """An ``--mcp-config`` server entry for the disposable Exomem of ``checkout`` under ``root``."""
    fresh_vault(root, checkout)
    return {
        "command": sys.executable,
        "args": ["-m", "exomem", "--transport", "stdio"],
        "env": server_pins(root, checkout),
    }


# --------------------------------------------------------------------------
# Copies of an arm's vault and state.
# --------------------------------------------------------------------------

SERVER_EXIT_SECONDS = 60.0


def server_alive(root: Path) -> bool:
    """Whether any process still serves the state root under ``root`` (its environment names it)."""
    needle = f"EXOMEM_STATE_ROOT={server_pins(root)['EXOMEM_STATE_ROOT']}".encode()
    for env_file in Path("/proc").glob("[0-9]*/environ"):
        try:
            if needle in env_file.read_bytes().split(b"\0"):
                return True
        except OSError:
            continue
    return False


def copy_state(source: Path, target: Path) -> None:
    """Replace ``target``'s vault and state with a copy of ``source``'s, once no server holds either.

    Used to snapshot a compiled vault, to hand each f37 case its own copy, and to put a vault back
    before a retry. The interpreter bytecode cache under the state root is left out: it rebuilds itself.
    """
    deadline = time.monotonic() + SERVER_EXIT_SECONDS
    while server_alive(source) or server_alive(target):
        if time.monotonic() > deadline:
            raise RuntimeError(f"an Exomem server still holds {source} or {target}; copy refused")
        time.sleep(1)
    for name in ("vault", "exomem-state"):
        shutil.rmtree(target / name, ignore_errors=True)
        shutil.copytree(
            source / name, target / name, symlinks=True, ignore=shutil.ignore_patterns("pycache")
        )
