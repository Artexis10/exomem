"""Post-database-restore provider scan and maximum-fence recovery gate."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .durability_repository import (
    DurabilityRepository,
    RediscoveryObservationInput,
    RediscoveryObservationSnapshot,
)

_REQUIRED_PROVIDERS = frozenset({"kubernetes", "hcloud", "traefik", "b2"})
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_BASE32_CHUNK = re.compile(r"^[a-z2-7]{1,52}$")
_ENVELOPE = re.compile(r"^[A-Za-z0-9_-]{40,4096}$")


class ProviderMetadataConflict(RuntimeError):
    pass


class ProviderReference:
    """Collision-free canonical identity for one exact external object."""

    _PREFIX = "pr1_"

    @classmethod
    def kubernetes(
        cls,
        *,
        provider: str,
        api_version: str,
        kind: str,
        namespace: str,
        name: str,
    ) -> str:
        if provider not in {"kubernetes", "traefik"}:
            raise ValueError("Kubernetes provider reference kind is invalid")
        return cls._build(
            {
                "apiVersion": api_version,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "provider": provider,
                "version": 1,
            }
        )

    @classmethod
    def hcloud(cls, *, kind: str, resource_id: int | str) -> str:
        identifier = str(resource_id)
        if not identifier.isdigit() or int(identifier) < 1:
            raise ValueError("HCloud provider reference ID is invalid")
        return cls._build(
            {
                "id": identifier,
                "kind": kind,
                "provider": "hcloud",
                "version": 1,
            }
        )

    @classmethod
    def b2(cls, *, bucket: str, key: str) -> str:
        return cls._build({"bucket": bucket, "key": key, "provider": "b2", "version": 1})

    @classmethod
    def parse(cls, value: str) -> dict[str, object]:
        try:
            if not value.startswith(cls._PREFIX):
                raise ValueError("provider reference version is invalid")
            decoded = ProviderRecoveryIdentityVerifier._decode(value.removeprefix(cls._PREFIX))
            parsed = json.loads(decoded.decode("ascii"))
            if (
                not isinstance(parsed, dict)
                or ProviderRecoveryIdentityCodec._canonical(parsed) != decoded
                or parsed.get("version") != 1
            ):
                raise ValueError("provider reference is not canonical")
            return parsed
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as error:
            raise ProviderMetadataConflict("provider recovery reference is invalid") from error

    @classmethod
    def _build(cls, value: dict[str, object]) -> str:
        if any(
            not isinstance(item, str) or not item
            for key, item in value.items()
            if key != "version" and key != "namespace"
        ):
            raise ValueError("provider reference fields must be non-empty strings")
        canonical = ProviderRecoveryIdentityCodec._canonical(value)
        return cls._PREFIX + ProviderRecoveryIdentityCodec._encode(canonical)


class ProviderRecoveryIdentityCodec:
    """Ed25519 signer for identity bound to one exact provider resource."""

    _DOMAIN = b"exomem.provider-recovery-identity.v1\x00"

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("provider recovery signing seed must be 32 bytes")
        self._private_key = Ed25519PrivateKey.from_private_bytes(key)

    @classmethod
    def from_secret(cls, secret: bytes | str) -> ProviderRecoveryIdentityCodec:
        """Derive a deterministic seed from arbitrary test/development material."""

        raw = secret.encode() if isinstance(secret, str) else secret
        return cls(hashlib.sha256(raw).digest())

    @classmethod
    def from_encoded_seed(cls, value: str) -> ProviderRecoveryIdentityCodec:
        """Load the base64url-no-pad raw seed emitted by the SOPS handoff."""

        try:
            raw = ProviderRecoveryIdentityVerifier._decode(value)
        except (ValueError, binascii.Error) as error:
            raise ValueError("provider recovery signing seed is invalid") from error
        return cls(raw)

    def public_key(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return self._encode(raw)

    def verifier(self) -> ProviderRecoveryIdentityVerifier:
        return ProviderRecoveryIdentityVerifier.from_public_key(self.public_key())

    def seal(
        self,
        *,
        provider: str,
        provider_reference: str,
        tenant_id: str,
        cell_id: str,
        operation_id: str,
        fence_generation: int,
    ) -> str:
        payload = {
            "provider": provider,
            "providerReference": provider_reference,
            "tenantId": tenant_id,
            "cellId": cell_id,
            "operationId": operation_id,
            "fenceGeneration": fence_generation,
        }
        payload_bytes = self._canonical(payload)
        signature = self._private_key.sign(self._DOMAIN + payload_bytes)
        envelope = self._canonical(
            {
                "alg": "Ed25519",
                "payload": self._encode(payload_bytes),
                "signature": self._encode(signature),
                "version": 1,
            }
        )
        return self._encode(envelope)

    def authenticate(self, envelope: str, **expected: object) -> None:
        self.verifier().authenticate(envelope, **expected)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _canonical(value: dict[str, object]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "ascii"
        )


class ProviderIdentityAuthenticator(Protocol):
    def authenticate(self, envelope: str, **expected: object) -> None: ...


class ProviderIdentitySigner(Protocol):
    def seal(
        self,
        *,
        provider: str,
        provider_reference: str,
        tenant_id: str,
        cell_id: str,
        operation_id: str,
        fence_generation: int,
    ) -> str: ...


class ProviderRecoveryIdentityVerifier:
    """Public-key verifier safe to mount in admission and rediscovery workloads."""

    _DOMAIN = ProviderRecoveryIdentityCodec._DOMAIN

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._public_key = public_key

    @classmethod
    def from_public_key(cls, value: str) -> ProviderRecoveryIdentityVerifier:
        try:
            raw = cls._decode(value)
            return cls(Ed25519PublicKey.from_public_bytes(raw))
        except ValueError as error:
            raise ValueError("provider recovery public key is invalid") from error

    def authenticate(
        self,
        envelope: str,
        *,
        provider: str,
        provider_reference: str,
        tenant_id: str,
        cell_id: str,
        operation_id: str,
        fence_generation: int,
    ) -> None:
        try:
            envelope_bytes = self._decode(envelope)
            value = json.loads(envelope_bytes.decode("ascii"))
            if not isinstance(value, dict):
                raise ValueError("identity envelope root is invalid")
            if ProviderRecoveryIdentityCodec._canonical(value) != envelope_bytes:
                raise ValueError("identity envelope is not canonical")
            if value.get("version") != 1 or value.get("alg") != "Ed25519":
                raise ValueError("identity envelope algorithm is invalid")
            payload_bytes = self._decode(str(value["payload"]))
            payload = json.loads(payload_bytes.decode("ascii"))
            if (
                not isinstance(payload, dict)
                or ProviderRecoveryIdentityCodec._canonical(payload) != payload_bytes
            ):
                raise ValueError("identity payload is not canonical")
            signature = self._decode(str(value["signature"]))
            self._public_key.verify(signature, self._DOMAIN + payload_bytes)
        except (
            InvalidSignature,
            ValueError,
            KeyError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as error:
            raise ProviderMetadataConflict(
                "provider recovery identity did not authenticate"
            ) from error
        expected_payload = {
            "provider": provider,
            "providerReference": provider_reference,
            "tenantId": tenant_id,
            "cellId": cell_id,
            "operationId": operation_id,
            "fenceGeneration": fence_generation,
        }
        if payload != expected_payload:
            raise ProviderMetadataConflict("provider recovery identity did not authenticate")

    @staticmethod
    def _decode(value: str) -> bytes:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)


@dataclass(frozen=True, slots=True)
class ProviderMetadataObservation:
    provider: str
    provider_reference: str
    tenant_id: str
    cell_id: str | None
    operation_id: str
    fence_generation: int
    observed_at: datetime
    metadata_authenticated: bool


class ProviderRecoveryIdentityDecoder:
    """Decode and cross-check the provider lane's reversible recovery identity."""

    @classmethod
    def kubernetes(
        cls,
        *,
        provider_reference: str,
        annotations: dict[str, str],
        observed_at: datetime,
        identity_codec: ProviderIdentityAuthenticator,
    ) -> ProviderMetadataObservation:
        return cls._kubernetes_like(
            provider="kubernetes",
            provider_reference=provider_reference,
            annotations=annotations,
            observed_at=observed_at,
            identity_codec=identity_codec,
        )

    @classmethod
    def traefik(
        cls,
        *,
        provider_reference: str,
        annotations: dict[str, str],
        observed_at: datetime,
        identity_codec: ProviderIdentityAuthenticator,
    ) -> ProviderMetadataObservation:
        return cls._kubernetes_like(
            provider="traefik",
            provider_reference=provider_reference,
            annotations=annotations,
            observed_at=observed_at,
            identity_codec=identity_codec,
        )

    @classmethod
    def _kubernetes_like(
        cls,
        *,
        provider: str,
        provider_reference: str,
        annotations: dict[str, str],
        observed_at: datetime,
        identity_codec: ProviderIdentityAuthenticator,
    ) -> ProviderMetadataObservation:
        tenant_id = cls._opaque(annotations.get("exomem.io/tenant-id"))
        cell_id = cls._opaque(annotations.get("exomem.io/cell-id"))
        operation_id = cls._opaque(annotations.get("exomem.io/operation-id"))
        fence = cls._fence(annotations.get("exomem.io/fence"))
        cls._require_digest(annotations.get("exomem.io/tenant-digest"), tenant_id, length=64)
        cls._require_digest(annotations.get("exomem.io/subject-digest"), cell_id, length=64)
        cls._require_digest(annotations.get("exomem.io/operation-digest"), operation_id, length=64)
        identity_codec.authenticate(
            cls._envelope(annotations.get("exomem.io/recovery-envelope")),
            provider=provider,
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence_generation=fence,
        )
        return cls._observation(
            provider=provider,
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence=fence,
            observed_at=observed_at,
        )

    @classmethod
    def hcloud(
        cls,
        *,
        provider_reference: str,
        labels: dict[str, str],
        observed_at: datetime,
        identity_codec: ProviderIdentityAuthenticator,
    ) -> ProviderMetadataObservation:
        tenant_id = cls._decode_chunks(labels, "exomem_tenant_id")
        cell_id = cls._decode_chunks(labels, "exomem_cell_id")
        operation_id = cls._decode_chunks(labels, "exomem_operation_id")
        fence = cls._fence(labels.get("exomem_fence"))
        cls._require_digest(labels.get("exomem_tenant"), tenant_id, length=24)
        cls._require_digest(labels.get("exomem_subject"), cell_id, length=24)
        cls._require_digest(labels.get("exomem_operation"), operation_id, length=24)
        identity_codec.authenticate(
            cls._decode_chunks(labels, "exomem_identity", envelope=True),
            provider="hcloud",
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence_generation=fence,
        )
        return cls._observation(
            provider="hcloud",
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence=fence,
            observed_at=observed_at,
        )

    @classmethod
    def b2(
        cls,
        *,
        provider_reference: str,
        metadata: dict[str, str],
        observed_at: datetime,
        identity_codec: ProviderIdentityAuthenticator,
    ) -> ProviderMetadataObservation:
        tenant_id = cls._opaque(metadata.get("tenant-id"))
        cell_id = cls._opaque(metadata.get("cell-id"))
        operation_id = cls._opaque(metadata.get("operation-id"))
        fence = cls._fence(metadata.get("fence-generation"))
        identity_codec.authenticate(
            cls._envelope(metadata.get("identity-envelope")),
            provider="b2",
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence_generation=fence,
        )
        return cls._observation(
            provider="b2",
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence=fence,
            observed_at=observed_at,
        )

    @classmethod
    def _decode_chunks(cls, labels: dict[str, str], prefix: str, *, envelope: bool = False) -> str:
        raw_count = labels.get(f"{prefix}_n", "")
        maximum_chunks = 48 if envelope else 8
        if not raw_count.isdigit() or not 1 <= int(raw_count) <= maximum_chunks:
            raise ProviderMetadataConflict("HCloud recovery identity chunk count is invalid")
        count = int(raw_count)
        expected = {f"{prefix}_n", *(f"{prefix}_{index}" for index in range(count))}
        actual = {key for key in labels if key.startswith(f"{prefix}_")}
        if actual != expected:
            raise ProviderMetadataConflict("HCloud recovery identity chunks are ambiguous")
        chunks = [labels[f"{prefix}_{index}"] for index in range(count)]
        if any(not _BASE32_CHUNK.fullmatch(chunk) for chunk in chunks):
            raise ProviderMetadataConflict("HCloud recovery identity chunk is invalid")
        encoded = "".join(chunks).upper()
        encoded += "=" * ((8 - len(encoded) % 8) % 8)
        try:
            decoded = base64.b32decode(encoded, casefold=False).decode("ascii")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ProviderMetadataConflict(
                "HCloud recovery identity is not canonical base32"
            ) from error
        value = cls._envelope(decoded) if envelope else cls._opaque(decoded)
        canonical = base64.b32encode(value.encode("ascii")).decode("ascii").rstrip("=").lower()
        if canonical != "".join(chunks):
            raise ProviderMetadataConflict("HCloud recovery identity is not canonical base32")
        return value

    @staticmethod
    def _opaque(value: str | None) -> str:
        if value is None or not _OPAQUE_ID.fullmatch(value):
            raise ProviderMetadataConflict("provider recovery identity is invalid")
        return value

    @staticmethod
    def _envelope(value: str | None) -> str:
        if value is None or not _ENVELOPE.fullmatch(value):
            raise ProviderMetadataConflict("provider recovery identity did not authenticate")
        return value

    @staticmethod
    def _fence(value: str | None) -> int:
        if value is None or not value.isdigit() or int(value) < 1:
            raise ProviderMetadataConflict("provider recovery fence is invalid")
        return int(value)

    @staticmethod
    def _require_digest(actual: str | None, value: str, *, length: int) -> None:
        expected = hashlib.sha256(value.encode()).hexdigest()[:length]
        if actual != expected:
            raise ProviderMetadataConflict("provider recovery identity digest differs")

    @staticmethod
    def _observation(
        *,
        provider: str,
        provider_reference: str,
        tenant_id: str,
        cell_id: str,
        operation_id: str,
        fence: int,
        observed_at: datetime,
    ) -> ProviderMetadataObservation:
        if not provider_reference:
            raise ProviderMetadataConflict("provider recovery reference is invalid")
        return ProviderMetadataObservation(
            provider=provider,
            provider_reference=provider_reference,
            tenant_id=tenant_id,
            cell_id=cell_id,
            operation_id=operation_id,
            fence_generation=fence,
            observed_at=observed_at,
            metadata_authenticated=True,
        )


