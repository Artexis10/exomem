"""Root-authenticated provider identity envelopes for disaster recovery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_ENVELOPE: Final = re.compile(r"^[A-Za-z0-9_-]{40,4096}$")
_BASE32_CHUNK: Final = re.compile(r"^[a-z2-7]{1,52}$")
_IDENTITY_ID: Final = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")


class ProviderIdentityConflict(RuntimeError):
    """Provider metadata is absent, forged, copied, or bound to another object."""


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
            decoded = _decode(value.removeprefix(cls._PREFIX))
            parsed = json.loads(decoded.decode("ascii"))
            if (
                not isinstance(parsed, dict)
                or _canonical(parsed) != decoded
                or parsed.get("version") != 1
            ):
                raise ValueError("provider reference is not canonical")
            return parsed
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as error:
            raise ProviderIdentityConflict("provider recovery reference is invalid") from error

    @classmethod
    def _build(cls, value: dict[str, object]) -> str:
        if any(
            not isinstance(item, str) or not item
            for key, item in value.items()
            if key != "version" and key != "namespace"
        ):
            raise ValueError("provider reference fields must be non-empty strings")
        return cls._PREFIX + _encode(_canonical(value))


_CELL_KUBERNETES_OBJECTS: Final[dict[str, tuple[str, str, str]]] = {
    "namespace": ("v1", "Namespace", ""),
    "vaultPvc": ("v1", "PersistentVolumeClaim", "{resource}-data"),
    "credentialSecret": ("v1", "Secret", "exomem-cell-credentials"),
    "serviceAccount": ("v1", "ServiceAccount", "{resource}"),
    "initRequestConfigMap": ("v1", "ConfigMap", "{resource}-init-request"),
    "providerOperationConfigMap": ("v1", "ConfigMap", "{operation_resource}"),
    "initJob": ("batch/v1", "Job", "{resource}-init"),
    "defaultDenyNetworkPolicy": (
        "networking.k8s.io/v1",
        "NetworkPolicy",
        "{resource}-default-deny",
    ),
    "traefikIngressNetworkPolicy": (
        "networking.k8s.io/v1",
        "NetworkPolicy",
        "{resource}-traefik-ingress",
    ),
    "resourceQuota": ("v1", "ResourceQuota", "{resource}-quota"),
    "limitRange": ("v1", "LimitRange", "{resource}-limits"),
    "service": ("v1", "Service", "{resource}"),
    "statefulSet": ("apps/v1", "StatefulSet", "{resource}"),
}
_CELL_TRAEFIK_OBJECTS: Final[dict[str, tuple[str, str, str]]] = {
    "stripCellMiddleware": (
        "traefik.io/v1alpha1",
        "Middleware",
        "{resource}-strip-cell",
    ),
    "controlIngressRoute": (
        "traefik.io/v1alpha1",
        "IngressRoute",
        "{resource}-control",
    ),
    "transferIngressRoute": (
        "traefik.io/v1alpha1",
        "IngressRoute",
        "{resource}-transfer",
    ),
}


def cell_provider_recovery_envelopes(
    codec: ProviderRecoveryIdentityCodec,
    *,
    tenant_id: str,
    cell_id: str,
    operation_id: str,
    fence_generation: int,
    resource_name: str,
    operation_resource_name: str,
) -> dict[str, str]:
    """Sign each exact cell object separately; envelopes are never reusable."""

    common = {
        "tenant_id": tenant_id,
        "cell_id": cell_id,
        "operation_id": operation_id,
        "fence_generation": fence_generation,
    }
    result: dict[str, str] = {}
    names = {
        "resource": resource_name,
        "operation_resource": operation_resource_name,
    }
    for key, (api_version, kind, name_template) in _CELL_KUBERNETES_OBJECTS.items():
        name = name_template.format_map(names) if name_template else resource_name
        namespace = "" if kind == "Namespace" else resource_name
        reference = ProviderReference.kubernetes(
            provider="kubernetes",
            api_version=api_version,
            kind=kind,
            namespace=namespace,
            name=name,
        )
        result[key] = codec.seal(
            provider="kubernetes",
            provider_reference=reference,
            **common,
        )
    for key, (api_version, kind, name_template) in _CELL_TRAEFIK_OBJECTS.items():
        name = name_template.format_map(names)
        reference = ProviderReference.kubernetes(
            provider="traefik",
            api_version=api_version,
            kind=kind,
            namespace=resource_name,
            name=name,
        )
        result[key] = codec.seal(
            provider="traefik",
            provider_reference=reference,
            **common,
        )
    return result


def authenticate_cell_provider_recovery_envelopes(
    verifier: ProviderRecoveryIdentityVerifier,
    envelopes: object,
    *,
    tenant_id: str,
    cell_id: str,
    operation_id: str,
    fence_generation: int,
    resource_name: str,
    operation_resource_name: str,
) -> dict[str, str]:
    """Verify the exact complete envelope set before any provider mutation."""

    expected_keys = set(_CELL_KUBERNETES_OBJECTS) | set(_CELL_TRAEFIK_OBJECTS)
    if (
        not isinstance(envelopes, dict)
        or set(envelopes) != expected_keys
        or any(not isinstance(value, str) for value in envelopes.values())
        or len(set(envelopes.values())) != len(expected_keys)
    ):
        raise ProviderIdentityConflict("provider recovery envelope set is invalid")
    values = {str(key): str(value) for key, value in envelopes.items()}
    names = {
        "resource": resource_name,
        "operation_resource": operation_resource_name,
    }
    common = {
        "tenant_id": tenant_id,
        "cell_id": cell_id,
        "operation_id": operation_id,
        "fence_generation": fence_generation,
    }
    for key, (api_version, kind, name_template) in _CELL_KUBERNETES_OBJECTS.items():
        name = name_template.format_map(names) if name_template else resource_name
        verifier.authenticate(
            values[key],
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version=api_version,
                kind=kind,
                namespace="" if kind == "Namespace" else resource_name,
                name=name,
            ),
            **common,
        )
    for key, (api_version, kind, name_template) in _CELL_TRAEFIK_OBJECTS.items():
        verifier.authenticate(
            values[key],
            provider="traefik",
            provider_reference=ProviderReference.kubernetes(
                provider="traefik",
                api_version=api_version,
                kind=kind,
                namespace=resource_name,
                name=name_template.format_map(names),
            ),
            **common,
        )
    return values


def cell_resource_name(cell_id: str) -> str:
    return "exo-" + hashlib.sha256(cell_id.encode("utf-8")).hexdigest()[:20]


def provider_operation_resource_name(operation_id: str) -> str:
    return "exo-op-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:20]


class ProviderRecoveryIdentityCodec:
    """Ed25519 signer held only by API/privileged provider workloads."""

    _DOMAIN = b"exomem.provider-recovery-identity.v1\x00"

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("provider recovery signing seed must be 32 bytes")
        self._private_key = Ed25519PrivateKey.from_private_bytes(key)

    @classmethod
    def from_secret(cls, secret: bytes | str) -> ProviderRecoveryIdentityCodec:
        """Derive a deterministic seed from arbitrary test/development material."""

        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        return cls(hashlib.sha256(raw).digest())

    @classmethod
    def from_encoded_seed(cls, value: str) -> ProviderRecoveryIdentityCodec:
        """Load the base64url-no-pad raw seed emitted by the SOPS handoff."""

        try:
            raw = _decode(value)
        except (ValueError, binascii.Error) as error:
            raise ValueError("provider recovery signing seed is invalid") from error
        return cls(raw)

    def public_key(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _encode(raw)

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
        payload_bytes = _canonical(payload)
        signature = self._private_key.sign(self._DOMAIN + payload_bytes)
        return _encode(
            _canonical(
                {
                    "alg": "Ed25519",
                    "payload": _encode(payload_bytes),
                    "signature": _encode(signature),
                    "version": 1,
                }
            )
        )


class ProviderRecoveryIdentityVerifier:
    """Public-key verifier safe for routine reconciliation and rediscovery."""

    _DOMAIN = ProviderRecoveryIdentityCodec._DOMAIN

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._public_key = public_key

    @classmethod
    def from_public_key(cls, value: str) -> ProviderRecoveryIdentityVerifier:
        try:
            return cls(Ed25519PublicKey.from_public_bytes(_decode(value)))
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
        payload = self.claims(envelope)
        if payload != {
            "provider": provider,
            "providerReference": provider_reference,
            "tenantId": tenant_id,
            "cellId": cell_id,
            "operationId": operation_id,
            "fenceGeneration": fence_generation,
        }:
            raise ProviderIdentityConflict("provider recovery identity did not authenticate")

    def claims(self, envelope: str) -> dict[str, object]:
        """Return bounded claims only after canonical Ed25519 verification."""

        try:
            if not _ENVELOPE.fullmatch(envelope):
                raise ValueError("identity envelope shape is invalid")
            envelope_bytes = _decode(envelope)
            value = json.loads(envelope_bytes.decode("ascii"))
            if not isinstance(value, dict) or _canonical(value) != envelope_bytes:
                raise ValueError("identity envelope is not canonical")
            if value.get("version") != 1 or value.get("alg") != "Ed25519":
                raise ValueError("identity envelope algorithm is invalid")
            payload_bytes = _decode(str(value["payload"]))
            payload = json.loads(payload_bytes.decode("ascii"))
            if not isinstance(payload, dict) or _canonical(payload) != payload_bytes:
                raise ValueError("identity payload is not canonical")
            self._public_key.verify(_decode(str(value["signature"])), self._DOMAIN + payload_bytes)
            if set(payload) != {
                "provider",
                "providerReference",
                "tenantId",
                "cellId",
                "operationId",
                "fenceGeneration",
            }:
                raise ValueError("identity payload fields are invalid")
            provider = payload["provider"]
            reference = payload["providerReference"]
            if provider not in {"kubernetes", "traefik", "hcloud", "b2"}:
                raise ValueError("identity payload provider is invalid")
            if not isinstance(reference, str):
                raise ValueError("identity payload provider reference is invalid")
            parsed_reference = ProviderReference.parse(reference)
            if parsed_reference.get("provider") != provider:
                raise ValueError("identity payload provider reference differs")
            if any(
                not isinstance(payload[key], str) or not _IDENTITY_ID.fullmatch(str(payload[key]))
                for key in ("tenantId", "cellId", "operationId")
            ):
                raise ValueError("identity payload subject is invalid")
            fence = payload["fenceGeneration"]
            if (
                not isinstance(fence, int)
                or isinstance(fence, bool)
                or not 1 <= fence <= 9_007_199_254_740_991
            ):
                raise ValueError("identity payload fence is invalid")
        except (
            InvalidSignature,
            ValueError,
            KeyError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as error:
            raise ProviderIdentityConflict(
                "provider recovery identity did not authenticate"
            ) from error
        return payload


def chunk_hcloud_identity_envelope(envelope: str) -> dict[str, str]:
    """Encode an authenticated envelope into canonical HCloud label chunks."""

    if not _ENVELOPE.fullmatch(envelope):
        raise ProviderIdentityConflict("provider recovery identity did not authenticate")
    encoded = base64.b32encode(envelope.encode("ascii")).decode("ascii").rstrip("=").lower()
    chunks = [encoded[offset : offset + 52] for offset in range(0, len(encoded), 52)]
    if not 1 <= len(chunks) <= 48:
        raise ProviderIdentityConflict("HCloud recovery envelope exceeds label capacity")
    return {
        "exomem_identity_n": str(len(chunks)),
        **{f"exomem_identity_{index}": chunk for index, chunk in enumerate(chunks)},
    }


def decode_hcloud_identity_envelope(labels: dict[str, str]) -> str:
    """Decode only an exact, canonical HCloud recovery envelope label set."""

    raw_count = labels.get("exomem_identity_n", "")
    if not raw_count.isdigit() or not 1 <= int(raw_count) <= 48:
        raise ProviderIdentityConflict("HCloud recovery envelope chunk count is invalid")
    count = int(raw_count)
    expected = {"exomem_identity_n", *(f"exomem_identity_{index}" for index in range(count))}
    actual = {key for key in labels if key.startswith("exomem_identity_")}
    if actual != expected:
        raise ProviderIdentityConflict("HCloud recovery envelope chunks are ambiguous")
    chunks = [labels[f"exomem_identity_{index}"] for index in range(count)]
    if any(not _BASE32_CHUNK.fullmatch(chunk) for chunk in chunks):
        raise ProviderIdentityConflict("HCloud recovery envelope chunk is invalid")
    encoded = "".join(chunks).upper()
    encoded += "=" * ((8 - len(encoded) % 8) % 8)
    try:
        value = base64.b32decode(encoded, casefold=False).decode("ascii")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ProviderIdentityConflict("HCloud recovery envelope is not canonical") from error
    if not _ENVELOPE.fullmatch(value):
        raise ProviderIdentityConflict("provider recovery identity did not authenticate")
    canonical = base64.b32encode(value.encode("ascii")).decode("ascii").rstrip("=").lower()
    if canonical != "".join(chunks):
        raise ProviderIdentityConflict("HCloud recovery envelope is not canonical")
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
