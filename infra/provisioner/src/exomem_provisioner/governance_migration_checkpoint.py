"""Bounded replay hints for the existing worker, never effect authorization."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from .driver import DriverTerminal, EffectContext

CHECKPOINT_VERSION = "gm1"
_PHASES = {
    "inspect": "i",
    "prepare": "p",
    "enroll": "e",
    "commit": "c",
    "complete": "d",
    "confirmed": "t",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]{0,511}@sha256:[0-9a-f]{64}\Z")


def _refuse() -> DriverTerminal:
    return DriverTerminal("PROVISIONER_CHECKPOINT_INVALID")


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _binding(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return False
    decoded = base64.urlsafe_b64decode(value + "=")
    return len(decoded) == 32 and base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() == value


def migration_binding(context: EffectContext, *, pvc_uid: str, runtime_image: str) -> str:
    """Bind retries to the same operation, volume and authenticated target image."""
    if (
        not isinstance(context, EffectContext)
        or type(context.fence_generation) is not int
        or context.fence_generation < 0
        or any(
            not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
            for value in (
                context.operation_id,
                context.provider_operation_id,
                context.tenant_id,
                context.cell_id,
                context.wire_protocol,
                pvc_uid,
            )
        )
        or not isinstance(runtime_image, str)
        or _IMAGE.fullmatch(runtime_image) is None
    ):
        raise _refuse()
    payload = json.dumps(
        [*context.provider_identity, context.wire_protocol, pvc_uid, runtime_image],
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    digest = hashlib.sha256(b"exomem.hosted-governance-migration-binding.v1\0" + payload).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class MigrationCheckpoint:
    phase: str
    vault_fingerprint: str
    binding: str
    source_store_digest: str | None = None
    plan_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.phase, str)
            or self.phase not in _PHASES
            or not _digest(self.vault_fingerprint)
            or not _binding(self.binding)
            or (
                self.source_store_digest is not None
                if self.phase == "inspect"
                else not _digest(self.source_store_digest)
            )
            or (
                not _digest(self.plan_digest)
                if self.phase not in {"inspect", "prepare"}
                else self.plan_digest is not None
            )
        ):
            raise _refuse()

    def encode(self) -> str:
        # One phase code and three hex digests plus a 43-byte binding fit
        # the existing 256-character column without truncation or new storage.
        return ":".join(
            (
                CHECKPOINT_VERSION,
                _PHASES[self.phase],
                self.vault_fingerprint,
                self.binding,
                self.source_store_digest or "-",
                self.plan_digest or "-",
            )
        )

    @classmethod
    def decode(cls, raw: str) -> MigrationCheckpoint:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 256:
            raise _refuse()
        fields = raw.split(":")
        if len(fields) != 6 or fields[0] != CHECKPOINT_VERSION:
            raise _refuse()
        phase = next((name for name, code in _PHASES.items() if code == fields[1]), None)
        if phase is None:
            raise _refuse()
        return cls(
            phase,
            fields[2],
            fields[3],
            None if fields[4] == "-" else fields[4],
            None if fields[5] == "-" else fields[5],
        )
