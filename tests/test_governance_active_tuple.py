from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from exomem import (
    create_file as create_file_module,
)
from exomem import (
    find_corpus,
    reserved_paths,
    semantic_writes,
    writer_lease,
)
from exomem import (
    vault as vault_module,
)
from exomem.governance import (
    authorization_custody,
    catalog_publication,
    membership,
    policy,
    projection_store,
    projections,
    receipts,
    schema_v4,
    store,
)
from exomem.governance.principal import owner_principal

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
FIRST_GENERATION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
SECOND_GENERATION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
LOSING_GENERATION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAZ"
LOGICAL_VAULT_ID = "vault-active-tuple"
ACTIVATION_STORE_ID = "activation-active-tuple"
KEYRING_ID = "keyring-active-tuple"
CELL_ID = "cell-active-tuple"
KEY_ID = "key-active-tuple"
SIGNING_KEY = b"k" * 32


def _documents(*, ceiling: int) -> tuple[tuple[str, bytes], ...]:
    return (
        (
            "rules/external.yaml",
            (
                "governance_version: 1\n"
                f"id: {RULE_ID}\n"
                "scope_ids:\n"
                f"  - {SCOPE_ID}\n"
                "audience: external\n"
                f"ceiling: {ceiling}\n"
            ).encode(),
        ),
        (
            "scopes/private.yaml",
            (
                "governance_version: 1\n"
                f"id: {SCOPE_ID}\n"
                "name: private\n"
                "paths:\n"
                "  - Notes/**\n"
                "default_deny: true\n"
            ).encode(),
        ),
    )


def _compiled(documents: tuple[tuple[str, bytes], ...]) -> policy.Policy:
    compiled = policy.compile_documents(dict(documents))
    assert not compiled.empty and not compiled.blocked
    return compiled


def _policy_seed(
    *,
    generation_id: str,
    documents: tuple[tuple[str, bytes], ...],
    predecessor_generation_id: str | None,
    event_suffix: str,
    now: int,
) -> schema_v4.PolicyGenerationSeed:
    compiled = _compiled(documents)
    return schema_v4.PolicyGenerationSeed(
        generation_id=generation_id,
        source_documents=documents,
        source_fingerprint=compiled.fingerprint,
        conflict_digest="0" * 64,
        compiled_policy=policy.canonical_compiled_bytes(compiled),
        policy_fingerprint=compiled.fingerprint,
        compiler_schema_version=1,
        projector_schema_version=1,
        predecessor_generation_id=predecessor_generation_id,
        authoring_event_id=f"authoring-{event_suffix}",
        receipt_event_id=f"receipt-{event_suffix}",
        created_at=now,
    )


def _migration_seed(*, now: int) -> schema_v4.MigrationSeed:
    return schema_v4.MigrationSeed(
        activation_store_id=ACTIVATION_STORE_ID,
        logical_vault_id=LOGICAL_VAULT_ID,
        activation_epoch=1,
        policy=_policy_seed(
            generation_id=FIRST_GENERATION_ID,
            documents=_documents(ceiling=2),
            predecessor_generation_id=None,
            event_suffix="first",
            now=now,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="namespace-first",
            evidence=b'{"ready":true}',
            ready_at=now,
        ),
        migrated_at=now,
    )


def _write_workspace(vault: Path, documents: tuple[tuple[str, bytes], ...]) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    for relative, content in documents:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _protected_file(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if os.name == "nt":
        from exomem import mutation_lock

        mutation_lock._windows_apply_private_dacl(
            path, mutation_lock._windows_current_user_sid()
        )
    else:
        path.chmod(0o600)


def _framed(domain: bytes, fields: list[bytes]) -> bytes:
    result = bytearray(domain)
    result.append(0)
    for field in fields:
        result.extend(len(field).to_bytes(4, "big"))
        result.extend(field)
    return bytes(result)


def _configure_custody(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    activation_epoch: int | None,
    activation_state_digest: str | None,
    now: int,
    governance_enrolled: bool = True,
) -> None:
    keyring = {
        "version": 1,
        "keyring_id": KEYRING_ID,
        "cell_id": CELL_ID,
        "logical_vault_id": LOGICAL_VAULT_ID,
        "active_key_id": KEY_ID,
        "accepted_keys": [
            {
                "key_id": KEY_ID,
                "key": base64.urlsafe_b64encode(SIGNING_KEY)
                .rstrip(b"=")
                .decode("ascii"),
                "not_before": now - 60,
                "not_after": now + 7_200,
            }
        ],
    }
    control: dict[str, object] = {
        "version": 1,
        "keyring_id": KEYRING_ID,
        "cell_id": CELL_ID,
        "logical_vault_id": LOGICAL_VAULT_ID,
        "registry_attachment_id": "attachment-active-tuple",
        "attachment_epoch": 1,
        "governance_enrolled": governance_enrolled,
        "activation_store_id": ACTIVATION_STORE_ID if governance_enrolled else None,
        "activation_epoch": activation_epoch,
        "activation_state_digest": activation_state_digest,
        "serving_membership_epoch": 1,
        "serving_membership_digest": "a" * 64,
        "issued_at": now - 30,
        "expires_at": now + 3_600,
        "signing_key_id": KEY_ID,
    }
    fields = [
        str(control["version"]).encode(),
        str(control["keyring_id"]).encode(),
        str(control["cell_id"]).encode(),
        str(control["logical_vault_id"]).encode(),
        str(control["registry_attachment_id"]).encode(),
        str(control["attachment_epoch"]).encode(),
        b"true" if governance_enrolled else b"false",
        (
            b""
            if control["activation_store_id"] is None
            else str(control["activation_store_id"]).encode()
        ),
        (
            b""
            if control["activation_epoch"] is None
            else str(control["activation_epoch"]).encode()
        ),
        (
            b""
            if control["activation_state_digest"] is None
            else str(control["activation_state_digest"]).encode()
        ),
        str(control["serving_membership_epoch"]).encode(),
        str(control["serving_membership_digest"]).encode(),
        str(control["issued_at"]).encode(),
        str(control["expires_at"]).encode(),
        str(control["signing_key_id"]).encode(),
    ]
    control["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(
                SIGNING_KEY,
                _framed(b"exomem.authorization-session.control/v1", fields),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    keyring_path = root / "keyring.json"
    control_path = root / "control.json"
    _protected_file(keyring_path, json.dumps(keyring, separators=(",", ":")).encode())
    _protected_file(control_path, json.dumps(control, separators=(",", ":")).encode())
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(keyring_path))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(control_path))


