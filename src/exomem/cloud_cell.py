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
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

CLOUD_MODE_ENV = "EXOMEM_CLOUD_CELL"
CLOUD_READ_ONLY_ENV = "EXOMEM_CLOUD_READ_ONLY"
RESOURCE_POLICY_ENV = "EXOMEM_CLOUD_RESOURCE_POLICY"
CLOUD_HEALTH_PORT_ENV = "EXOMEM_CLOUD_HEALTH_PORT"
CLOUD_CELL_ISSUER = "exomem-cloud-cell"
#: The canonical shape of a cell ID: sixteen base32 characters. Shape only;
#: the control plane decides which cells exist.
CELL_ID_PATTERN = r"[a-z2-7]{16}"
#: The gateway names the authenticated OAuth client of each request here. A
#: stateless request from a handshake-protocol client carries no `clientInfo`,
#: and the gateway forwards no `User-Agent`, so without it every such call looks
#: like an unknown coding client. Trusted only in cloud mode: there the cell
#: bearer proves the request came through the gateway, which sets this header
#: itself and never forwards a caller's copy. It tunes eagerness only.
GATEWAY_CLIENT_HEADER = "x-exomem-oauth-client"
#: The gateway names the engagement context (`coding` or `conversation`) of the
#: authenticated OAuth client here, from the control plane's client registry.
#: Trusted on the same terms as `GATEWAY_CLIENT_HEADER`; neither name starts
#: with `mcp-param-`, the one prefix the gateway forwards from a caller.
GATEWAY_CONTEXT_HEADER = "x-exomem-client-context"
#: The gateway names the authenticated OAuth grant with its stable UUID here.
#: Trusted on the same terms as `GATEWAY_CLIENT_HEADER`, never from clientInfo.
#: This content-free, owner-private actor metadata grants no authority.
GATEWAY_APP_ID_HEADER = "x-exomem-app-id"

_TRUE = frozenset({"1", "true", "yes", "on"})
_ISSUER_FAMILY_PREFIX = "cloud-cell:"


def issuer_family(cell_id: str) -> str:
    """The principal issuer family of an operation authenticated by this cell's bearer."""
    return _ISSUER_FAMILY_PREFIX + cell_id


def is_cloud_issuer_family(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_ISSUER_FAMILY_PREFIX)


def canonical_app_id(value: object) -> str | None:
    """The gateway's canonical UUID, or unknown; never interpret a client name."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return value if str(parsed) == value else None


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


def health_port(env: Mapping[str, str] | None = None) -> int | None:
    """The port of the cell's health-only listener (`cloud_health`), or None.

    The platform sets it; a cell started without it serves readiness only on
    the port that carries MCP, as a local install does.
    """
    values = os.environ if env is None else env
    if not cloud_mode_enabled(values):
        return None
    raw = str(values.get(CLOUD_HEALTH_PORT_ENV, "")).strip()
    if not raw:
        return None
    if not (raw.isascii() and raw.isdecimal()) or not 1 <= int(raw) <= 65535:
        raise CloudConfigError("CLOUD_HEALTH_PORT_INVALID", "health port must be within 1..65535")
    return int(raw)


def resource_policy(env: Mapping[str, str] | None = None) -> str:
    """Validate the operator profile without loading models or vault state.

    Authentication remains the server's existing credential validation; init
    and backup jobs can resolve resource policy without a serving bearer.
    """
    values = os.environ if env is None else env
    selected = str(values.get(RESOURCE_POLICY_ENV, "")).strip().lower() or "legacy"
    if selected not in {"legacy", "service-v1"}:
        raise CloudConfigError("CLOUD_RESOURCE_POLICY_INVALID", "unknown resource profile")
    if selected == "legacy":
        return selected
    if not cloud_mode_enabled(values):
        raise CloudConfigError("CLOUD_RESOURCE_POLICY_INVALID", "service profile requires Cloud mode")
    if str(values.get("EXOMEM_HOSTED_CELL", "")).strip().lower() in _TRUE:
        raise CloudConfigError("CLOUD_RESOURCE_POLICY_CONFLICT", "service profile cannot select Hosted mode")
    for name in ("EXOMEM_PRELOAD_MODELS", "EXOMEM_RELEASE_GPU_WHEN_IDLE"):
        if str(values.get(name, "")).strip():
            raise CloudConfigError("CLOUD_RESOURCE_POLICY_CONFLICT", f"unsupported override: {name}")
    if str(values.get("EXOMEM_ALLOW_NATIVE_THREAD_OVERRIDES", "")).strip() == "1":
        raise CloudConfigError("CLOUD_RESOURCE_POLICY_CONFLICT", "unsafe native overrides are disabled")
    for name, lower, upper in (
        ("EXOMEM_EMBED_BATCH", 1, 8),
        ("EXOMEM_CPU_THREADS", 1, 2),
        ("EXOMEM_SYNC_WORKERS", 4, 8),
    ):
        raw = str(values.get(name, "")).strip()
        if not raw:
            continue
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if not lower <= parsed <= upper:
            raise CloudConfigError("CLOUD_RESOURCE_BUDGET_INVALID", f"{name} must be within {lower}..{upper}")
    for name in ("EXOMEM_DEVICE", "EXOMEM_TORCH_DEVICE", "EXOMEM_EMBED_DEVICE", "EXOMEM_CLIP_DEVICE", "EXOMEM_VOICE_DEVICE", "EXOMEM_ASR_DEVICE", "EXOMEM_DIARIZE_DEVICE"):
        raw = str(values.get(name, "")).strip().lower()
        if raw and raw != "cpu":
            raise CloudConfigError("CLOUD_RESOURCE_POLICY_CONFLICT", f"CPU required for {name}")
    return selected


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
