"""Alternate setting prefixes (EVERMATTER_*, KB_MCP_*) onto EXOMEM_*.

Internal code reads only `EXOMEM_*`. A deployment may name a setting with the
product prefix `EVERMATTER_*` or the pre-rename prefix `KB_MCP_*` instead. Every
source of settings (the process environment, a `.env` file, a service
environment file) maps its own names through `canonical_settings` before its own
precedence rules apply, so a source never lets an alias beat an explicit
`EXOMEM_X`, and a conflict between two sources is seen under one name.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TypeVar

log = logging.getLogger(__name__)

V = TypeVar("V")

LEGACY_PREFIX = "KB_MCP_"
PUBLIC_PREFIX = "EVERMATTER_"
CANONICAL_PREFIX = "EXOMEM_"
# Closed by the product's naming history, in precedence order after EXOMEM_; the hooks copy the EVERMATTER_ step.
ALIAS_PREFIXES = (PUBLIC_PREFIX, LEGACY_PREFIX)
SETTING_PREFIXES = (CANONICAL_PREFIX, *ALIAS_PREFIXES)

_advised = False
# A conflict is reported once per process: the same environment is mapped again
# by every later loader, and one line per pair is enough for the operator.
_reported: set[tuple[str, str]] = set()


def _fill_sources(settings: Mapping[str, str | None]) -> dict[str, str]:
    """`{canonical name: alias name}` for each canonical name that `settings` leaves unset.

    The alias is the highest-precedence one that `settings` sets. A value of
    None counts as unset. When names for one setting carry different values,
    the highest-precedence name wins and one warning names the pair (never the
    values: they may be secrets).
    """
    by_suffix: dict[str, list[tuple[int, str]]] = {}
    for name, value in settings.items():
        if value is None:
            continue
        for rank, prefix in enumerate(SETTING_PREFIXES):
            if name.startswith(prefix):
                by_suffix.setdefault(name[len(prefix) :], []).append((rank, name))
                break
    sources: dict[str, str] = {}
    for suffix, candidates in by_suffix.items():
        candidates.sort()
        winner = candidates[0][1]
        for _, loser in candidates[1:]:
            if settings[loser] != settings[winner] and (winner, loser) not in _reported:
                _reported.add((winner, loser))
                log.warning(
                    "%s and %s are both set to different values; using %s",
                    winner,
                    loser,
                    winner,
                )
        if not winner.startswith(CANONICAL_PREFIX):
            sources[CANONICAL_PREFIX + suffix] = winner
    return sources


def names_for(name: str) -> tuple[str, ...]:
    """Every name that can set `name`, highest precedence first.

    An `EXOMEM_X` name has its alias names too; any other name stands alone. A
    writer that removes a setting removes all of them, or an alias line would
    bring the removed value back.
    """
    if not name.startswith(CANONICAL_PREFIX):
        return (name,)
    suffix = name[len(CANONICAL_PREFIX) :]
    return tuple(prefix + suffix for prefix in SETTING_PREFIXES)


def canonical_settings(settings: Mapping[str, V]) -> dict[str, V]:
    """`settings` with each unset `EXOMEM_X` filled from its highest-precedence alias.

    An `EXOMEM_X` that `settings` sets is never replaced. The alias entries stay.
    """
    resolved = dict(settings)
    for canonical, alias in _fill_sources(settings).items():
        resolved[canonical] = settings[alias]
    return resolved


def promote_legacy() -> list[str]:
    """Apply `canonical_settings` to the process environment.

    Returns the canonical names that were populated from an alias this call.
    Safe to call repeatedly (idempotent per variable); call again after any
    late environment loading. Logs one advisory line, once per process, the
    first time a `KB_MCP_*` value is promoted.
    """
    global _advised
    sources = _fill_sources(os.environ)
    for canonical, alias in sources.items():
        os.environ[canonical] = os.environ[alias]
    if not _advised and any(alias.startswith(LEGACY_PREFIX) for alias in sources.values()):
        _advised = True
        log.info(
            "legacy KB_MCP_* environment variables detected and honored; "
            "prefer the EXOMEM_* or EVERMATTER_* names going forward",
        )
    return list(sources)


def load_dotenv(
    dotenv_path: str | os.PathLike[str] | None = None, *, override: bool = False
) -> bool:
    """`dotenv.load_dotenv`, plus the canonical names the file fills from an alias.

    A file that sets only `EVERMATTER_X` gives the process `EXOMEM_X`. Under
    `override` it beats an exported `EXOMEM_X`, exactly as the file's own
    `EXOMEM_X` would; without it, an exported value stays. The caller passes a
    path it has already checked with `dotenv_guard`.
    """
    import dotenv

    loaded = dotenv.load_dotenv(dotenv_path=dotenv_path, override=override)
    raw = dotenv.dotenv_values(dotenv_path)
    for name, value in canonical_settings(raw).items():
        if raw.get(name) is None and value is not None and (override or name not in os.environ):
            os.environ[name] = value
            loaded = True
    return loaded