def _migrate(vault: Path, *, now: int) -> schema_v4.MigrationResult:
    connection = store.open_connection(vault)
    try:
        result = schema_v4.migrate_v3_connection(connection, _migration_seed(now=now))
    finally:
        connection.close()
    return result


def _migrate_with_empty_projection_catalog(
    vault: Path,
    *,
    now: int,
) -> schema_v4.MigrationResult:
    documents = _documents(ceiling=2)
    compiled = _compiled(documents)
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )
    manifest = projection_store.stage_variant_store(vault, key=key, items=())
    connection = store.open_connection(vault)
    try:
        result = schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id=ACTIVATION_STORE_ID,
                logical_vault_id=LOGICAL_VAULT_ID,
                activation_epoch=1,
                policy=_policy_seed(
                    generation_id=FIRST_GENERATION_ID,
                    documents=documents,
                    predecessor_generation_id=None,
                    event_suffix="first",
                    now=now,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=1,
                    descriptor=projection_store.catalog_descriptor_bytes(key, ()),
                    artifact_count=0,
                    created_at=now,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id=key.namespace_id,
                    evidence=projection_store.projection_namespace_evidence_bytes(
                        manifest
                    ),
                    ready_at=now,
                ),
                migrated_at=now,
            ),
        )
    finally:
        connection.close()
    return result


def _projection_item(
    *,
    vault: Path,
    compiled: policy.Policy,
    path: str,
    source: str,
    catalog_generation: int,
) -> projection_store.ProjectionItemVariants:
    content = source.encode("utf-8")
    parsed = find_corpus.parse_page(
        vault / path,
        0.0,
        vault,
        content=content,
        resolved_relative=path,
    )
    assert parsed is not None
    content_hash = hashlib.sha256(content).hexdigest()
    scope_ids = tuple(
        sorted(membership.evaluate_snapshot(parsed, compiled, content_hash=content_hash))
    )
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=catalog_generation,
    )
    search_fields = {
        "body": parsed.body,
        "title": parsed.title,
    }
    for name, value in (
        ("status", parsed.frontmatter.get("status")),
        ("type", parsed.page_type),
        ("updated", parsed.updated),
    ):
        if value:
            search_fields[name] = str(value)
    variants = projections.enumerate_projection_variants(
        item_identity=path,
        content_hash=content_hash,
        scope_ids=scope_ids,
        policy=compiled,
        projector_schema_version=key.projector_schema_version,
        full_search_fields=search_fields,
    )
    return projection_store.ProjectionItemVariants(
        item_identity=path,
        content_hash=content_hash,
        scope_ids=scope_ids,
        variants=variants,
    )


def _migrate_with_projection_item(
    vault: Path,
    *,
    path: str,
    source: str,
    now: int,
) -> schema_v4.MigrationResult:
    documents = _documents(ceiling=2)
    compiled = _compiled(documents)
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )
    item = _projection_item(
        vault=vault,
        compiled=compiled,
        path=path,
        source=source,
        catalog_generation=1,
    )
    manifest = projection_store.stage_variant_store(vault, key=key, items=(item,))
    connection = store.open_connection(vault)
    try:
        return schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id=ACTIVATION_STORE_ID,
                logical_vault_id=LOGICAL_VAULT_ID,
                activation_epoch=1,
                policy=_policy_seed(
                    generation_id=FIRST_GENERATION_ID,
                    documents=documents,
                    predecessor_generation_id=None,
                    event_suffix="first",
                    now=now,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=1,
                    descriptor=projection_store.catalog_descriptor_bytes(key, (item,)),
                    artifact_count=1,
                    created_at=now,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id=key.namespace_id,
                    evidence=projection_store.projection_namespace_evidence_bytes(
                        manifest
                    ),
                    ready_at=now,
                ),
                migrated_at=now,
            ),
        )
    finally:
        connection.close()


