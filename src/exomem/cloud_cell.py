"""Environment-only configuration for Evermatter Cloud cell mode (design D1).

Cloud mode (`EXOMEM_CLOUD_CELL=1`) is a thin seam over the standalone server:
only authentication, logging, tool surface and read-only enforcement change.
`LocalRuntimeActivation`, `AuthorizationSessionMiddleware` and the local
writer lease stay exactly the standalone path. This module holds the small
amount of environment parsing that seam needs, so no other module reaches
into `os.environ` for these names directly.
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

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
#: The gateway passes the operator's current maintenance or incident notice here, as
#: base64url-encoded UTF-8 JSON. Trusted on the same terms as `GATEWAY_CLIENT_HEADER`: the
#: gateway refuses every caller header that starts with `x-exomem-`. The cell relays the notice
#: in its MCP results and never reads it outside cloud mode.
GATEWAY_SERVICE_NOTICE_HEADER = "x-exomem-service-notice"
SERVICE_NOTICE_VERSION = 1
SERVICE_NOTICE_MAX_HEADER_BYTES = 1024
SERVICE_NOTICE_MAX_SAY_CHARS = 400
# nosemgrep: ep-word-set -- the closed kinds and states of the control plane's published notice header, not a reading of text.
SERVICE_NOTICE_KINDS = frozenset({"maintenance", "incident"})
SERVICE_NOTICE_STATES = frozenset({"scheduled", "active"})  # nosemgrep: ep-word-set -- same published header contract.
#: The header's fields after `v`, which the cell checks and drops; the cell relays these and no other.
_SERVICE_NOTICE_RELAYED = ("kind", "state", "starts_at", "ends_at", "say")  # nosemgrep: ep-word-set -- same published header contract.
#: RFC 4648 section 5 alphabet, padding optional: the shape of a base64url token, not a reading of text.
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")

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


def _iso_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _notice_sentence(value: object) -> bool:
    """A sentence of 1 to `SERVICE_NOTICE_MAX_SAY_CHARS` characters that encodes as UTF-8.

    JSON can spell a lone surrogate, which no UTF-8 response can carry; the notice would then fail the call it rides on.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= SERVICE_NOTICE_MAX_SAY_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def parse_service_notice(value: object) -> dict[str, object] | None:
    """The notice a gateway header carries, or None when it is not a valid notice.

    A header that is too long, not base64url-encoded UTF-8 JSON, or whose fields fall outside
    the published shape is ignored, never refused: the notice informs the person and grants
    nothing, so a bad one must not fail a call. The result holds only the header's own values.
    """
    # A base64url token is ASCII, so the character count is the byte count here.
    if not isinstance(value, str) or len(value) > SERVICE_NOTICE_MAX_HEADER_BYTES:
        return None
    if _BASE64URL.fullmatch(value) is None:
        return None
    token = value.rstrip("=")
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.keys() != {"v", *_SERVICE_NOTICE_RELAYED}:
        return None
    version, kind, state = payload["v"], payload["kind"], payload["state"]
    ends_at = payload["ends_at"]
    valid = (
        type(version) is int
        and version == SERVICE_NOTICE_VERSION
        and isinstance(kind, str)
        and kind in SERVICE_NOTICE_KINDS
        and isinstance(state, str)
        and state in SERVICE_NOTICE_STATES
        and _iso_timestamp(payload["starts_at"])
        and (ends_at is None or _iso_timestamp(ends_at))
        and _notice_sentence(payload["say"])
    )
    if not valid:
        return None
    return {key: payload[key] for key in _SERVICE_NOTICE_RELAYED}


class CloudConfigError(RuntimeError):
    """Raised when Evermatter Cloud cell environment configuration is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def cloud_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `EXOMEM_CLOUD_CELL` selects the cloud-mode seam (D1)."""
    values = os.environ if env is None else env
    raw = str(values.get(CLOUD_MODE_ENV, "")).strip().lower()
    return raw in _TRUE


def hosted_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `EXOMEM_HOSTED_CELL` selects the hosted cell (truthy-set parse, like the cloud flag)."""
    values = os.environ if env is None else env
    raw = str(values.get("EXOMEM_HOSTED_CELL", "")).strip().lower()
    return raw in _TRUE


def store_identity(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Which Evermatter this process serves: its `kind` and the `name` to give a person.

    One home for the answer, so the bootstrap and the write summary cannot disagree. Two
    connectors can present the same display name; this tells their stores apart.
    """
    if cloud_mode_enabled(env):
        return {"kind": "cloud", "name": "Evermatter Cloud"}
    if hosted_mode_enabled(env):
        return {"kind": "hosted", "name": "Evermatter Hosted"}
    return {"kind": "personal", "name": "personal Evermatter"}


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
    if hosted_mode_enabled(values):
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
