"""Owner policy for verified original copies and independent failure domains."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import originals

POLICY_FIELD = "protection"


class ProtectionPolicyError(ValueError):
    """The current owner policy cannot be applied."""


class ProtectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_verified_copies: int = Field(ge=1, lt=2**63)
    minimum_failure_domains: int = Field(ge=1, lt=2**63)
    copy_bytes_per_month: int = Field(ge=0, lt=2**63)
    replica_stores: tuple[str, ...]
    local_failure_domain: str | None = Field(default=None, max_length=1024)
    eviction_authorized: bool = False

    @field_validator("replica_stores", mode="before")
    @classmethod
    def _store_names(cls, value: Any) -> tuple[str, ...]:
        if (not isinstance(value, list) or not value
                or not all(isinstance(name, str) and name.strip() == name and name for name in value)
                or len(set(value)) != len(value)):
            raise ValueError("replica_stores must list each eligible store once")
        return tuple(value)

    @field_validator("local_failure_domain")
    @classmethod
    def _local_domain(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("local_failure_domain must be a nonempty domain name")
        return value


def owner_policy(config: dict[str, object] | None) -> ProtectionPolicy | None:
    if config is None or POLICY_FIELD not in config:
        return None
    try:
        result = ProtectionPolicy.model_validate(config[POLICY_FIELD])
    except ValidationError as error:
        raise ProtectionPolicyError("the storage protection policy is invalid") from error
    if set(result.replica_stores) - originals.store_entries(config).keys():
        raise ProtectionPolicyError("a policy store has no media store declaration")
    return result


@dataclass(frozen=True, slots=True)
class VerifiedCopy:
    """A physical copy admitted by the original or replica proof boundary."""

    identity: tuple[str, ...]
    failure_domain: str | None


@dataclass(frozen=True, slots=True)
class Coverage:
    copies: int
    domains: int

    def meets(self, policy: ProtectionPolicy) -> bool:
        return self.copies >= policy.minimum_verified_copies and self.domains >= policy.minimum_failure_domains


def verified_coverage(copies: Iterable[VerifiedCopy]) -> Coverage:
    """Count an exact physical copy once even when many bindings refer to it."""
    identities: dict[tuple[str, ...], set[str]] = {}
    for copy in copies:
        domains = identities.setdefault(copy.identity, set())
        if copy.failure_domain is not None:
            domains.add(copy.failure_domain)
    # Conflicting domain labels for one exact identity prove no independent location.
    return Coverage(len(identities), len({next(iter(domains)) for domains in identities.values() if len(domains) == 1}))


def eviction_eligible(config: dict[str, object] | None) -> bool:
    """An opt-in policy needs separate owner authority for eviction."""
    try:
        policy = owner_policy(config)
    except ProtectionPolicyError:
        return False
    return policy is None or policy.eviction_authorized


def verified_remote_copies(
    references: Iterable[originals.StoredObject], proved: frozenset[originals.StoredObject],
    eligible: set[str], entries: dict[str, originals.StoreEntry],
) -> tuple[VerifiedCopy, ...]:
    return tuple(
        VerifiedCopy((ref.store, ref.object, ref.version), entries[ref.store].failure_domain)
        for ref in references if ref in proved and ref.store in eligible
    )


def permits_unlink(vault: Path, conn: Connection, object_key: str, *, config: dict[str, object]) -> bool:
    """Check each current filing's verified remote coverage before a local unlink."""
    from . import media_bindings, media_replicas

    try:
        current = originals.configuration(vault)
    except (OSError, ValueError):
        return False  # An unreadable configuration keeps the local original.
    if current != config:
        return False  # Owner policy or its store declarations changed during preflight.
    try:
        policy = owner_policy(current)
    except ProtectionPolicyError:
        return False
    if policy is None:
        return True
    if not policy.eviction_authorized:
        return False
    objects = media_bindings.policy_filings(vault, conn, (object_key,))
    filings = objects.get(object_key) if objects is not None else None
    if not filings:
        return False
    proved = media_replicas.verified_for_policy(conn, config, (ref for filing in filings for ref in filing.references))
    eligible = set(policy.replica_stores)
    entries = originals.store_entries(config)
    return all(verified_coverage(verified_remote_copies(filing.references, proved, eligible, entries)).meets(policy)
               for filing in filings)