def test_govern_memory_v4_proposal_persists_exact_authority_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    target_documents = {
        relative: content.decode("utf-8")
        for relative, content in _documents(ceiling=1)
    }

    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents=target_documents,
            target_ceiling=1,
            now=now + 1,
        )

    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        proposal_json, membership_manifest = connection.execute(
            "SELECT proposal_json, membership_manifest FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone()
    payload = json.loads(proposal_json)
    binding = payload["authority_binding"]
    target = binding["target"]
    reviewed = binding["reviewed_active_tuple"]
    snapshot = binding["authoring_snapshot"]
    target_policy = _compiled(_documents(ceiling=1))
    target_key = projections.ProjectionNamespaceKey(
        policy_fingerprint=target_policy.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )

    assert binding["schema"] == "exomem.governance-policy-proposal/v3"
    assert reviewed == {
        "activation_epoch": 1,
        "activation_state_digest": migration.activation_state_digest,
        "activation_store_id": ACTIVATION_STORE_ID,
        "catalog_generation": 1,
        "logical_vault_id": LOGICAL_VAULT_ID,
        "policy_fingerprint": _compiled(_documents(ceiling=2)).fingerprint,
        "policy_generation_id": FIRST_GENERATION_ID,
        "projection_namespace_id": projections.ProjectionNamespaceKey(
            policy_fingerprint=_compiled(_documents(ceiling=2)).fingerprint,
            projector_schema_version=1,
            catalog_generation=1,
        ).namespace_id,
        "projector_schema_version": 1,
    }
    assert snapshot["source_fingerprint"] == reviewed["policy_fingerprint"]
    assert snapshot["conflict_set_digest"] == hashlib.sha256(
        b"exomem.governance-conflict-set.v1\0"
    ).hexdigest()
    assert snapshot["guard_generation"]
    assert len(snapshot["documents"]) == 2
    assert len(snapshot["file_identities"]) == 2
    assert [item["path"] for item in snapshot["directory_identities"]] == [
        "rules",
        "scopes",
    ]
    assert target["policy_fingerprint"] == target_policy.fingerprint
    assert target["source_fingerprint"] == target_policy.fingerprint
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", target["generation_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", target["authoring_event_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", target["receipt_event_id"])
    assert target["compiled_policy"] == base64.b64encode(
        policy.canonical_compiled_bytes(target_policy)
    ).decode("ascii")
    assert target["projection_namespace"] == {
        "catalog_generation": 1,
        "evidence": base64.b64encode(
            projection_store.projection_namespace_evidence_bytes(
                projection_store.verify_variant_store(
                    vault,
                    key=target_key,
                    expected_rows_digest=target["projection_rows_digest"],
                )
            )
        ).decode("ascii"),
        "namespace_id": target_key.namespace_id,
        "projector_schema_version": 1,
        "ready_at": now + 1,
    }
    assert json.loads(membership_manifest) == binding["membership_manifest"]


def test_govern_memory_v4_proposal_refuses_unprepared_model_measurements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    load_evidence = projection_store.namespace_evidence_from_snapshot

    def model_backed_evidence(
        snapshot: schema_v4.ActivePolicySnapshot,
    ) -> projection_store.ProjectionNamespaceEvidence:
        evidence = load_evidence(snapshot)
        return projection_store.ProjectionNamespaceEvidence(
            manifest=evidence.manifest,
            required_measurement_roots=(object(),),
        )

    monkeypatch.setattr(
        projection_store,
        "namespace_evidence_from_snapshot",
        model_backed_evidence,
    )

    with reserved_paths._owner_authority_scope("govern_memory"):
        with pytest.raises(GovernanceError) as error:
            op_govern_memory(
                vault,
                operation="propose",
                principal=owner_principal(),
                intent="Lower the external ceiling",
                documents={
                    relative: content.decode("utf-8")
                    for relative, content in _documents(ceiling=1)
                },
                target_ceiling=1,
                now=now + 1,
            )
    assert error.value.code == "GOVERNANCE_PROJECTION_REBUILD_REQUIRED"
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_proposals"
        ).fetchone() == (0,)


