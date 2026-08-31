"""Closed executors for owner-protected consolidation verification contracts."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import NoReturn

from .. import capabilities, cli_ops, commands, schema, writer_lease
from . import (
    authorization_custody,
    authorization_request,
    authorization_session_lifecycle,
    consolidation_authority,
    consolidation_plan_store,
    consolidation_policy,
    consolidation_verification,
    consolidation_verification_manifest,
    principal,
    store,
)

VERIFICATION_WIRE_RESULT_SCHEMA = "exomem.consolidation-verification-wire-result/v1"

_WIRE_DOMAIN = VERIFICATION_WIRE_RESULT_SCHEMA.encode("ascii")
_MAX_WIRE_BYTES = 64 * 1024 * 1024

__all__ = [
    "ConsolidationVerificationRegistryUnavailable",
    "VERIFICATION_WIRE_RESULT_SCHEMA",
    "render_rest_verification_wire",
    "run_probe",
    "verification_wire_result_digest",
]


class ConsolidationVerificationRegistryUnavailable(RuntimeError):
    """Content-free refusal for an unavailable executor or surface result."""

    code = "CONSOLIDATION_VERIFICATION_REGISTRY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


def _fail() -> NoReturn:
    raise ConsolidationVerificationRegistryUnavailable from None


def verification_wire_result_digest(surface: str, wire: bytes) -> str:
    """Digest one exact bounded adapter representation with its surface identity."""

    if surface not in {"rest"} or type(wire) is not bytes or len(wire) > _MAX_WIRE_BYTES:
        _fail()
    domain = _WIRE_DOMAIN + b"\0" + surface.encode("ascii")
    framed = len(domain).to_bytes(4, "big") + domain + len(wire).to_bytes(8, "big") + wire
    return hashlib.sha256(framed).hexdigest()


def render_rest_verification_wire(
    *,
    success: bool,
    data: object = None,
    error: Mapping[str, object] | None = None,
) -> bytes:
    """Render the same REST response object as the public facade, including headers."""

    from ..server_rest import RestJSONResponse

    if type(success) is not bool or (success and error is not None):
        _fail()
    error_value = dict(error) if error is not None else None
    envelope = cli_ops.envelope(success, data=data, error=error_value)
    status = 200 if success else cli_ops.http_status_for(str((error_value or {}).get("code") or ""))
    response = RestJSONResponse(envelope, status_code=status)
    status_line = f"HTTP {response.status_code}\r\n".encode("ascii")
    headers = b"".join(name + b": " + value + b"\r\n" for name, value in response.raw_headers)
    wire = status_line + headers + b"\r\n" + response.body
    if len(wire) > _MAX_WIRE_BYTES:
        _fail()
    return wire


def _require_context(
    probe: consolidation_verification.VerificationProbe,
    context: consolidation_verification.VerificationProbeContext,
) -> consolidation_verification_manifest.VerificationContract:
    if (
        type(probe) is not consolidation_verification.VerificationProbe
        or type(context) is not consolidation_verification.VerificationProbeContext
        or type(context.contract) is not consolidation_verification_manifest.VerificationContract
    ):
        _fail()
    contract = context.contract
    if (
        contract.probe_id != probe.probe_id
        or contract.probe_kind != probe.probe_kind
        or contract.executor_id != probe.executor_id
        or contract.contract_digest != probe.contract_digest
        or contract.expected_result_digest != probe.expected_result_digest
    ):
        _fail()
    try:
        consolidation_authority.require_authority(
            context.authority,
            vault_binding_digest=context.vault_binding_digest,
            run_id=context.run_id,
            operation_id=context.operation_id,
            journal_digest=context.journal_digest,
            phase="verifying",
            action="verify",
        )
    except consolidation_authority.ConsolidationAuthorityUnavailable:
        _fail()
    return contract


def _revalidate_session(
    vault_root,
    who: principal.RequestPrincipal,
    *,
    now: int,
) -> principal.RequestPrincipal:
    context = who.verified_authorization_session
    if not isinstance(context, authorization_session_lifecycle.AuthorizationSessionContext):
        _fail()
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        connection = store.open_authorization_session_connection(vault_root)
        current = authorization_session_lifecycle.status_verified_session(
            connection,
            custody=custody,
            context=context,
            now=now,
        )
        return who.with_verified_authorization_session(
            current,
            issuer_family=current.issuer_family,
        )
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
        TypeError,
        ValueError,
    ):
        _fail()
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _fresh_verification_time() -> tuple[int, str]:
    """Return one trusted live instant for session and attestation freshness."""

    current = datetime.now(UTC)
    current = current.replace(microsecond=(current.microsecond // 1000) * 1000)
    return (
        int(current.timestamp()),
        current.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )


def _delegated_principal(
    contract: consolidation_verification_manifest.VerificationContract,
    context: consolidation_verification.VerificationProbeContext,
) -> principal.RequestPrincipal:
    matches = tuple(
        item
        for item in context.principals
        if type(item) is principal.RequestPrincipal
        and item.resolved
        and item.audience_id == contract.principal_id
        and item.surface == contract.surface
    )
    if len(matches) != 1:
        _fail()
    now, verified_at = _fresh_verification_time()
    checked = _revalidate_session(
        context.vault_root,
        matches[0],
        now=now,
    )
    try:
        bundle = consolidation_plan_store.ConsolidationPlanStore(
            context.vault_root
        ).load_policy_bundle(
            context.run_id,
            plan_kind="cutover",
            plan_digest=context.plan_digest,
        )
    except consolidation_plan_store.ConsolidationPlanStoreUnavailable:
        _fail()
    attestations = tuple(
        item
        for item in bundle.attestations
        if item.fingerprint == contract.principal_attestation_fingerprint
    )
    if len(attestations) != 1:
        _fail()
    try:
        consolidation_policy.verify_destination_principal_attestation(
            attestations[0],
            principal=checked,
            destination_vault_id=bundle.destination_vault_id,
            required_purpose=contract.purpose,
            expected_nonce=bundle.nonce,
            verified_at=verified_at,
        )
    except consolidation_policy.DestinationPolicyUnavailable:
        _fail()
    return checked.with_purpose(contract.purpose)


def _request_principal(
    contract: consolidation_verification_manifest.VerificationContract,
    context: consolidation_verification.VerificationProbeContext,
) -> principal.RequestPrincipal:
    if contract.principal_kind == "owner":
        if contract.surface != "rest" or contract.principal_id != principal.OWNER_AUDIENCE:
            _fail()
        return principal.resolve_rest_principal(None).with_purpose(contract.purpose)
    if contract.principal_kind != "delegated":
        _fail()
    return _delegated_principal(contract, context)


def _rest_command(
    contract: consolidation_verification_manifest.VerificationContract,
):
    expose_tier2 = not os.environ.get("EXOMEM_DISABLE_TIER2")
    available = {
        command.name: command
        for command in commands.product_commands_for("rest", expose_tier2=expose_tier2)
    }
    command = available.get(contract.command_name)
    if command is None:
        _fail()
    return command


def _execute_rest(
    probe: consolidation_verification.VerificationProbe,
    context: consolidation_verification.VerificationProbeContext,
) -> consolidation_verification.VerificationProbeTerminal:
    contract = _require_context(probe, context)
    if contract.surface != "rest":
        _fail()
    command = _rest_command(contract)
    raw = consolidation_verification_manifest.contract_arguments(contract)
    if any(parameter.name == "purpose" for parameter in command.params):
        raw["purpose"] = contract.purpose
    try:
        kwargs = cli_ops.coerce(
            command.params,
            raw,
            guarded_fields=command.guarded_fields,
            tool=command.name,
        )
        if not commands.invocation_is_read_only(command, kwargs):
            _fail()
        who = _request_principal(contract, context)
        expose_tier2 = not os.environ.get("EXOMEM_DISABLE_TIER2")
        descriptor = capabilities.ActiveSurfaceDescriptor(
            surface="rest",
            profile="openapi",
            tier2_enabled=expose_tier2,
            product_commands=tuple(
                item.name
                for item in commands.product_commands_for(
                    "rest",
                    expose_tier2=expose_tier2,
                )
            ),
        )
        injected = (
            (context.vault_root, schema.load_source_schema(context.vault_root))
            if command.needs_schema
            else (context.vault_root,)
        )
        with writer_lease._verification_component_failure_scope() as component_failures:  # noqa: SLF001
            leaf_error: BaseException | None = None
            try:
                with capabilities.active_surface(descriptor), principal.request_scope(who):
                    try:
                        result = writer_lease.invoke_command(command, *injected, **kwargs)
                    except (
                        authorization_request.AuthorizationContextUnavailable,
                        authorization_request.AuthorizationRouteUnclassified,
                        cli_ops.OpError,
                        ValueError,
                        TypeError,
                    ) as error:
                        leaf_error = error
                        raise
            except (
                authorization_request.AuthorizationContextUnavailable,
                authorization_request.AuthorizationRouteUnclassified,
                cli_ops.OpError,
                ValueError,
                TypeError,
            ) as error:
                if error is not leaf_error or component_failures:
                    _fail()
                public = cli_ops.error_dict(error)
                result = None
            else:
                if component_failures:
                    _fail()
                public = None
    except ConsolidationVerificationRegistryUnavailable:
        raise
    except Exception:  # noqa: BLE001 - unknown failures remain content-free
        _fail()
    try:
        wire = (
            render_rest_verification_wire(success=True, data=result)
            if public is None
            else render_rest_verification_wire(success=False, error=public)
        )
    except ConsolidationVerificationRegistryUnavailable:
        raise
    except Exception:  # noqa: BLE001 - adapter failures remain content-free
        _fail()
    return consolidation_verification.VerificationProbeTerminal(
        schema=consolidation_verification.VERIFICATION_PROBE_TERMINAL_SCHEMA,
        probe_id=probe.probe_id,
        probe_digest=probe.probe_digest,
        result_digest=verification_wire_result_digest(contract.surface, wire),
        outcome="passed",
    )


_EXECUTORS: Mapping[
    str,
    Callable[
        [
            consolidation_verification.VerificationProbe,
            consolidation_verification.VerificationProbeContext,
        ],
        consolidation_verification.VerificationProbeTerminal,
    ],
] = MappingProxyType({consolidation_verification.CANONICAL_SURFACE_EXECUTOR_ID: _execute_rest})


def run_probe(
    probe: consolidation_verification.VerificationProbe,
    context: consolidation_verification.VerificationProbeContext,
) -> consolidation_verification.VerificationProbeTerminal:
    """Dispatch only by the fixed executor id; probe ids never select behavior."""

    if type(probe) is not consolidation_verification.VerificationProbe:
        _fail()
    executor = _EXECUTORS.get(probe.executor_id)
    if executor is None:
        _fail()
    return executor(probe, context)
