"""Legacy environment-variable compatibility (KB_MCP_* → EXOMEM_*).

The project renamed its internals from kb_mcp to exomem; every configuration
variable is now `EXOMEM_*` and internal code reads only that prefix. Existing
deployments configured with `KB_MCP_*` keep working through promotion: at
package import (and on demand, for env populated later), each `KB_MCP_X` value
is copied to `EXOMEM_X` when the new name is unset. An explicitly set
`EXOMEM_X` always wins over a conflicting legacy value.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

LEGACY_PREFIX = "KB_MCP_"
CANONICAL_PREFIX = "EXOMEM_"

_advised = False


def promote_legacy() -> list[str]:
    """Copy each `KB_MCP_X` env value to `EXOMEM_X` when the new name is unset.

    Returns the canonical names that were populated from legacy values this
    call. Safe to call repeatedly (idempotent per variable); call again after
    any late environment loading. Logs one advisory line, once per process,
    the first time anything is promoted.
    """
    global _advised
    promoted: list[str] = []
    for key in list(os.environ):
        if not key.startswith(LEGACY_PREFIX):
            continue
        canonical = CANONICAL_PREFIX + key[len(LEGACY_PREFIX):]
        if os.environ.get(canonical) is None:
            os.environ[canonical] = os.environ[key]
            promoted.append(canonical)
    if promoted and not _advised:
        _advised = True
        log.info(
            "legacy KB_MCP_* environment variables detected and honored "
            "(%d promoted); prefer the EXOMEM_* names going forward",
            len(promoted),
        )
    return promoted