def test_govern_memory_v4_commit_publishes_the_exact_reviewed_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    served = policy.load(vault)
    custody = authorization_custody.load_authorization_custody(vault, now=now + 2)

    assert committed == {
        "status": "committed",
        "event_id": committed["event_id"],
        "proposal_id": proposed["proposal_id"],
        "direction": "narrowing",
        "mirror_status": "complete",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", committed["event_id"])
    assert served.fingerprint == _compiled(_documents(ceiling=1)).fingerprint
    assert served.rules[0].ceiling == 1
    assert custody.control.activation_epoch == 2
    assert custody.control.activation_state_digest is not None
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == dict(_documents(ceiling=1))["rules/external.yaml"]

    mirror_records = [
        record
        for record in receipts.event_records(vault)
        if record.get("operation") == "governance_policy_workspace_mirror"
        or record.get("outcome") == "complete"
    ]
    assert [record["phase"] for record in mirror_records] == ["intent", "committed"]
    assert mirror_records[0]["parent_causation_id"] == committed["event_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", mirror_records[0]["prior"])
    assert re.fullmatch(r"[0-9a-f]{64}", mirror_records[0]["prepared"])
    assert re.fullmatch(r"[0-9a-f]{64}", mirror_records[0]["target"])

    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, reserved_event_id, spent_at FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent", None, now + 2)
        active = connection.execute(
            "SELECT policy_generation_id, policy_fingerprint, "
            "projector_schema_version, catalog_generation "
            "FROM active_governance_tuple WHERE singleton=1"
        ).fetchone()
        assert active[0] != FIRST_GENERATION_ID
        assert active[1:] == (_compiled(_documents(ceiling=1)).fingerprint, 1, 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_recovers_mirror_after_lost_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    target_documents = dict(_documents(ceiling=1))
    target_documents["scopes/private.yaml"] = target_documents[
        "scopes/private.yaml"
    ].replace(b"name: private\n", b"name: sensitive\n")
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in target_documents.items()
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(GovernanceCrash, match="v4_after_mirror_write:1"):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                crash_at="v4_after_mirror_write:1",
                now=now + 2,
            )

    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == target_documents["rules/external.yaml"]
    assert (
        vault / "Knowledge Base" / "_Governance" / "scopes" / "private.yaml"
    ).read_bytes() == dict(_documents(ceiling=2))["scopes/private.yaml"]
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent",)

    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["mirror_status"] == "complete"
    assert (
        vault / "Knowledge Base" / "_Governance" / "scopes" / "private.yaml"
    ).read_bytes() == target_documents["scopes/private.yaml"]
    records = receipts.event_records(vault)
    intents = [
        record
        for record in records
        if record.get("operation") == "governance_policy_workspace_mirror"
    ]
    terminals = [
        record
        for record in records
        if record.get("causation_id") == intents[0]["event_id"]
    ]
    assert len(intents) == 1
    assert [(record["phase"], record["outcome"]) for record in terminals] == [
        ("committed", "complete")
    ]
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_retries_transient_mirror_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import held_fs
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    original_acquire = held_fs.acquire
    armed = False
    mirror_acquires = 0

    def arm_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal armed
        if phase == "after_intent":
            armed = True

    def refuse_effect_acquire(root: Path):
        nonlocal mirror_acquires
        if armed:
            mirror_acquires += 1
            if mirror_acquires == 4:
                return held_fs.HeldResult(
                    error=held_fs.HeldFsError(
                        "CAPABILITY_UNAVAILABLE",
                        "test-only transient capability refusal",
                    )
                )
        return original_acquire(root)

    monkeypatch.setattr(tool, "_v4_workspace_mirror_barrier", arm_after_intent)
    monkeypatch.setattr(held_fs, "acquire", refuse_effect_acquire)
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(tool.GovernanceError) as refused:
            tool.op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                now=now + 2,
            )

    assert refused.value.code == "GOVERNANCE_BLOCKED"
    assert policy.load(vault).rules[0].ceiling == 1
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == dict(_documents(ceiling=2))["rules/external.yaml"]

    monkeypatch.setattr(held_fs, "acquire", original_acquire)
    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["mirror_status"] == "complete"
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == dict(_documents(ceiling=1))["rules/external.yaml"]


def test_govern_memory_v4_commit_replays_after_mirror_terminal_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(GovernanceCrash, match="v4_after_mirror_terminal"):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                crash_at="v4_after_mirror_terminal",
                now=now + 2,
            )
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["mirror_status"] == "complete"
    records = receipts.event_records(vault)
    intents = [
        record
        for record in records
        if record.get("operation") == "governance_policy_workspace_mirror"
    ]
    assert len(intents) == 1
    assert sum(
        record.get("causation_id") == intents[0]["event_id"] for record in records
    ) == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_preserves_observed_workspace_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    drift = dict(_documents(ceiling=3))["rules/external.yaml"]
    changed = False

    def mutate_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_intent" and not changed:
            changed = True
            (
                vault
                / "Knowledge Base"
                / "_Governance"
                / "rules"
                / "external.yaml"
            ).write_bytes(drift)

    monkeypatch.setattr(tool, "_v4_workspace_mirror_barrier", mutate_after_intent)
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["mirror_status"] == "diverged"
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == drift
    assert policy.load(vault).rules[0].ceiling == 1
    mirror_terminal = next(
        record
        for record in receipts.event_records(vault)
        if record.get("outcome") == "diverged"
    )
    assert mirror_terminal["phase"] == "committed"


