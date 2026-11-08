"""The platform side of the Exomem Cloud media edge (add-cloud-media-store D4-D6).

cellctl mints a cell's media key bundle, signs the cell's edge credential, plans
the active set the edge loads, and reads the generation the edge reports. The
functions here are pure, apart from one HTTP read; reconcile.py owns the
database and the cluster.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .db import CELL_ID_RE
from .manifests import MEDIA_STORE_OWNED_KEYS, check_media_edge_url
from .secrets import CONTENT_KEY_VERSION, unwrap_secret, wrap_secret

# The credential's audience and format version: the edge's contract with cellctl.
EDGE_AUDIENCE = "exomem-media-edge"
# collect-released-media D3: the backup Job's credential, which may only condemn. The cell's copy is
# COLLECTOR_AUDIENCE in src/exomem/media_edge_protocol.py; tests/test_media_collection.py fails when they differ.
COLLECTOR_AUDIENCE = "exomem-media-edge-collector"
CREDENTIAL_FORMAT = 1
# The purpose the cell root binds into the wrapped bundle (see secrets._aad).
BUNDLE_COLUMN = "media_key_wrapped"
# The ConfigMap the edge mounts. The first key is the edge's file; cellctl alone reads the second.
ACTIVE_SET_NAME = "exomem-media-edge-active-set"
ACTIVE_SET_KEY = "active-set.json"
EPOCHS_KEY = "credential-epochs.json"
MAX_SELECTED_CELLS = 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")


# -- the media key bundle: an age X25519 identity and the object name key --

# Plain bech32 (BIP 173) is how age spells its keys. Python's standard library
# has no encoder, so this is the one the keys need, checked against `age` itself.
_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_AGE_RECIPIENT_HRP = "age"
_AGE_IDENTITY_HRP = "age-secret-key-"


def _bech32_polymod(values: Iterable[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for bit, generator in enumerate(_BECH32_GENERATOR):
            if (top >> bit) & 1:
                checksum ^= generator
    return checksum


def _bech32_encode(hrp: str, data: bytes) -> str:
    groups: list[int] = []
    accumulator = bits = 0
    for byte in data:
        accumulator = (accumulator << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            groups.append((accumulator >> bits) & 31)
        accumulator &= (1 << bits) - 1
    if bits:
        groups.append((accumulator << (5 - bits)) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    checksum = _bech32_polymod(expanded + groups + [0] * 6) ^ 1
    groups += [(checksum >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_ALPHABET[group] for group in groups)


@dataclass(frozen=True)
class MediaBundle:
    """What the cell's runtime needs: `age` reads the identity and the recipient, the object names use the name key."""

    identity: str = field(repr=False)
    recipient: str
    name_key: bytes = field(repr=False)


def mint_bundle() -> bytes:
    """The raw bundle: the X25519 secret, then the name key, 32 bytes each."""
    return X25519PrivateKey.generate().private_bytes_raw() + os.urandom(32)


def open_bundle(raw: bytes) -> MediaBundle:
    if len(raw) != 64:
        raise ValueError("the media key bundle is malformed")
    secret, name_key = raw[:32], raw[32:]
    public = X25519PrivateKey.from_private_bytes(secret).public_key().public_bytes_raw()
    return MediaBundle(
        identity=_bech32_encode(_AGE_IDENTITY_HRP, secret).upper(),
        recipient=_bech32_encode(_AGE_RECIPIENT_HRP, public),
        name_key=name_key,
    )


def wrap_bundle(cell_root: bytes, raw: bytes, *, cell_id: str) -> bytes:
    """D5: AES-GCM under the cell root, with the cell, purpose and version as additional data."""
    return wrap_secret(cell_root, raw, cell_id=cell_id, column=BUNDLE_COLUMN, key_version=CONTENT_KEY_VERSION)


def unwrap_bundle(cell_root: bytes, wrapped: bytes, *, cell_id: str, key_version: int) -> bytes:
    return unwrap_secret(cell_root, wrapped, cell_id=cell_id, column=BUNDLE_COLUMN, key_version=key_version)


# -- the per-cell edge credential --


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _compact_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def sign_credential(key: Ed25519PrivateKey, *, cell_id: str, version: int, audience: str = EDGE_AUDIENCE) -> str:
    """A compact EdDSA JWT that binds the audience, the cell and the credential version.

    It has no expiry: the active set revokes it. `issued_ms` equals the version, which is the
    generation at which the credential was admitted, so the same inputs always sign to the same
    credential and a pass that changes nothing renders an unchanged Secret. Ed25519 signing is
    deterministic.
    """
    header = _b64url(_compact_json({"alg": "EdDSA", "typ": "JWT"}))
    claims = {"v": CREDENTIAL_FORMAT, "aud": audience, "sub": cell_id, "ver": version, "issued_ms": version}
    signing_input = f"{header}.{_b64url(_compact_json(claims))}"
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode('ascii')))}"


