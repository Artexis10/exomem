"""Dependency-light operation and recovery-strategy registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class RecoveryStrategy:
    key: str
    component_kinds: frozenset[str]


_RECOVERY_STRATEGIES: Mapping[str, RecoveryStrategy] = MappingProxyType(
    {
        "composite_yaml": RecoveryStrategy(
            "composite_yaml",
            frozenset({"archive", "proposal", "proposal_guard", "yaml"}),
        ),
        "composite_dependents": RecoveryStrategy(
            "composite_dependents",
            frozenset({"archive", "dependent_grant", "yaml"}),
        ),
        "composite_sidecar": RecoveryStrategy(
            "composite_sidecar", frozenset({"grant", "purpose"})
        ),
        "composite_companion": RecoveryStrategy(
            "composite_companion", frozenset({"catalog", "companion", "proposal"})
        ),
        "compound_grant": RecoveryStrategy(
            "compound_grant", frozenset({"grant", "token"})
        ),
    }
)
RECOVERY_STRATEGY_KEYS = frozenset(_RECOVERY_STRATEGIES)

HANDLER_STRATEGY_KEYS = frozenset(
    {
        "backfill_companion_commit",
        "backfill_companion_preview",
        "commit",
        "declare",
        "grant_session",
        "grant_standing",
        "inspect",
        "proposal",
        "revoke_session",
        "revoke_standing",
        "session",
        "toggle_rules",
        "undo",
    }
)
_AUTHORIZATION_KEYS = frozenset({"inspect", "owner", "self_session", "token_session"})


@dataclass(frozen=True, slots=True)
class OperationVariant:
    mode: str
    handler_key: str
    authorization: str
    authorization_affecting: bool
    journal_operation: str | None = None
    receipt_event: str | None = None
    recovery_policy: str | None = None
    child_receipts: tuple[str, ...] = ()
    yaml_marker: bool = False
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class OperationSpec:
    read_only: bool
    authorization: str
    authorization_affecting: bool = False
    authorization_exemption: bool = False
    yaml_marker: bool = False
    receipt_event: str | None = None
    recovery_policy: str | None = None
    child_receipts: tuple[str, ...] = ()
    handler_key: str = ""
    destructive: bool = False
    variants: tuple[OperationVariant, ...] = ()


OPERATION_SPECS: Mapping[str, OperationSpec] = MappingProxyType(
    {
        "list": OperationSpec(True, "inspect", handler_key="inspect"),
        "explain": OperationSpec(True, "inspect", handler_key="inspect"),
        "simulate": OperationSpec(True, "inspect", handler_key="inspect"),
        "propose": OperationSpec(
            False,
            "owner",
            authorization_exemption=True,
            handler_key="proposal",
        ),
        "commit": OperationSpec(
            False,
            "owner",
            True,
            yaml_marker=True,
            receipt_event="governance_policy_commit",
            recovery_policy="composite_yaml",
            handler_key="commit",
        ),
        "grant": OperationSpec(
            False,
            "token_session",
            True,
            receipt_event="governance_session_grant",
            recovery_policy="compound_grant",
            child_receipts=(
                "governance_token_redemption",
                "governance_grant_creation",
            ),
            handler_key="grant_session",
            variants=(
                OperationVariant(
                    mode="standing",
                    handler_key="grant_standing",
                    authorization="owner",
                    authorization_affecting=True,
                    journal_operation="standing_grant",
                    receipt_event="governance_standing_grant",
                    recovery_policy="composite_yaml",
                    yaml_marker=True,
                    destructive=False,
                ),
            ),
        ),
        "revoke": OperationSpec(
            False,
            "self_session",
            True,
            receipt_event="governance_grant_revoke",
            recovery_policy="composite_sidecar",
            handler_key="revoke_session",
            destructive=True,
            variants=(
                OperationVariant(
                    mode="standing",
                    handler_key="revoke_standing",
                    authorization="owner",
                    authorization_affecting=True,
                    journal_operation="standing_revoke",
                    receipt_event="governance_standing_revoke",
                    recovery_policy="composite_yaml",
                    yaml_marker=True,
                    destructive=True,
                ),
            ),
        ),
        "suspend": OperationSpec(
            False,
            "owner",
            True,
            yaml_marker=True,
            receipt_event="governance_rule_suspend",
            recovery_policy="composite_yaml",
            handler_key="toggle_rules",
        ),
        "resume": OperationSpec(
            False,
            "owner",
            True,
            yaml_marker=True,
            receipt_event="governance_rule_resume",
            recovery_policy="composite_yaml",
            handler_key="toggle_rules",
        ),
        "undo": OperationSpec(
            False,
            "owner",
            True,
            yaml_marker=True,
            receipt_event="governance_policy_undo",
            recovery_policy="composite_dependents",
            handler_key="undo",
            destructive=True,
        ),
        "declare": OperationSpec(
            False,
            "self_session",
            True,
            receipt_event="governance_purpose_declare",
            recovery_policy="composite_sidecar",
            handler_key="declare",
        ),
        "backfill_companion": OperationSpec(
            False,
            "owner",
            authorization_exemption=True,
            handler_key="backfill_companion_preview",
            variants=(
                OperationVariant(
                    mode="commit",
                    handler_key="backfill_companion_commit",
                    authorization="owner",
                    authorization_affecting=True,
                    journal_operation="commit_backfill_companion",
                    receipt_event="governance_companion_backfill",
                    recovery_policy="composite_companion",
                ),
            ),
        ),
        "session": OperationSpec(
            False,
            "inspect",
            authorization_exemption=True,
            handler_key="session",
        ),
    }
)

_CONSUMED_SPEC_FIELDS = frozenset(
    {
        "read_only",
        "authorization",
        "authorization_affecting",
        "authorization_exemption",
        "yaml_marker",
        "receipt_event",
        "recovery_policy",
        "child_receipts",
        "handler_key",
        "destructive",
        "variants",
    }
)
_CONSUMED_VARIANT_FIELDS = frozenset(
    {
        "mode",
        "handler_key",
        "authorization",
        "authorization_affecting",
        "journal_operation",
        "receipt_event",
        "recovery_policy",
        "child_receipts",
        "yaml_marker",
        "destructive",
    }
)


def recovery_strategy(key: str | None) -> RecoveryStrategy:
    try:
        return _RECOVERY_STRATEGIES[str(key)]
    except KeyError as exc:
        raise LookupError(key) from exc


def _assert_strategy_coverage(
    owner: str,
    *,
    authorization_affecting: bool,
    handler_key: str,
    receipt_event: str | None,
    recovery_policy: str | None,
) -> None:
    if authorization_affecting and (not receipt_event or not recovery_policy):
        raise RuntimeError(f"governance operation lacks receipt coverage: {owner}")
    if handler_key not in HANDLER_STRATEGY_KEYS:
        raise RuntimeError(f"invalid governance handler strategy: {owner}")
    if authorization_affecting:
        try:
            recovery_strategy(recovery_policy)
        except LookupError as exc:
            raise RuntimeError(
                f"invalid governance recovery strategy: {owner}"
            ) from exc


def assert_operation_coverage(
    registry: Mapping[str, OperationSpec] = OPERATION_SPECS,
) -> None:
    if frozenset(OperationSpec.__dataclass_fields__) != _CONSUMED_SPEC_FIELDS:
        raise RuntimeError("unconsumed operation metadata")
    if frozenset(OperationVariant.__dataclass_fields__) != _CONSUMED_VARIANT_FIELDS:
        raise RuntimeError("unconsumed operation variant metadata")
    journal_operations: set[str] = set()
    for name, spec in registry.items():
        _assert_strategy_coverage(
            name,
            authorization_affecting=spec.authorization_affecting,
            handler_key=spec.handler_key,
            receipt_event=spec.receipt_event,
            recovery_policy=spec.recovery_policy,
        )
        if spec.authorization not in _AUTHORIZATION_KEYS:
            raise RuntimeError(f"invalid governance authorization strategy: {name}")
        if not spec.authorization_affecting and not spec.read_only:
            if not spec.authorization_exemption:
                raise RuntimeError(f"governance write lacks authorization exemption: {name}")
        if spec.authorization_affecting:
            journal_operations.add(name)
        modes: set[str] = set()
        for variant in spec.variants:
            owner = f"{name}:{variant.mode}"
            _assert_strategy_coverage(
                owner,
                authorization_affecting=variant.authorization_affecting,
                handler_key=variant.handler_key,
                receipt_event=variant.receipt_event,
                recovery_policy=variant.recovery_policy,
            )
            if variant.authorization not in _AUTHORIZATION_KEYS:
                raise RuntimeError(f"invalid governance authorization strategy: {owner}")
            if variant.handler_key != f"{name}_{variant.mode}":
                raise RuntimeError(f"variant handler strategy drift: {name}")
            if variant.mode in modes or variant.journal_operation in journal_operations:
                raise RuntimeError(f"duplicate governance operation variant: {name}")
            if variant.journal_operation != f"{variant.mode}_{name}":
                raise RuntimeError(f"variant journal operation drift: {name}")
            if variant.mode == "standing" and not variant.yaml_marker:
                raise RuntimeError(f"standing variant metadata drift: {name}")
            modes.add(variant.mode)
            if variant.journal_operation is not None:
                journal_operations.add(variant.journal_operation)


def is_read_only(operation: str) -> bool:
    try:
        return OPERATION_SPECS[operation].read_only
    except KeyError as exc:
        raise LookupError(operation) from exc


def operation_variant(operation: str, mode: str | None = None) -> OperationVariant:
    try:
        spec = OPERATION_SPECS[operation]
    except KeyError as exc:
        raise LookupError(operation) from exc
    if mode is not None:
        for variant in spec.variants:
            if variant.mode == mode:
                return variant
        raise LookupError(f"{operation}:{mode}")
    return OperationVariant(
        mode="default",
        handler_key=spec.handler_key,
        authorization=spec.authorization,
        authorization_affecting=spec.authorization_affecting,
        journal_operation=operation if spec.authorization_affecting else None,
        receipt_event=spec.receipt_event,
        recovery_policy=spec.recovery_policy,
        child_receipts=spec.child_receipts,
        yaml_marker=spec.yaml_marker,
        destructive=spec.destructive,
    )


def select_operation(operation: str, mode: object = None) -> OperationVariant:
    spec = OPERATION_SPECS.get(operation)
    if spec is None:
        raise LookupError(operation)
    if isinstance(mode, str):
        for variant in spec.variants:
            if variant.mode == mode:
                return variant
    return operation_variant(operation)


def is_destructive(operation: str, mode: object = None) -> bool:
    return select_operation(operation, mode).destructive


def journal_variant(journal_operation: str) -> OperationVariant:
    for name, spec in OPERATION_SPECS.items():
        if name == journal_operation and spec.authorization_affecting:
            return operation_variant(name)
        for variant in spec.variants:
            if variant.journal_operation == journal_operation:
                return variant
    raise LookupError(journal_operation)


assert_operation_coverage()
