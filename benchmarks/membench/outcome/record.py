"""What an arm manifest says about the product and the actor under test."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from epistemic.journeys.f27_replay import discover_agent_envelope

from .isolation import REPO_ROOT, product_version

#: What a call that continues a run shares with that run: the product under test and the CLI that drives it.
IDENTITY_KEYS = ("product_revision", "product_dirty", "cli")


def cli_version() -> str:
    """``claude --version``: the CLI that runs every session of this call."""
    return discover_agent_envelope().version


def require_same_run(path: Path, stored: dict, now: dict, keys: Iterable[str]) -> None:
    """Refuse a call that continues the run recorded at ``path`` when it differs from that run in any of ``keys``.

    Units that two calls play must come from one product and one CLI, or the report mixes them.
    """
    for key in keys:
        if stored.get(key) != now.get(key):
            fix = (
                "restore the checkout and CLI"
                if key in IDENTITY_KEYS
                else "repeat that call's inputs"
            )
            raise SystemExit(
                f"refused: {path} records {key} {stored.get(key)!r} and this call has {now.get(key)!r}; "
                f"use a new --run-id, or {fix}"
            )


def checkout_state(repo: Path = REPO_ROOT) -> dict:
    """The product checkout under test: its commit, the tags on that commit, and whether it has changes."""

    def git(*args: str) -> str:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
        return done.stdout.strip()

    return {
        "product_revision": git("rev-parse", "HEAD"),
        "product_tags": git("tag", "--points-at", "HEAD").split(),
        "product_dirty": bool(git("status", "--porcelain")),
    }


def product_record(root: Path) -> dict:
    """The Exomem under test: the interpreter that serves it and the version it reports."""
    return {"python": sys.executable, "exomem_version": product_version(root)}


def actor_record(*, cli: str, model: str, effort: str, root: Path) -> dict:
    """The requested half of the actor record; ``observed`` adds what the CLI reported.

    The caller adds ``mcp_config_sha256``: each arm, and each f37 case, has its own configuration.
    """
    return {
        "cli": cli,
        "model_requested": model,
        "effort": effort,
        "product": product_record(root),
    }


def observed(init: dict | None) -> dict:
    """What the CLI's first ``init`` event reported: the model it ran, the tools, skills and plugins it served."""
    init = init or {}
    return {
        "model_reported": init.get("model"),
        "tools": sorted(init.get("tools") or []),
        "mcp_servers": init.get("mcp_servers"),
        "skills": sorted(init.get("skills") or []),
        "plugins": init.get("plugins"),
    }


def run_identity(*run_dirs: Path) -> dict:
    """The actor and product that a run's arms used, from what each arm's manifest or result recorded."""
    seen: dict[str, set] = {
        "cli": set(),
        "model_requested": set(),
        "model_reported": set(),
        "exomem_version": set(),
    }
    for run_dir in run_dirs:
        for path in [*run_dir.glob("*/manifest.json"), *run_dir.glob("*/*/result.json")]:
            data = json.loads(path.read_text())
            actor = data.get("actor")
            if not actor:
                continue
            seen["cli"].add(actor.get("cli"))
            seen["model_requested"].add(actor.get("model_requested") or data.get("model_requested"))
            seen["model_reported"].add((actor.get("observed") or {}).get("model_reported"))
            seen["exomem_version"].add((actor.get("product") or {}).get("exomem_version"))
    return {key: sorted(v for v in values if v) for key, values in seen.items()}
