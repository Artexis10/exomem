"""The EVERMATTER_ and KB_MCP_ names of the EXOMEM_ settings that cellctl reads or screens.

cellctl cannot import the substrate's `exomem.env_compat`, so it holds the rule here. The module
imports only the standard library: `tests/test_setting_names_in_step.py` loads it by path, without
cellctl's dependencies, and fails when its answers differ from `env_compat`'s.
"""

from __future__ import annotations

import logging
import os

# The product's closed naming history, in precedence order after EXOMEM_.
ALIAS_PREFIXES = ("EVERMATTER_", "KB_MCP_")
CANONICAL_PREFIX = "EXOMEM_"


def canonical_name(name: str) -> str:
    """The EXOMEM_ name that `name` sets: an alias name maps to it, any other name to itself."""
    for prefix in ALIAS_PREFIXES:
        if name.startswith(prefix):
            return CANONICAL_PREFIX + name[len(prefix) :]
    return name


def setting(name: str) -> str | None:
    """The value of `name`; an EXOMEM_ name also reads its alias names when it is unset.

    `name` wins over its aliases. A differing EVERMATTER_ value logs one warning that names both
    variables, never the values.
    """
    value = os.environ.get(name)
    if not name.startswith(CANONICAL_PREFIX):
        return value
    for prefix in ALIAS_PREFIXES:
        alias = prefix + name[len(CANONICAL_PREFIX) :]
        alias_value = os.environ.get(alias)
        if alias_value is None:
            continue
        if value is None:
            value = alias_value
        elif prefix == "EVERMATTER_" and alias_value != value:
            logging.getLogger(__name__).warning("%s and %s differ; using %s", alias, name, name)
    return value