def test_govern_memory_v4_commit_refuses_swapped_reviewed_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    changed = False
    rules = vault / "Knowledge Base" / "_Governance" / "rules"
    displaced = tmp_path / "reviewed-rules"

    def exchange_parent_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_intent" and not changed:
            changed = True
            rules.rename(displaced)
            rules.mkdir()
            (displaced / "external.yaml").rename(rules / "external.yaml")
            displaced.rmdir()

    monkeypatch.setattr(
        tool,
        "_v4_workspace_mirror_barrier",
        exchange_parent_after_intent,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["mirror_status"] == "diverged"
    assert (rules / "external.yaml").read_bytes() == dict(_documents(ceiling=2))[
        "rules/external.yaml"
    ]
    assert policy.load(vault).rules[0].ceiling == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink race fixture")
def test_govern_memory_v4_commit_refuses_symlinked_mirror_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    changed = False
    leaf = vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"

    def alias_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_intent" and not changed:
            changed = True
            leaf.unlink()
            leaf.symlink_to("../scopes/private.yaml")

    monkeypatch.setattr(tool, "_v4_workspace_mirror_barrier", alias_after_intent)
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["mirror_status"] == "diverged"
    assert leaf.is_symlink()
    assert policy.load(vault).blocked


def test_govern_memory_v4_commit_recovers_lost_registry_ack_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )

    def crash(point: str) -> None:
        if point == "policy-publication-after-commit-before-registry":
            raise GovernanceCrash(point)

    monkeypatch.setattr(schema_v4, "_crash_point", crash)
    with reserved_paths._owner_authority_scope("govern_memory"):
        with pytest.raises(GovernanceCrash):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                now=now + 2,
            )

    assert policy.load(vault).blocked
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)

    monkeypatch.setattr(schema_v4, "_crash_point", lambda _point: None)
    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 1_000,
        )

    assert recovered["status"] == "committed"
    assert recovered["proposal_id"] == proposed["proposal_id"]
    assert policy.load(vault).rules[0].ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, spent_at FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent", now + 1_000)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_recovers_after_registry_ack_before_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(GovernanceCrash, match="v4_after_registry_ack"):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                crash_at="v4_after_registry_ack",
                now=now + 2,
            )

    assert policy.load(vault).rules[0].ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("pending",)

    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["status"] == "committed"
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, spent_at FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent", now + 3)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_adopts_its_exact_concurrent_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )

    publish = schema_v4.publish_policy_generation

    def concurrent_winner(*args: object, **kwargs: object) -> object:
        publish(*args, **kwargs)  # type: ignore[arg-type]
        raise schema_v4.ActiveTupleStale("concurrent retry lost the CAS")

    monkeypatch.setattr(schema_v4, "publish_policy_generation", concurrent_winner)
    with reserved_paths._owner_authority_scope("govern_memory"):
        committed = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["status"] == "committed"
    assert policy.load(vault).rules[0].ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent",)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_refuses_reviewed_tuple_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        winner = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Close the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=0)
            },
            target_ceiling=0,
            now=now + 1,
        )
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=winner["proposal_id"],
            now=now + 2,
        )

    with reserved_paths._owner_authority_scope("govern_memory"):
        with pytest.raises(GovernanceError) as error:
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                now=now + 3,
            )
    assert error.value.code == "STALE_GOVERNANCE_POLICY"
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def _acknowledge(
    active: schema_v4.VerifiedActiveGovernanceState,
) -> schema_v4.ActivationRegistryAcknowledgement:
    return schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id=active.activation_store_id,
        activation_epoch=active.activation_epoch,
        activation_state_digest=active.activation_state_digest,
    )


def test_compiled_policy_authority_bytes_have_a_fixed_vector() -> None:
    compiled = _compiled(_documents(ceiling=2))

    assert (
        hashlib.sha256(policy.canonical_compiled_bytes(compiled)).hexdigest()
        == "b10ad7307c6c63f0cc732e5bc03462997e59be1fea03c19038945da3e2944ed2"
    )


def test_activation_state_digest_has_a_cross_runtime_fixed_vector() -> None:
    assert schema_v4.activation_state_digest(
        logical_vault_id=LOGICAL_VAULT_ID,
        activation_store_id=ACTIVATION_STORE_ID,
        activation_epoch=7,
        policy_generation_id=FIRST_GENERATION_ID,
        policy_fingerprint="1" * 64,
        policy_row_digest="2" * 64,
        projector_schema_version=3,
        catalog_generation=11,
        catalog_descriptor_digest="4" * 64,
        projection_namespace_identity="5" * 64,
    ) == "07a35c70829d9486f876aed26c650e3aeb3eaf064a676ba842e7dbc97ebb878b"


def test_bounded_pointer_exposes_sqlite_cas_before_registry_ack(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        predecessor = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        forbidden_columns = {
            "source_documents",
            "compiled_policy",
            "descriptor",
            "evidence",
        }

        def bounded_authorizer(
            action: int,
            _table: str | None,
            column: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and column in forbidden_columns:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(bounded_authorizer)
        assert schema_v4.load_active_tuple_pointer(connection) == predecessor
        connection.set_authorizer(None)

        def unavailable_registry(
            _active: schema_v4.VerifiedActiveGovernanceState,
        ) -> schema_v4.ActivationRegistryAcknowledgement:
            raise RuntimeError("registry acknowledgement unavailable")

        with pytest.raises(RuntimeError, match="registry acknowledgement unavailable"):
            schema_v4.publish_policy_generation(
                connection,
                expected=predecessor,
                policy=_policy_seed(
                    generation_id=SECOND_GENERATION_ID,
                    documents=_documents(ceiling=1),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="bounded-pointer",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-bounded-pointer",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=unavailable_registry,
            )

        successor = schema_v4.load_active_tuple_pointer(connection)
        assert successor.activation_epoch == predecessor.activation_epoch + 1
        assert successor != predecessor
    finally:
        connection.close()


def test_tuple_publication_schema_is_closed_and_append_only(tmp_path: Path) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        columns = tuple(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(governance_tuple_publications)"
            )
        )
        assert columns == (
            "event_id",
            "publication_kind",
            "predecessor_activation_state_digest",
            "target_activation_state_digest",
            "policy_generation_id",
            "policy_fingerprint",
            "projector_schema_version",
            "catalog_generation",
            "activation_epoch",
            "status",
            "activated_at",
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE governance_tuple_publications SET status='committed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM governance_tuple_publications")
    finally:
        connection.close()


def test_migration_refuses_noncanonical_compiled_seed_before_schema_write(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    connection = store.open_connection(vault)
    invalid = dataclasses.replace(
        _migration_seed(now=now),
        policy=dataclasses.replace(
            _migration_seed(now=now).policy,
            compiled_policy=b'{"schema":"caller-selected"}',
        ),
    )
    try:
        with pytest.raises(schema_v4.SchemaV4Error, match="source parity"):
            schema_v4.migrate_v3_connection(connection, invalid)

        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='compiled_policy_generations'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_v4_policy_load_uses_the_verified_immutable_generation_not_live_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    result = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=result.activation_state_digest,
        now=now,
    )

    initial = policy.load(vault)
    _write_workspace(vault, _documents(ceiling=0))
    pending = policy.load(vault)

    assert initial.fingerprint == _compiled(_documents(ceiling=2)).fingerprint
    assert pending == initial
    assert pending.rules[0].ceiling == 2


def test_v4_policy_load_blocks_registry_tuple_mismatch_and_workspace_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    result = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest="f" * 64,
        now=now,
    )

    assert policy.load(vault).blocked

    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=result.activation_state_digest,
        now=now,
    )
    for path in sorted(
        (vault / "Knowledge Base" / "_Governance").rglob("*"), reverse=True
    ):
        path.rmdir() if path.is_dir() else path.unlink()
    (vault / "Knowledge Base" / "_Governance").rmdir()

    assert policy.load(vault).blocked


def test_external_enrollment_proof_controls_open_and_missing_store_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=None,
        activation_state_digest=None,
        governance_enrolled=False,
        now=now,
    )

    assert policy.load(vault).empty

    _write_workspace(vault, _documents(ceiling=2))
    assert policy.load(vault).blocked

    for path in sorted(
        (vault / "Knowledge Base" / "_Governance").rglob("*"), reverse=True
    ):
        path.rmdir() if path.is_dir() else path.unlink()
    (vault / "Knowledge Base" / "_Governance").rmdir()
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest="f" * 64,
        governance_enrolled=True,
        now=now,
    )

    assert policy.load(vault).blocked