# -- the active set --


@dataclass(frozen=True)
class ActiveSet:
    """The set of cells the edge admits, each with its current credential version.

    `generation` rises with every change to the set. `cells` admits runtime credentials and
    `collectors` admits backup Job credentials (collect-released-media D3). `epochs` is cellctl's own
    record of the credential epoch each version was admitted under; the edge never reads it.
    """

    generation: int = 0
    cells: Mapping[str, int] = field(default_factory=dict)
    epochs: Mapping[str, int] = field(default_factory=dict)
    collectors: Mapping[str, int] = field(default_factory=dict)

    def active_set_json(self) -> str:
        document: dict[str, object] = {"generation": self.generation, "cells": dict(self.cells)}
        if self.collectors:
            # A set with no collector keeps the bytes that an edge without collection loads.
            document["collectors"] = dict(self.collectors)
        return _compact_json(document).decode("ascii")

    def sha256(self) -> str:
        """The digest of the bytes the edge loads from the ConfigMap, which it reports back."""
        return hashlib.sha256(self.active_set_json().encode("ascii")).hexdigest()

    def epochs_json(self) -> str:
        return _compact_json(dict(self.epochs)).decode("ascii")


def _cell_integers(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or not all(
        isinstance(cell, str) and CELL_ID_RE.fullmatch(cell) and type(number) is int and number >= 0
        for cell, number in value.items()
    ):
        raise ValueError("expected an object of cell IDs to whole numbers")
    return dict(value)


def read_active_set(data: Mapping[str, str] | None) -> ActiveSet:
    """The set last published, or an empty one when none was or the published one is unreadable.

    An unreadable ConfigMap is replaced, not trusted. New versions come from the clock as well as
    from the last generation, so nothing the replaced set admitted is admitted again.
    """
    if not data or ACTIVE_SET_KEY not in data:
        return ActiveSet()
    try:
        document = json.loads(data[ACTIVE_SET_KEY])
        generation = document["generation"]
        if type(generation) is not int or generation < 0:
            raise ValueError("the generation is not a whole number")
        return ActiveSet(generation, _cell_integers(document["cells"]), _cell_integers(json.loads(data.get(EPOCHS_KEY, "{}"))),
                         _cell_integers(document.get("collectors", {})))
    except (ValueError, KeyError, TypeError):
        return ActiveSet()


def plan_active_set(
    published: ActiveSet, selected: Mapping[str, int], now_ms: int, *, above: int = 0,
    collectors: Mapping[str, int] | None = None,
) -> ActiveSet:
    """The set to publish for the selected cells and the collecting cells, each with its credential epoch.

    A cell keeps its version while its epoch is unchanged. A new cell, and a cell whose epoch changed,
    takes the new generation as its version, which no earlier credential carries. A cell that leaves the set
    loses its version, and a later return starts afresh, so a revoked credential stays revoked. Collectors
    follow the same rule in their own map, under the cell's one epoch.

    `above` is the generation of a set the edge loaded that cellctl did not publish, for example after a
    clock that went back. The edge refuses a generation at or below the one it loaded, so the plan rises past it.
    """
    collectors = {} if collectors is None else collectors
    epochs = {**collectors, **selected}
    # Zero is unpublished state; an empty revocation must still advance the generation.
    if (published.generation > max(above, 0) and published.cells.keys() == selected.keys()
            and published.collectors.keys() == collectors.keys()
            and all(published.epochs.get(cell, 0) == epoch for cell, epoch in epochs.items())):
        return published
    generation = max(published.generation + 1, now_ms, above + 1)

    def versions(admitted: Mapping[str, int], wanted: Mapping[str, int]) -> dict[str, int]:
        return {cell: admitted[cell] if cell in admitted and published.epochs.get(cell, 0) == epoch else generation
                for cell, epoch in wanted.items()}

    return ActiveSet(generation, versions(published.cells, selected), epochs, versions(published.collectors, collectors))


def render_active_set(active: ActiveSet, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ACTIVE_SET_NAME,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": "exomem-media-edge", "app.kubernetes.io/part-of": "exomem-cloud"},
        },
        "data": {ACTIVE_SET_KEY: active.active_set_json(), EPOCHS_KEY: active.epochs_json()},
    }


