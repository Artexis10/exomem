"""Which account pays for a session: a run condition that every result records, never a defect.

The same model answers through a subscription login and through API credits, so a run may use
either. It declares which one it expects, and a session that reports the other is a harness
fault (``faults.init_faults``): one run never mixes sources by accident. A report names the
source of each arm, and says so in its leading lines when compared arms differ.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

#: The declaration: the billing name that this run's sessions must report. Absent means subscription.
ENV_VAR = "EXOMEM_OUTCOME_BILLING"

#: Billing name -> the ``apiKeySource`` that a session on it reports. The CLI fixes these values:
#: ``none`` for a subscription login, and the key's variable name for `harness claude-credits run`
#: (docs/harness.md, "It stops the run if Claude's init message reports an apiKeySource other than
#: ANTHROPIC_API_KEY"). It is a closed set; any other value stays a fault.
KEY_SOURCES = {"subscription": "none", "credits": "ANTHROPIC_API_KEY"}
DEFAULT = "subscription"
#: What a source outside the set, or none at all, is called in a record.
UNKNOWN = "unknown"


def declared(env: Mapping[str, str] | None = None) -> str:
    """The billing source this run expects; refuses a name outside ``KEY_SOURCES``."""
    name = (os.environ if env is None else env).get(ENV_VAR) or DEFAULT
    if name not in KEY_SOURCES:
        raise SystemExit(f"{ENV_VAR} is {name!r}; it takes {' or '.join(KEY_SOURCES)}")
    return name


def of_key_source(api_key_source: object) -> str:
    """The billing name for the ``apiKeySource`` a session reported, ``unknown`` for any other value."""
    by_value = {value: name for name, value in KEY_SOURCES.items()}
    return by_value.get(api_key_source, UNKNOWN) if isinstance(api_key_source, str) else UNKNOWN


def arm_sources(*run_dirs: Path) -> dict[str, list[str]]:
    """``arm -> billing names`` that each arm's manifests recorded; ``unknown`` when one recorded none."""
    seen: dict[str, set[str]] = {}
    for run_dir in run_dirs:
        paths = [
            *run_dir.glob("*/manifest.json"),
            *run_dir.glob("*/*/result.json"),
            *run_dir.glob("*/*/*/manifest.json"),
        ]
        for path in paths:
            data = json.loads(path.read_text())
            actor = data.get("actor")
            if not actor:
                continue
            arm = str(data.get("arm") or path.parent.name)
            observed = actor.get("observed") or {}
            seen.setdefault(arm, set()).add(observed.get("billing") or UNKNOWN)
    return {arm: sorted(names) for arm, names in sorted(seen.items())}


def mix_line(
    by_arm: Mapping[str, list[str]], compared: list[tuple[str, str]] | None = None
) -> str | None:
    """A sentence for the leading lines when compared arms differ in billing source, else ``None``.

    ``compared`` is the pairs the report compares; without it every arm is compared with every other.
    """
    names = {arm: tuple(v) for arm, v in by_arm.items()}
    pairs = compared if compared is not None else [(a, b) for a in names for b in names if a < b]
    differing = [(a, b) for a, b in pairs if a in names and b in names and names[a] != names[b]]
    inside = [arm for arm, v in names.items() if len(v) > 1]
    if not differing and not inside:
        return None
    parts = [f"{a} ({'/'.join(names[a])}) vs {b} ({'/'.join(names[b])})" for a, b in differing]
    parts += [f"{arm} ran on {' and '.join(names[arm])}" for arm in inside]
    return (
        "billing sources are mixed: "
        + "; ".join(parts)
        + ". Same model, different billing: read it as a run condition."
    )


def lines(
    by_arm: Mapping[str, list[str]], compared: list[tuple[str, str]] | None = None
) -> list[str]:
    """The report's leading lines: the mix warning when there is one, then each arm's billing source."""
    if not by_arm:
        return []
    mix = mix_line(by_arm, compared)
    out = [mix] if mix else []
    out.append("billing by arm: " + ", ".join(f"{a} {'/'.join(v)}" for a, v in by_arm.items()))
    return out