class ProviderScanner(Protocol):
    provider: str

    async def scan(self) -> list[ProviderMetadataObservation]: ...


class PaginatedProviderScanner:
    """Read every provider page and reject looping/ambiguous cursors."""

    def __init__(
        self,
        *,
        provider: str,
        page_reader: Callable[
            [str | None], Awaitable[tuple[list[ProviderMetadataObservation], str | None]]
        ],
    ) -> None:
        if provider not in _REQUIRED_PROVIDERS:
            raise ValueError("provider scanner kind is invalid")
        self.provider = provider
        self._page_reader = page_reader

    async def scan(self) -> list[ProviderMetadataObservation]:
        values: list[ProviderMetadataObservation] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page, next_token = await self._page_reader(token)
            if any(value.provider != self.provider for value in page):
                raise ProviderMetadataConflict("provider page returned another identity kind")
            values.extend(page)
            if next_token is None:
                return values
            if not next_token or next_token in seen_tokens:
                raise ProviderMetadataConflict("provider pagination cursor did not advance")
            seen_tokens.add(next_token)
            token = next_token


@dataclass(frozen=True, slots=True)
class ProviderRediscoveryResult:
    maximum_fences: dict[str, int]
    observations: tuple[RediscoveryObservationSnapshot, ...]


class ProviderRediscoveryGate:
    """Finishes all provider reads before raising fences and classifying side effects."""

    def __init__(
        self, *, repository: DurabilityRepository, scanners: list[ProviderScanner]
    ) -> None:
        providers = [scanner.provider for scanner in scanners]
        if len(providers) != len(set(providers)) or set(providers) != _REQUIRED_PROVIDERS:
            raise ValueError(
                "provider rediscovery requires exactly Kubernetes, HCloud, Traefik, and B2"
            )
        self._repository = repository
        self._scanners = scanners

    async def reconcile(self) -> ProviderRediscoveryResult:
        # Deliberately scan to completion first. No adoption, quarantine, fence,
        # or provider mutation occurs until every external failure domain was read.
        observed: list[ProviderMetadataObservation] = []
        for scanner in self._scanners:
            values = await scanner.scan()
            if any(value.provider != scanner.provider for value in values):
                raise ProviderMetadataConflict("scanner returned another provider identity")
            observed.extend(values)
        deduplicated: dict[tuple[str, str], ProviderMetadataObservation] = {}
        for value in observed:
            if (
                not value.metadata_authenticated
                or not value.provider_reference
                or not value.tenant_id
                or not value.operation_id
                or value.fence_generation < 1
            ):
                raise ProviderMetadataConflict("provider metadata is unauthenticated or invalid")
            key = (value.provider, value.provider_reference)
            prior = deduplicated.get(key)
            if prior is not None and prior != value:
                raise ProviderMetadataConflict("one provider reference has conflicting identity")
            deduplicated[key] = value
        complete = list(deduplicated.values())
        maximum_fences: dict[str, int] = {}
        for value in complete:
            maximum_fences[value.tenant_id] = max(
                maximum_fences.get(value.tenant_id, 0), value.fence_generation
            )
        snapshots = await self._repository.reconcile_provider_observations(
            [
                RediscoveryObservationInput(
                    provider=value.provider,
                    provider_reference=value.provider_reference,
                    tenant_id=value.tenant_id,
                    cell_id=value.cell_id,
                    operation_id=value.operation_id,
                    fence_generation=value.fence_generation,
                    observed_at=value.observed_at,
                )
                for value in complete
            ],
            maximum_fences=maximum_fences,
        )
        return ProviderRediscoveryResult(
            maximum_fences=maximum_fences,
            observations=tuple(snapshots),
        )