@dataclass(frozen=True)
class EdgeEvidence:
    """What the edge reports about the set it serves (add-cloud-media-store D6, exact-set evidence).

    `settled` is the newest generation whose earlier requests and writes have finished. `loaded` is the
    generation of the set in force, `set_sha256` the digest of that set's bytes, and `bucket` the bucket
    the edge writes to. A generation alone cannot say which set it was.
    """

    settled: int
    loaded: int
    set_sha256: str
    bucket: str

    def matches(self, active: ActiveSet, bucket: str) -> bool:
        """Whether the edge serves exactly the set cellctl published, on the bucket cellctl expects."""
        return (
            self.loaded == active.generation
            and self.set_sha256 == active.sha256()
            and self.bucket == bucket
            and self.settled >= active.generation
        )


def read_edge_evidence(url: str, *, timeout: float = 3.0) -> EdgeEvidence:
    """The edge's report of the set it serves. Anything it does not say in full is an error, not a guess."""
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(f"{url.rstrip('/')}/v1/generation")
    response.raise_for_status()
    report = response.json()
    settled, loaded, digest, bucket = (report.get(name) for name in ("generation", "loaded", "set_sha256", "bucket"))
    if (
        any(type(number) is not int or number < 0 for number in (settled, loaded))
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None  # a hex digest's shape, fixed by SHA-256
        or not isinstance(bucket, str)
        or not bucket
    ):
        raise ValueError("the media edge reported no complete generation")
    return EdgeEvidence(settled, loaded, digest, bucket)


# -- cellctl's settings for the edge --


@dataclass(frozen=True)
class MediaEdgeConfig:
    """The chart's media edge settings: the endpoint, the cells it serves and the policy cells receive."""

    url: str
    max_object_bytes: int
    signing_key: Ed25519PrivateKey = field(repr=False)
    # The bucket the edge must report. An edge on another bucket proves nothing about this one.
    bucket: str
    cell_ids: tuple[str, ...] = ()
    # The fleet switch: every cell, including each created later. The list stays as the canary.
    all_cells: bool = False
    # Raise a cell's number to replace its credential and revoke the old one.
    credential_epochs: Mapping[str, int] = field(default_factory=dict)
    # Eviction and hydration settings the runtime reads beside the keys.
    store_policy: Mapping[str, bool | int | float] = field(default_factory=dict)
    # D10: the physical meter alerts above the cell's allowance times this factor, which leaves room for
    # versions that stay until the follow-on change destroys them. None leaves that alert off.
    physical_headroom: float | None = None

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("the media edge needs a literal endpoint")
        check_media_edge_url(self.url)
        if not isinstance(self.signing_key, Ed25519PrivateKey):
            raise ValueError("the media edge credential signing key must be an Ed25519 private key")
        if not isinstance(self.bucket, str) or not self.bucket:
            raise ValueError("the media edge needs the name of its bucket")
        if type(self.max_object_bytes) is not int or self.max_object_bytes <= 0:
            raise ValueError("the media edge object bound must be a positive whole number of bytes")
        if (
            not isinstance(self.cell_ids, tuple)
            or len(self.cell_ids) > MAX_SELECTED_CELLS
            or len(set(self.cell_ids)) != len(self.cell_ids)
            or not all(isinstance(cell, str) and CELL_ID_RE.fullmatch(cell) for cell in self.cell_ids)
        ):
            raise ValueError("the media edge selection needs at most 1024 unique base32 cell IDs")
        if self.physical_headroom is not None and not (
            type(self.physical_headroom) in (int, float) and math.isfinite(self.physical_headroom)
            and self.physical_headroom >= 1
        ):
            raise ValueError("the media physical headroom factor is a number of at least 1")
        try:
            _cell_integers(dict(self.credential_epochs))
        except ValueError as error:
            raise ValueError("the media edge credential epochs map cell IDs to whole numbers") from error
        if not all(
            isinstance(key, str) and key not in MEDIA_STORE_OWNED_KEYS and type(value) in (bool, int, float)
            for key, value in self.store_policy.items()
        ):
            raise ValueError("the media store policy holds only settings that cellctl does not render itself")

    def selects(self, cell_id: str) -> bool:
        return self.all_cells or cell_id in self.cell_ids

    def epoch(self, cell_id: str) -> int:
        return self.credential_epochs.get(cell_id, 0)

