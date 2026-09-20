"""Content-free proof of a committed hosted mutation publication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from .governance import authorization_custody as custody
from .governance import hosted_mutation_journal as journal
from .hosted_activation_ack_protocol import PROTOCOL, ProtocolError, decode_message, encode_message
from .hosted_activation_delivery import (
    VerifiedDeliveryBundle,
    parse_delivery_bundle,
    require_serving_delivery,
)


class ActivationProofUnavailable(RuntimeError):
    code = "ACK_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("hosted activation proof is unavailable")


class ActivationProofEndpoint:
    """Reserved one-second proof execution, independent of content workers.

    Authenticate with the existing private route machinery before invoking
    this handler. A cancelled HTTP request cannot release an occupied worker.
    """

    def __init__(self, prove: Callable[[Mapping[str, object]], dict[str, object]]) -> None:
        self._prove = prove
        self._slot = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="activation-proof")

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def handle(self, request):
        from starlette.responses import Response

        def failure(code: str, status: int, correlation: str | None):
            return Response(
                encode_message(
                    {
                        "protocol": PROTOCOL,
                        "request_id": correlation,
                        "code": code,
                        "retry_after_ms": 0 if status == 400 else 250,
                    },
                    "errorResponse",
                ),
                status_code=status,
                media_type="application/json",
            )

        if not self._slot.acquire(blocking=False):
            return failure("ACK_CAPACITY_EXCEEDED", 503, "0" * 64)
        submitted = False
        correlation = "0" * 64
        try:
            async with asyncio.timeout(1.0):
                if (
                    request.url.query
                    or sum(len(k) + len(v) + 4 for k, v in request.scope["headers"]) > 8192
                ):
                    raise ProtocolError
                chunks = []
                count = 0
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > 8192:
                        raise ProtocolError
                    chunks.append(chunk)
                body = decode_message(b"".join(chunks), "proofRequest")
                correlation = body["challenge_id"]
                future = self._executor.submit(self._prove, body)
                submitted = True
                future.add_done_callback(lambda _: self._slot.release())
                result = await asyncio.shield(asyncio.wrap_future(future))
                return Response(
                    encode_message(result, "proofResponse"), media_type="application/json"
                )
        except ProtocolError:
            return failure("MALFORMED_REQUEST", 400, None)
        except TimeoutError:
            return failure("ACK_DEADLINE_EXCEEDED", 503, correlation)
        except (OSError, RuntimeError, ValueError, TypeError):
            return failure("ACK_UNAVAILABLE", 503, correlation)
        finally:
            if not submitted:
                self._slot.release()


def read_installed_bundle(vault_root: Path, *, now: int) -> VerifiedDeliveryBundle:
    """Read custody without the writer, store manager or lifecycle fences."""
    external = custody.load_external_custody(vault_root)
    membership_path = custody._configured_external_path(custody.MEMBERSHIP_FILE_ENV, vault_root)
    membership = custody._load_file(membership_path)
    return parse_delivery_bundle(
        {
            "keyring.json": external.keyring,
            "control.json": external.control,
            "serving-membership.json": membership.data,
        },
        now=now,
    )


def selector_for_committed_target(vault_root: Path, *, predecessor, successor) -> dict[str, object]:
    """Select an immutable event; the challenged reader supplies its proof."""
    from .governance.store import sidecar_path

    path = sidecar_path(vault_root)
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path.absolute()))}?mode=ro", uri=True, timeout=0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=0")
            rows = connection.execute(
                "SELECT event_id FROM governance_tuple_publications WHERE status='committed' "
                "AND activation_epoch=? AND predecessor_activation_state_digest=? "
                "AND target_activation_state_digest=? AND length(event_id) BETWEEN 1 AND 512 LIMIT 2",
                (
                    successor.activation_epoch,
                    predecessor.activation_state_digest,
                    successor.activation_state_digest,
                ),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) != 1:
            raise ActivationProofUnavailable
        return {
            "publication_event_id": rows[0][0],
            "predecessor": {
                name: getattr(predecessor, name)
                for name in ("activation_store_id", "activation_epoch", "activation_state_digest")
            },
            "successor": {
                name: getattr(successor, name)
                for name in ("activation_store_id", "activation_epoch", "activation_state_digest")
            },
        }
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise ActivationProofUnavailable from None


def prove_publication(
    request: Mapping[str, object],
    *,
    installed: VerifiedDeliveryBundle,
    governance_db_path: Path,
    idempotency_db_path: Path,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_replica_id: str,
    now: int,
) -> dict[str, object]:
    """Sign only an exact publication proven by authenticated private evidence.

    The challenge's source revision need not match installed custody: a lost
    delivery can leave the worker ahead of the pod. Its authenticated issuer
    verifies that revision again before CAS; this proof binds it byte exactly.
    """
    try:
        encode_message(request, "proofRequest")
        # Re-authenticate at the current time; historical delivery snapshots
        # and caller-constructed dataclasses cannot grant proof authority.
        installed = parse_delivery_bundle(installed.files, now=now)
        require_serving_delivery(installed)
        control = installed.control
        replica = installed.membership.replicas[0]
        if (
            request["issued_at"] > now + 1
            or request["expires_at"] <= now
            or not 0 < request["expires_at"] - request["issued_at"] <= 3
            or request["expires_at"] > control.expires_at
            or control.cell_id != expected_cell_id
            or control.logical_vault_id != expected_logical_vault_id
            or not control.governance_enrolled
            or replica.replica_id != expected_replica_id
            or replica.schema_version != 4
            or replica.state != "SERVING"
            or replica.issuance_stopped
            or replica.no_in_flight
            or any(
                request[name] != getattr(control, name)
                for name in (
                    "cell_id",
                    "logical_vault_id",
                    "registry_attachment_id",
                    "attachment_epoch",
                )
            )
        ):
            raise ActivationProofUnavailable
        evidence = journal.read_committed_publication_evidence_for_selector(
            governance_db_path=governance_db_path,
            idempotency_db_path=idempotency_db_path,
            selector=request["publication"],
        )
        if evidence is None or any(
            getattr(evidence, name) != getattr(control, name)
            for name in (
                "cell_id",
                "logical_vault_id",
                "registry_attachment_id",
                "attachment_epoch",
                "activation_store_id",
            )
        ):
            raise ActivationProofUnavailable
        proven_selector = {
            "publication_event_id": evidence.publication.event_id,
            "predecessor": {
                "activation_store_id": evidence.activation_store_id,
                "activation_epoch": evidence.publication.activation_epoch - 1,
                "activation_state_digest": evidence.publication.predecessor_activation_state_digest,
            },
            "successor": {
                "activation_store_id": evidence.activation_store_id,
                "activation_epoch": evidence.publication.activation_epoch,
                "activation_state_digest": evidence.publication.target_activation_state_digest,
            },
        }
        if request["publication"] != proven_selector:
            raise ActivationProofUnavailable
        key = next(
            item
            for item in installed.keyring.accepted_keys
            if item.key_id == installed.keyring.active_key_id
        )
        if key.not_before > min(now, request["issued_at"]) or key.not_after < max(
            now, request["expires_at"]
        ):
            raise ActivationProofUnavailable
        response = {
            **request,
            "publication_evidence": {
                "component_kind": evidence.component_kind,
                "component_sha256": evidence.component_sha256,
            },
            "signing_key_id": key.key_id,
            "mac": "0" * 64,
        }
        encode_message(response, "proofResponse")
        body = dict(response)
        body.pop("mac")
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        response["mac"] = hmac.new(
            key.key, b"exomem.hosted-activation-proof/v1\x00" + canonical, hashlib.sha256
        ).hexdigest()
        return response
    except ActivationProofUnavailable:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
        ProtocolError,
    ):
        raise ActivationProofUnavailable from None
