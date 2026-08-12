"""Immutable names and boundaries for structured-collection product profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class CollectionProfile:
    """The product-owned names the shared collection mechanics must not infer."""

    name: str
    placement_layer: str
    item_id_property: str
    item_type: str
    reference_namespace: str
    manifest_audit_property: str
    item_audit_marker: str
    activity_prefix: str


RECORDS_PROFILE = CollectionProfile(
    name="records",
    placement_layer="Records",
    item_id_property="record_id",
    item_type="record",
    reference_namespace="record",
    manifest_audit_property="record_audit",
    item_audit_marker="exomem-record-audit",
    activity_prefix="Records audit-v1 ",
)
PLANNING_PROFILE = CollectionProfile(
    name="planning",
    placement_layer="Planning",
    item_id_property="plan_id",
    item_type="plan",
    reference_namespace="plan",
    manifest_audit_property="plan_audit",
    item_audit_marker="exomem-plan-audit",
    activity_prefix="Planning audit-v1 ",
)
PROFILES: Mapping[str, CollectionProfile] = MappingProxyType(
    {RECORDS_PROFILE.name: RECORDS_PROFILE, PLANNING_PROFILE.name: PLANNING_PROFILE}
)


def profile_for(name: str) -> CollectionProfile:
    """Return the registered immutable contract for one explicit profile."""
    return PROFILES[name]