@pytest.mark.skipif(os.name == "nt", reason="requires an unprivileged symlink fixture")
def test_never_enrolled_refuses_broken_activation_or_workspace_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=None,
        activation_state_digest=None,
        governance_enrolled=False,
        now=now,
    )
    (kb / ".governance.sqlite").symlink_to(tmp_path / "missing-store")

    assert policy.load(vault).blocked

    (kb / ".governance.sqlite").unlink()
    (kb / "_Governance").symlink_to(tmp_path / "missing-workspace", target_is_directory=True)

    assert policy.load(vault).blocked


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_never_enrolled_refuses_orphaned_activation_store_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=None,
        activation_state_digest=None,
        governance_enrolled=False,
        now=now,
    )
    (kb / f".governance.sqlite{suffix}").write_bytes(b"orphaned activation state")

    assert policy.load(vault).blocked


def test_policy_publication_cas_has_one_winner_and_no_losing_rows(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    result = _migrate(vault, now=now)
    first = store.open_authorization_session_connection(vault)
    second = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            first,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=result.activation_state_digest,
        )
        winner = schema_v4.publish_policy_generation(
            first,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="winner",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-winner",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )

        with pytest.raises(schema_v4.ActiveTupleStale):
            schema_v4.publish_policy_generation(
                second,
                expected=expected,
                policy=_policy_seed(
                    generation_id=LOSING_GENERATION_ID,
                    documents=_documents(ceiling=0),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="loser",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-loser",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert winner.active.policy_generation_id == SECOND_GENERATION_ID
        assert winner.active.activation_epoch == 2
        rows = first.execute(
            "SELECT generation_id FROM compiled_policy_generations ORDER BY generation_id"
        ).fetchall()
        assert rows == [(FIRST_GENERATION_ID,), (SECOND_GENERATION_ID,)]
        assert first.execute(
            "SELECT COUNT(*) FROM governance_projection_namespaces "
            "WHERE namespace_id='namespace-loser'"
        ).fetchone() == (0,)
    finally:
        second.close()
        first.close()


def test_registry_ack_is_required_before_the_new_policy_can_serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    prior_custody = authorization_custody.load_authorization_custody(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_policy_generation(
            connection,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="second",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-second",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=lambda active: authorization_custody.acknowledge_activation_tuple(
                vault,
                expected_control=prior_custody.control,
                target=active,
                now=now + 1,
            ),
        )
    finally:
        connection.close()

    served = policy.load(vault)

    assert served.fingerprint == publication.active.policy_fingerprint
    assert served.rules[0].ceiling == 1


def test_crash_after_tuple_commit_stays_blocked_until_exact_registry_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    prior_custody = authorization_custody.load_authorization_custody(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )

        def crash_after_commit(_active: schema_v4.VerifiedActiveGovernanceState):
            raise RuntimeError("lost registry acknowledgement")

        with pytest.raises(RuntimeError, match="lost registry acknowledgement"):
            schema_v4.publish_policy_generation(
                connection,
                expected=expected,
                policy=_policy_seed(
                    generation_id=SECOND_GENERATION_ID,
                    documents=_documents(ceiling=1),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="ack-crash",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-ack-crash",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=crash_after_commit,
            )

        assert policy.load(vault).blocked
        recovered = schema_v4.recover_registry_acknowledgement(
            connection,
            expected=expected,
            acknowledge_registry=lambda active: authorization_custody.acknowledge_activation_tuple(
                vault,
                expected_control=prior_custody.control,
                target=active,
                now=now + 1,
            ),
        )
        served = policy.load(vault)

        assert recovered.active.activation_epoch == 2
        assert served.rules[0].ceiling == 1
    finally:
        connection.close()


def test_active_reader_pins_one_sqlite_snapshot_across_publication(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    reader = sqlite3.connect(store.sidecar_path(vault))
    writer = store.open_authorization_session_connection(vault)
    try:
        reader.execute("BEGIN")
        predecessor = schema_v4.load_active_policy(
            reader,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_policy_generation(
            writer,
            expected=predecessor.active,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="snapshot",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-snapshot",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )
        still_predecessor = schema_v4.load_active_policy(
            reader,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        reader.commit()
        successor = schema_v4.load_active_policy(
            reader,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=publication.active.activation_epoch,
            expected_activation_state_digest=publication.active.activation_state_digest,
        )

        assert predecessor.policy.rules[0].ceiling == 2
        assert still_predecessor == predecessor
        assert successor.policy.rules[0].ceiling == 1
    finally:
        writer.close()
        reader.close()


def test_policy_publication_crash_before_commit_restores_exact_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )

        def crash(point: str) -> None:
            if point == "policy-publication-before-commit":
                raise RuntimeError("injected tuple publication crash")

        monkeypatch.setattr(schema_v4, "_crash_point", crash)
        with pytest.raises(RuntimeError, match="injected tuple publication crash"):
            schema_v4.publish_policy_generation(
                connection,
                expected=expected,
                policy=_policy_seed(
                    generation_id=SECOND_GENERATION_ID,
                    documents=_documents(ceiling=1),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="crash",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-crash",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        ) == expected
        assert connection.execute(
            "SELECT COUNT(*) FROM compiled_policy_generations "
            "WHERE generation_id=?",
            (SECOND_GENERATION_ID,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE event_id='receipt-crash'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_policy_publication_refuses_noncanonical_compiled_target_before_write(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        invalid = dataclasses.replace(
            _policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="invalid",
                now=now + 1,
            ),
            compiled_policy=b'{"schema":"caller-selected"}',
        )

        with pytest.raises(schema_v4.SchemaV4Error, match="source parity"):
            schema_v4.publish_policy_generation(
                connection,
                expected=expected,
                policy=invalid,
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-invalid",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert not connection.in_transaction
        assert schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        ) == expected
    finally:
        connection.close()


def test_catalog_publication_keeps_the_reviewed_policy_and_advances_one_tuple(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_catalog_generation(
            connection,
            expected=expected,
            catalog=schema_v4.CatalogGenerationSeed(
                catalog_generation=2,
                descriptor=b'{"artifacts":["Notes/new.md"]}',
                artifact_count=1,
                created_at=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-catalog-2",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            receipt_event_id="receipt-catalog-2",
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )
        loaded = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=publication.active.activation_epoch,
            expected_activation_state_digest=publication.active.activation_state_digest,
        )

        assert publication.active.policy_generation_id == FIRST_GENERATION_ID
        assert publication.active.catalog_generation == 2
        assert publication.active.activation_epoch == 2
        assert loaded.policy.rules[0].ceiling == 2
        assert loaded.catalog_descriptor == b'{"artifacts":["Notes/new.md"]}'
    finally:
        connection.close()


def test_semantic_edit_publishes_the_next_v4_catalog_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )
    committed = semantic_writes.commit_existing(vault, preflight=preflight)

    assert committed.mutated is True
    assert target.read_text(encoding="utf-8") == after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    assert active.active.catalog_generation == 2
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=active.policy.fingerprint,
        projector_schema_version=active.active.projector_schema_version,
        catalog_generation=2,
    )
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=key,
        expected_rows_digest=(
            projection_store.namespace_evidence_from_snapshot(active).manifest.rows_digest
        ),
    )
    assert manifest.item_count == 1
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (relative, vault_module.content_hash(after))
    ]


def test_semantic_creation_publishes_the_next_v4_catalog_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/new.md"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    created = create_file_module.create_file(
        vault,
        path=relative,
        content="New governed note.\n",
        frontmatter={"title": "New", "status": "draft"},
        today=dt.date(2026, 8, 25),
    )

    assert created.creation is not None
    source = (vault / relative).read_text(encoding="utf-8")
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    _manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (relative, vault_module.content_hash(source))
    ]


def test_semantic_edit_refuses_catalog_drift_before_changing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    active_source = "---\ntitle: Private\nstatus: draft\n---\n\nactive\n"
    drifted_source = active_source.replace("active", "out-of-band")
    requested_source = drifted_source.replace("out-of-band", "requested")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(active_source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=active_source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    target.write_text(drifted_source, encoding="utf-8")
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=requested_source,
        operation="edit",
        expected_before_hash=vault_module.content_hash(drifted_source),
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(vault, preflight=preflight)

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert target.read_text(encoding="utf-8") == drifted_source
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_semantic_edit_recovers_lost_catalog_registry_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    real_crash_point = schema_v4._crash_point
    crashed = False

    def lose_first_ack(point: str) -> None:
        nonlocal crashed
        if point == "catalog-publication-after-commit-before-registry" and not crashed:
            crashed = True
            raise RuntimeError("lost catalogue acknowledgement")
        real_crash_point(point)

    monkeypatch.setattr(schema_v4, "_crash_point", lose_first_ack)
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )

    committed = semantic_writes.commit_existing(vault, preflight=preflight)

    assert crashed is True
    assert committed.mutated is True
    assert target.read_text(encoding="utf-8") == after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2


def test_semantic_edit_refuses_model_namespace_before_changing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    real_evidence = projection_store.namespace_evidence_from_snapshot

    def model_bound(snapshot):
        evidence = real_evidence(snapshot)
        return dataclasses.replace(
            evidence,
            required_measurement_roots=(object(),),
        )

    monkeypatch.setattr(
        projection_store,
        "namespace_evidence_from_snapshot",
        model_bound,
    )
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(vault, preflight=preflight)

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert target.read_text(encoding="utf-8") == before
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_abandoned_content_preparation_does_not_poison_next_catalog_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    first_candidate = before.replace("before", "abandoned")
    selected_candidate = before.replace("before", "selected")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    abandoned = catalog_publication.prepare_markdown_upsert(
        vault,
        path=relative,
        source=first_candidate,
        expected_before_hash=vault_module.content_hash(before),
        now=now + 1,
    )
    selected = catalog_publication.prepare_markdown_upsert(
        vault,
        path=relative,
        source=selected_candidate,
        expected_before_hash=vault_module.content_hash(before),
        now=now + 1,
    )

    assert abandoned is not None and selected is not None
    assert abandoned.target_key == selected.target_key
    assert not projection_store.variant_store_path(
        vault, selected.target_key
    ).exists()
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=selected_candidate,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )
    committed = semantic_writes.commit_existing(vault, preflight=preflight)
    assert committed.mutated is True
    assert target.read_text(encoding="utf-8") == selected_candidate


def test_policy_and_catalog_publications_from_one_predecessor_have_one_winner(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    policy_writer = store.open_authorization_session_connection(vault)
    catalog_writer = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            policy_writer,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        schema_v4.publish_policy_generation(
            policy_writer,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="policy-race",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-policy-race",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )

        with pytest.raises(schema_v4.ActiveTupleStale):
            schema_v4.publish_catalog_generation(
                catalog_writer,
                expected=expected,
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=2,
                    descriptor=b'{"artifacts":["Notes/loser.md"]}',
                    artifact_count=1,
                    created_at=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-catalog-loser",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                receipt_event_id="receipt-catalog-loser",
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert policy_writer.execute(
            "SELECT COUNT(*) FROM catalog_generation_descriptors "
            "WHERE catalog_generation=2"
        ).fetchone() == (0,)
        assert policy_writer.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE event_id='receipt-catalog-loser'"
        ).fetchone() == (0,)
    finally:
        catalog_writer.close()
        policy_writer.close()


def test_active_reader_refuses_corrupt_publication_predecessor(tmp_path: Path) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_policy_generation(
            connection,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="corrupt-predecessor",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-corrupt-predecessor",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )
        connection.execute("DROP TRIGGER governance_tuple_publications_no_update")
        connection.execute(
            "UPDATE governance_tuple_publications "
            "SET predecessor_activation_state_digest=? WHERE activation_epoch=2",
            ("f" * 64,),
        )
        connection.commit()

        with pytest.raises(schema_v4.SchemaV4Error, match="activation state"):
            schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=LOGICAL_VAULT_ID,
                expected_activation_store_id=ACTIVATION_STORE_ID,
                expected_activation_epoch=2,
                expected_activation_state_digest=(
                    publication.active.activation_state_digest
                ),
            )
    finally:
        connection.close()


def test_external_activation_digest_binds_projection_namespace_bytes(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        connection.execute(
            "DROP TRIGGER governance_projection_namespaces_no_update"
        )
        evidence = b'{"ready":false,"tampered":true}'
        namespace_digest = schema_v4._framed_digest(
            b"exomem.authorization-projection-namespace.v1",
            _compiled(_documents(ceiling=2)).fingerprint.encode("ascii"),
            b"1",
            b"1",
            b"namespace-first",
            evidence,
            str(now).encode("ascii"),
        )
        connection.execute(
            "UPDATE governance_projection_namespaces "
            "SET evidence=?, namespace_digest=? WHERE namespace_id='namespace-first'",
            (evidence, namespace_digest),
        )
        connection.commit()

        with pytest.raises(schema_v4.SchemaV4Error, match="activation state"):
            schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=LOGICAL_VAULT_ID,
                expected_activation_store_id=ACTIVATION_STORE_ID,
                expected_activation_epoch=1,
                expected_activation_state_digest=migration.activation_state_digest,
            )
    finally:
        connection.close()


def test_v4_policy_loader_reuses_only_exact_pinned_source_compiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    policy._compile_pinned_documents.cache_clear()
    original = policy._compile_document_bytes
    calls = 0

    def counting_compile(documents: dict[str, bytes]):
        nonlocal calls
        calls += 1
        return original(documents)

    monkeypatch.setattr(policy, "_compile_document_bytes", counting_compile)

    assert policy.load(vault).rules[0].ceiling == 2
    assert policy.load(vault).rules[0].ceiling == 2
    assert calls == 1

    _write_workspace(vault, _documents(ceiling=1))

    assert policy.load(vault).rules[0].ceiling == 2
    assert calls == 2
