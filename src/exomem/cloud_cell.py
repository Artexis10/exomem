"""Environment-only configuration for Exomem Cloud cell mode (design D1).

Cloud mode (`EXOMEM_CLOUD_CELL=1`) is a thin seam over the standalone server:
only authentication, logging, tool surface and read-only enforcement change.
`LocalRuntimeActivation`, `AuthorizationSessionMiddleware` and the local
writer lease stay exactly the standalone path. This module holds the small
amount of environment parsing that seam needs, so no other module reaches
into `os.environ` for these names directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

CLOUD_MODE_ENV = "EXOMEM_CLOUD_CELL"
CLOUD_READ_ONLY_ENV = "EXOMEM_CLOUD_READ_ONLY"
CLOUD_CELL_ISSUER = "exomem-cloud-cell"

_TRUE = frozenset({"1", "true", "yes", "on"})


class CloudConfigError(RuntimeError):
    """Raised when Exomem Cloud cell environment configuration is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def cloud_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `EXOMEM_CLOUD_CELL` selects the cloud-mode seam (D1)."""
    values = os.environ if env is None else env
    raw = str(values.get(CLOUD_MODE_ENV, "")).strip().lower()
    return raw in _TRUE


def cloud_read_only_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `EXOMEM_CLOUD_READ_ONLY` puts the cell in read-only mode (D1.4)."""
    values = os.environ if env is None else env
    raw = str(values.get(CLOUD_READ_ONLY_ENV, "")).strip().lower()
    return raw in _TRUE


#: A C4 bearer is always 43 base64url characters, so this only ever fires on
#: a mis-rendered Secret -- never on a real one, current or previous (D1.1).
MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True, slots=True)
class CloudCellCredentials:
    """One cell's bearer, and its previous value during rotation (C4)."""

    cell_id: str
    token: str
    previous_token: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CloudCellCredentials:
        values = os.environ if env is None else env
        cell_id = str(values.get("EXOMEM_CLOUD_CELL_ID", "")).strip()
        token = str(values.get("EXOMEM_CLOUD_CELL_TOKEN", "")).strip()
        previous = str(values.get("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", "")).strip() or None
        missing = [
            name
            for name, value in (
                ("EXOMEM_CLOUD_CELL_ID", cell_id),
                ("EXOMEM_CLOUD_CELL_TOKEN", token),
            )
            if not value
        ]
        if missing:
            raise CloudConfigError(
                "CLOUD_CELL_CONFIG_MISSING",
                f"missing required env var(s): {', '.join(missing)}",
            )
        short = [
            name
            for name, value in (
                ("EXOMEM_CLOUD_CELL_TOKEN", token),
                ("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", previous),
            )
            if value is not None and len(value) < MIN_TOKEN_LENGTH
        ]
        if short:
            # Never echo the value: the message names only the env var.
            raise CloudConfigError(
                "CLOUD_CELL_CONFIG_INVALID",
                f"env var(s) shorter than {MIN_TOKEN_LENGTH} characters: {', '.join(short)}",
            )
        return cls(cell_id=cell_id, token=token, previous_token=previous)
