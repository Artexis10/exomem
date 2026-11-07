"""What an arm manifest says about the product and the actor under test."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .isolation import product_python_of, product_version


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def product_record(root: Path, product_python: Path | None) -> dict:
    """The Exomem under test: the interpreter that serves it and the version it reports."""
    return {
        "python": product_python_of(product_python),
        "exomem_version": product_version(root, product_python),
        "source": "installed interpreter" if product_python is not None else "this checkout",
    }


def actor_record(
    *, cli: str, model: str, effort: str, mcp_config: Path, root: Path, product_python: Path | None
) -> dict:
    """The requested half of the actor record; ``observed`` adds what the CLI reported."""
    return {
        "cli": cli,
        "model_requested": model,
        "effort": effort,
        "mcp_config_sha256": sha256_of(mcp_config),
        "product": product_record(root, product_python),
    }


def observed(init: dict | None) -> dict:
    """What the CLI's first ``init`` event reported: the model it ran and the tools it served."""
    init = init or {}
    return {
        "model_reported": init.get("model"),
        "tools": sorted(init.get("tools") or []),
        "mcp_servers": init.get("mcp_servers"),
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
