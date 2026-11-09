"""Deletion grants (automate-cell-storage-deletion D1, D5).

Substrate signs a deletion grant when it deletes a cell. cellctl relays each grant into one ConfigMap and
cannot forge one. The key custodian verifies each grant here before it acts. The custodian runs the cellctl
image, which holds no `exomem` package, so the media edge has its own verifier in `src/exomem`.
`infra/contracts/cell-deletion-grants-v1.json` holds the grants that both verifiers accept or refuse alike.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .db import CELL_ID_RE

logger = logging.getLogger(__name__)

# The ConfigMap that cellctl relays the grants into, in its own namespace, and its one key: a JSON array of
# grant tokens. The chart's admission exception names the same two strings.
DELETIONS_CONFIGMAP = "exomem-cell-deletions"
GRANTS_KEY = "grants.json"

GRANT_AUDIENCE = "exomem-cell-delete"
# The grant's closed claim set, fixed by the contract with Substrate. It has no `exp`: a deletion is final.
GRANT_CLAIMS = frozenset({"aud", "iat", "jti", "cell_id", "purge_after"})
# A bad grace setting in Substrate must not remove the grace: a shorter one is refused.
MIN_GRACE_SECONDS = 86_400
MAX_GRANT_CHARS = 8192
# Substrate's clock may run a little ahead of this one when it signs `iat`.
_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class DeletionGrant:
    cell_id: str
    iat: int
    purge_after: int


def load_public_keys(raw: bytes) -> list[Ed25519PublicKey]:
    """The pinned grant public keys: a JSON array of PEM strings, the current key and, during a rotation,
    the next one. Raises on any key that is not Ed25519, so a bad chart value stops the start."""

    pems = json.loads(raw)
    if not isinstance(pems, list) or not all(isinstance(item, str) for item in pems):
        raise ValueError("the grant public keys are not a JSON array of PEM strings")
    keys = [serialization.load_pem_public_key(item.encode()) for item in pems]
    if not all(isinstance(key, Ed25519PublicKey) for key in keys):
        raise ValueError("every grant public key must be Ed25519")
    return keys


def verify_grant(token: object, keys: Sequence[Ed25519PublicKey]) -> DeletionGrant | None:
    """The grant's verified claims, or None. The cell ID comes from these claims alone."""

    if not isinstance(token, str) or not token or len(token) > MAX_GRANT_CHARS:
        return None
    try:
        if jwt.get_unverified_header(token).get("typ") != "JWT":
            return None
    except jwt.PyJWTError:
        return None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=["EdDSA"], audience=GRANT_AUDIENCE,
                                options={"require": sorted(GRANT_CLAIMS)}, leeway=_LEEWAY_SECONDS)
        except (jwt.PyJWTError, ValueError, TypeError):
            continue
        if (set(claims) != GRANT_CLAIMS or claims["aud"] != GRANT_AUDIENCE
                or type(claims["iat"]) is not int or type(claims["purge_after"]) is not int
                or claims["purge_after"] < claims["iat"] + MIN_GRACE_SECONDS
                or not isinstance(claims["jti"], str) or not claims["jti"]
                or not isinstance(claims["cell_id"], str) or not CELL_ID_RE.fullmatch(claims["cell_id"])):
            return None
        return DeletionGrant(claims["cell_id"], claims["iat"], claims["purge_after"])
    return None


def verified_grants(raw: str | None, keys: Sequence[Ed25519PublicKey]) -> dict[str, DeletionGrant]:
    """The valid grants of the relayed key, by cell. Each bad entry is skipped on its own and counted in the
    log. Two valid grants for one cell keep the later `purge_after`, so no grant makes a purge come sooner."""

    if raw is None:
        return {}
    try:
        entries = json.loads(raw)
    except ValueError:
        entries = None
    if not isinstance(entries, list):
        logger.error("deletion grants: the relayed list is malformed; no grant is read from it")
        return {}
    grants: dict[str, DeletionGrant] = {}
    refused = 0
    for entry in entries:
        grant = verify_grant(entry, keys)
        if grant is None:
            refused += 1
            continue
        known = grants.get(grant.cell_id)
        if known is None or grant.purge_after > known.purge_after:
            grants[grant.cell_id] = grant
    if refused:
        logger.error("deletion grants: %d relayed entries refused", refused)
    return grants


def read_hold_list(path: Path) -> frozenset[str] | None:
    """The operator's hold list (D5): the cells that no executor purges. None when the file cannot be read
    or parsed. The caller then purges nothing, because a hold that is set must never be lost to a bad read;
    the cost is a delay that the watcher's anomaly alert reports."""

    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)
