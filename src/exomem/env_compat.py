"""Environment-variable compatibility (KB_MCP_* and EVERMATTER_* → EXOMEM_*).

Internal code reads only the `EXOMEM_*` prefix. Two other prefixes reach it
through promotion: at package import (and on demand, for env populated later),
each `KB_MCP_X` value is copied to `EXOMEM_X` when that name is unset, and each
`EVERMATTER_X` value is copied to `EXOMEM_X` always.

The two rules differ on a conflict, for one reason. `KB_MCP_*` is the older name,
so an explicitly set `EXOMEM_X` wins over it. `EVERMATTER_*` is the product name
a user sets on purpose after the rename, so it wins over `EXOMEM_X`.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

LEGACY_PREFIX = "KB_MCP_"
CANONICAL_PREFIX = "EXOMEM_"
PRODUCT_PREFIX = "EVERMATTER_"

# Every prefix that may spell a setting: the one place a reader of a file that
# declares settings (such as a `.env` read before promotion) learns the names.
ACCEPTED_PREFIXES = (CANONICAL_PREFIX, LEGACY_PREFIX, PRODUCT_PREFIX)

_advised = False


def promote_legacy() -> list[str]:
    """Copy `KB_MCP_X` and `EVERMATTER_X` env values to `EXOMEM_X`.

    `KB_MCP_X` fills `EXOMEM_X` only when that name is unset; `EVERMATTER_X`
    overwrites it. Returns the canonical names that were populated this call.
    Safe to call repeatedly (idempotent per variable); call again after any
    late environment loading. Logs one advisory line, once per process, the
    first time anything is promoted.
    """
    global _advised
    promoted: list[str] = []
    for key in list(os.environ):
        if key.startswith(LEGACY_PREFIX):
            canonical = CANONICAL_PREFIX + key[len(LEGACY_PREFIX):]
            if os.environ.get(canonical) is None:
                os.environ[canonical] = os.environ[key]
                promoted.append(canonical)
        elif key.startswith(PRODUCT_PREFIX):
            canonical = CANONICAL_PREFIX + key[len(PRODUCT_PREFIX):]
            if os.environ.get(canonical) != os.environ[key]:
                os.environ[canonical] = os.environ[key]
                promoted.append(canonical)
    if promoted and not _advised:
        _advised = True
        log.info(
            "KB_MCP_* and EVERMATTER_* environment variables detected and honored "
            "(%d promoted to EXOMEM_*); code reads the EXOMEM_* names",
            len(promoted),
        )
    return promoted
