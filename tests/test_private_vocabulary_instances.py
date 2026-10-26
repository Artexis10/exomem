"""Private registry operations keep their definitions and audit evidence private."""

import datetime as dt
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_connector_boundary import SCOPE
from test_connector_boundary import configured_boundary as configured_boundary

from exomem import entity_types, registry_history
from exomem.governance.principal import library_scope
from exomem.vocabulary import registry
from exomem.vocabulary.registry import content_hash


@pytest.fixture
def private_instances(configured_boundary, vault):
    from exomem import state_migration
    from exomem.vocabulary import registry_specs

    config, authenticate = configured_boundary
    public = {"namespace": "Knowledge Base/_Schema/public", "history": "public", "overrides": {}}
    for spec in registry_specs().values():
        if spec.overlay(vault).exists():
            public["overrides"][spec.stem] = {"overlay": spec.overlay(vault).relative_to(vault).as_posix()}
    data = json.loads(config.read_text())
    data["vocabulary"] = {
        "public": public,
        "private": {SCOPE: {"namespace": "Knowledge Base/_Schema/private", "history": "private"}},
        "destinations": {},
        "selections": {},
    }
    # A limited client creates only under capture paths, so one entity folder is one.
    data["capture_paths"] += [
        "Knowledge Base/_Schema/public", "Knowledge Base/_Schema/history/public",
        "Knowledge Base/Entities/Venues",
    ]
    config.write_text(json.dumps(data))
    authority = state_migration.assert_offline_migration_authority(source="private vocabulary fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    return authenticate


def test_private_relation_alias_records_the_canonical_promoted_edge(private_instances, vault):
    from exomem import commands, review_state
    from exomem.governance.principal import request_scope

    with request_scope(private_instances("full")):
        inspected = commands.op_schema_memory(
            vault, "inspect", subject="relations", registry_scope=SCOPE,
        )
        saved = commands.op_schema_memory(
            vault, "save", subject="relations", registry_scope=SCOPE,
            expected_hash=inspected["content_hash"], why="Register a private relation.",
            proposal={"upsert": {"vault.accounts_for": {
                "parent": "supports", "description": "Accounts for a synthetic ledger.",
                "aliases": ["accounts"],
            }}},
        )
        assert saved["valid"], saved
        inspected_type = commands.op_schema_memory(
            vault, "inspect", subject="entity-types", registry_scope=SCOPE,
        )
        saved_type = commands.op_schema_memory(
            vault, "save", subject="entity-types", registry_scope=SCOPE,
            expected_hash=inspected_type["content_hash"], why="Register private entity placement.",
            proposal={"upsert": {"venue": {
                "label": "Venue", "guidance": "A recurring meeting place.", "parent": "concept",
                "attributes": {"folder": "Private Venues", "optional_frontmatter": ["project"]},
            }}},
        )
        assert saved_type["valid"], saved_type
        target = commands.op_connect_memory(
            vault, "create-entity", entity_type="venue", name="Tidal Ledger",
            summary="A recurring synthetic ledger.", project="private-project", registry_scope=SCOPE,
        )["path"]
        source = commands.op_connect_memory(
            vault, "create-entity", entity_type="venue", name="Harbour Accounts",
            summary="A recurring synthetic account.", project="private-project", registry_scope=SCOPE,
            relations=[{"kind": "accounts", "target": target}],
        )["path"]

        edges = [
            subject for entry in review_state.ReviewStateStore(vault).load()["promotions"].values()
            if entry["kind"] == "edge" for subject in entry["subjects"]
            if subject["subject"] == source
        ]

        assert len(edges) == 1
        assert edges[0]["predicate"] == "vault.accounts_for"
        assert edges[0]["target"] == target


@pytest.mark.parametrize("include_public_subject", [False, True])
def test_private_promotion_is_admitted_before_count_read_or_settlement(
    private_instances, vault, monkeypatch, include_public_subject,
):
    """A limited client cannot discover or consume a private registry notice."""
    from exomem import command_surface, commands, due_state, promotions, review_state
    from exomem.governance.principal import request_scope

    monkeypatch.setattr(command_surface, "mcp_caller_identity", lambda: {"session_id": "creator"})
    monkeypatch.setattr(promotions, "write_token", lambda: "one-originating-bulk-write")
    with request_scope(private_instances("full")):
        inspected = commands.op_schema_memory(
            vault, "inspect", subject="entity-types", registry_scope=SCOPE,
        )
        saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", registry_scope=SCOPE,
            expected_hash=inspected["content_hash"], why="Add a private meeting place type.",
            proposal={"upsert": {"venue": {
                "label": "Venue", "guidance": "A meeting place.", "parent": "concept",
                "attributes": {"folder": "Venues"},
            }}},
        )
        assert saved["valid"], saved
        if include_public_subject:
            inspected_public = commands.op_schema_memory(vault, "inspect", subject="entity-types")
            saved_public = commands.op_schema_memory(
                vault, "save", subject="entity-types", expected_hash=inspected_public["content_hash"],
                why="Add a public meeting place type in the same write.",
                proposal={"upsert": {"venue": {
                    "label": "Venue", "guidance": "A meeting place.", "parent": "concept",
                    "attributes": {"folder": "Venues"},
                }}},
            )
            assert saved_public["valid"], saved_public
        promotion_id = next(
            key for key, row in review_state.ReviewStateStore(vault).load()["promotions"].items()
            if row["kind"] == "registry"
        )
        ref = promotions.promotion_ref(promotion_id)
        assert promotions.served_rows(vault, principal=private_instances("limited")) == []

    monkeypatch.setattr(command_surface, "mcp_caller_identity", lambda: {"session_id": "reader"})
    with request_scope(private_instances("limited")):
        block = due_state.served(vault)
        assert ((block or {}).get("categories") or {}).get(promotions.FAMILY, 0) == 0
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
            commands.op_review_item_context(vault, ref)
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
            commands.op_triage_memory(
                vault, ref=ref, action="dismiss",
                expected_fingerprint=promotions.identity_fingerprint(promotion_id),
            )
        promotions.settle_delivered(vault, [{"category": promotions.FAMILY, "ref": ref}])

    with request_scope(private_instances("full")):
        assert [row["ref"] for row in promotions.served_rows(vault)] == [ref]
        assert commands.op_review_item_context(vault, ref)["ref"] == ref
        promotions.settle_delivered(vault, [{"category": promotions.FAMILY, "ref": ref}])
        assert promotions.served_rows(vault) == []


def test_catalogue_ranking_keeps_the_page_selected_status_instance(
    private_instances, vault, warm_managed_cell, monkeypatch
):
    """Equal authored labels in two instances must retain their different multipliers."""
    from test_recall_integration import _PageReadSentinel

    from exomem import find as find_module
    from exomem import find_policy, lifecycle_statuses
    from exomem.governance.principal import request_scope
    from exomem.ranking_config import DEFAULT_RANKING
    from exomem.vocabulary import instances

    who = private_instances("full")
    with request_scope(who):
        for scope, lifecycle in ((None, "live"), (SCOPE, "superseded")):
            overlay = instances.select(vault, lifecycle_statuses.SPEC, scope).overlay(vault)
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text(
                "schema_version: 1\nentries:\n  locally-filed:\n"
                f"    attributes: {{class: {lifecycle}}}\n", encoding="utf-8",
            )
        paths = []
        for name, selected in (("public-ranking", False), ("private-ranking", True)):
            rel = f"Knowledge Base/Notes/{name}.md"
            (vault / rel).write_text(
                "---\ntype: insight\nstatus: locally-filed\n"
                + (f"project: private-project\nregistry_scope: {SCOPE}\n" if selected else "")
                + "---\n\nranking instance marker\n", encoding="utf-8",
            )
            paths.append(rel)
        warm_managed_cell(vault)
        parsed = {rel: find_module._CACHE.get(vault / rel, vault) for rel in paths}
        reads = _PageReadSentinel(vault)
        reads.install(monkeypatch)
        metadata = find_module._managed_page_metadata(
            vault, set(paths), scope="kb", freshness_key=find_module.FreshnessSnapshot(vault).kb(),
            pending=None, include_ranking=True,
        )
        views = {rel: row.ranking for rel, row in metadata.items()}
        kwargs = dict(prefer_compiled=False, prefer_active=True, temporal=False)
        expected = find_policy.apply_post_rrf_multipliers(
            [(rel, 1.0) for rel in paths], "marker", DEFAULT_RANKING,
            page_of=parsed.get, status_basis=lifecycle_statuses.Basis(vault), **kwargs,
        )
        actual = find_policy.apply_post_rrf_multipliers(
            [(rel, 1.0) for rel in paths], "marker", DEFAULT_RANKING,
            page_of=views.get, status_basis=lifecycle_statuses.Basis(vault), **kwargs,
        )
    assert actual == expected == [
        (paths[0], 1.0), (paths[1], DEFAULT_RANKING.superseded_penalty),
    ]
    assert reads.count == 0, reads.report()


@pytest.fixture
def bound_private_session(private_instances, vault, tmp_path, monkeypatch, request):
    import test_authorization_session_wire_reconnect as wire
    from test_vocabulary_authority_lifecycle import _publish_floor_two

    from exomem.governance import authorization_custody, authorization_session_lifecycle, store
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import instances

    transport = private_instances("full")
    with request_scope(transport):
        overlay = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\nentity_types: {}\n", encoding="utf-8")
    policy_shape = getattr(request, "param", None)
    if policy_shape in {"default-deny", "deny-history-file"}:
        denied_scope = SCOPE
        if policy_shape == "deny-history-file":
            denied_scope = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
            (vault / "Knowledge Base/_Governance/scopes/retained.yaml").write_text(
                f"governance_version: 1\nid: {denied_scope}\n"
                'paths: ["_Schema/history/private/entity-types/*.yaml"]\n', encoding="utf-8",
            )
        rules = vault / "Knowledge Base/_Governance/rules"
        rules.mkdir(exist_ok=True)
        (rules / "private.yaml").write_text(
            f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAY\nscope_ids: [{denied_scope}]\n"
            f"audience: {transport.audience_id}\nceiling: 0\n", encoding="utf-8",
        )
    custody_root = tmp_path / "custody"
    wire._configure_v4_authority(vault, custody_root, monkeypatch)
    custody_root.chmod(0o700)
    now = int(time.time())
    _publish_floor_two(authorization_custody.load_authorization_custody(vault, now=now))
    custody = authorization_custody.load_authorization_custody(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        issuance = authorization_session_lifecycle.open_session(
            connection, custody=custody, principal_id=transport.audience_id,
            issuer_family=transport.issuer_family, now=now, ttl_seconds=600,
        )
    finally:
        connection.close()
    who = replace(transport, authorization_session_id=issuance.context.session_id,
                  verified_authorization_session=issuance.context)
    return who, custody


@pytest.fixture
def private_writer(bound_private_session, vault, tmp_path):
    from exomem import vocabulary_authority, vocabulary_control, writer_lease

    who, custody = bound_private_session
    now = int(time.time())
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    control = vocabulary_control.VocabularyControl(
        vocabulary_authority.VocabularyAuthority(vault),
        owner_decision_callback=lambda intent: vocabulary_authority._trusted_owner_decision_for_adapter(
            owner_id="owner", ceremony_id=f"control-{intent.binding_digest[:20]}",
            binding_digest=intent.binding_digest, expires_at=now + 300,
        ),
        deployment_floor_callback=lambda _intent: vocabulary_authority._deployment_floor_for_adapter(
            runtime="vocabulary-authority/v2", generation=custody.control.activation_epoch,
        ),
        activation_guard_factory=lambda root: manager.mutation_guard(
            root, operation="vocabulary-authority-activation",
            holder_kind="vocabulary-authority-control", activation_admission=True,
        ),
    )
    assert control.activate(principal=who, body={}).mode == "v2"
    return manager, control


@pytest.mark.parametrize("history_exists", [False, True])
def test_bound_private_session_inspects_and_publishes_with_history_storage(
    bound_private_session, private_instances, private_writer, vault, history_exists,
):
    """A signed floor-two session must admit directory metadata before registry publication."""
    from exomem import commands
    from exomem.governance import principal
    from exomem.vocabulary import instances

    who, _custody = bound_private_session
    manager, control = private_writer
    control.grant(principal=who, body={
        "actions": ["entity_type.add"], "scope": "vault", "expires_at": int(time.time()) + 300,
    })
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "schema_memory")
    with principal.request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        directory = registry_history.history_dir(vault, spec.stem)
        if history_exists:
            directory.mkdir(parents=True)
        inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        assert inspected.get("available", True), inspected
        saved = manager.invoke(command, (vault,), {
            "operation": "save", "subject": "entity-types", "registry_scope": SCOPE,
            "proposal": {"upsert": {"venue": {
                "label": "Venue", "guidance": "A stable meeting place.", "parent": "concept",
                "attributes": {"folder": "Venues"},
            }}},
            "expected_hash": inspected["content_hash"], "why": "Private recurring places",
        }, idempotency_key="private-type", read_only=False)
        assert saved["state"] == "committed", saved
        after = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        history = commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)
        assert after.get("available", True), after
        assert history["versions"][0]["why"] == "Private recurring places"
        restored = manager.invoke(command, (vault,), {
            "operation": "restore", "subject": "entity-types", "registry_scope": SCOPE,
            "version": history["versions"][0]["version"], "expected_hash": after["content_hash"],
            "why": "Restore the private core",
        }, idempotency_key="private-restore", read_only=False)
        assert restored["state"] == "committed", restored
        assert entity_types.load_entity_types(vault, registry_scope=SCOPE).resolve("venue") is None
    with principal.request_scope(private_instances("limited")):
        assert commands.op_schema_memory(
            vault, "inspect", subject="entity-types", registry_scope=SCOPE,
        )["available"] is False
        public = commands.op_schema_memory(vault, "inspect", subject="entity-types")
        assert all(entry["key"] != "venue" for entry in public["entries"])


@pytest.mark.parametrize("failure", ["closed-session", "custody-loss", "principal-mismatch"])
def test_bound_history_metadata_refuses_lost_session_authority(
    bound_private_session, vault, failure,
):
    """Owner and open-policy shortcuts cannot retain metadata authority after a session loses custody."""
    from exomem import commands
    from exomem.governance import authorization_custody, authorization_session_lifecycle, store
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import contract, instances

    who, custody = bound_private_session
    who = replace(who, purpose="registry-editing")
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        witness = contract.capture_admission(vault, spec, spec.overlay(vault).read_bytes())
        assert contract.history_refusal(vault, spec) is None
    if failure == "closed-session":
        connection = store.open_authorization_session_connection(vault)
        try:
            authorization_session_lifecycle.close_verified_session(
                connection, custody=custody, context=who.verified_authorization_session, now=int(time.time()),
            )
        finally:
            connection.close()
    elif failure == "custody-loss":
        custody.keyring_path.unlink()
    else:
        who = replace(who, authorization_session_id="another-session")
    with request_scope(who):
        inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        assert inspected == {"subject": "entity-types", "available": False, "reason": "audience_restricted"}
        assert contract.history_refusal(vault, spec)["available"] is False
        with pytest.raises((registry.RegistryError, authorization_session_lifecycle.AuthorizationSessionUnavailable,
                            authorization_custody.AuthorizationCustodyUnavailable)):
            witness.recheck()


@pytest.mark.parametrize("substitution", ["file", "symlink", "ancestor-symlink", "reserved-identity", "unreadable"])
def test_bound_history_metadata_refuses_physical_substitution(
    bound_private_session, vault, tmp_path, monkeypatch, substitution,
):
    """Directory metadata admission must not accept aliases, reserved identities or a file replacement."""
    from exomem import commands, reserved_paths
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import contract, instances

    who, _custody = bound_private_session
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        directory = registry_history.history_dir(vault, spec.stem)
        directory.mkdir(parents=True)
        witness = contract.capture_admission(vault, spec, spec.overlay(vault).read_bytes())
        assert contract.history_refusal(vault, spec) is None
    if substitution == "file":
        directory.rmdir()
        directory.write_text("Not a history directory", encoding="utf-8")
    elif substitution == "symlink":
        directory.rmdir()
        directory.symlink_to(tmp_path, target_is_directory=True)
    elif substitution == "ancestor-symlink":
        moved = tmp_path / "moved-history"
        directory.parent.rename(moved)
        directory.parent.symlink_to(moved, target_is_directory=True)
    elif substitution == "reserved-identity":
        identity = reserved_paths.inspect_generic_path(vault, directory.relative_to(vault).as_posix())
        monkeypatch.setattr(reserved_paths, "_published_identity_catalogue", lambda _root:
                            reserved_paths.IdentityCatalogue({
                                (identity.device, identity.inode, identity.kind): "governance-tree",
                            }))
    else:
        directory.chmod(0)
    try:
        with request_scope(who):
            inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
            assert inspected["available"] is False, inspected
            assert contract.history_refusal(vault, spec)["available"] is False
            with pytest.raises(registry.RegistryError, match="REGISTRY_UNAVAILABLE"):
                witness.recheck()
    finally:
        if substitution == "unreadable":
            directory.chmod(0o700)


@pytest.mark.parametrize("bound_private_session", ["default-deny"], indirect=True)
@pytest.mark.parametrize("granted_artifact", ["overlay", "history-file", "other-artifact"])
def test_bound_history_metadata_cannot_inherit_an_artifact_byte_grant(
    bound_private_session, vault, granted_artifact,
):
    """A valid grant for private bytes must not lift a default-denied directory or changed bytes."""
    import hashlib

    from exomem import commands
    from exomem.governance import authorization_session_authority, egress, membership, policy, store
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import contract, instances

    who, custody = bound_private_session
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        overlay = spec.overlay(vault).relative_to(vault).as_posix()
        directory = registry_history.history_dir(vault, spec.stem)
        directory.mkdir(parents=True)
        retained = directory / f"{registry_history.version_id(dt.datetime.now(dt.UTC), 'retained')}.yaml"
        retained.write_text("schema_version: 1\n", encoding="utf-8")
        target = (overlay if granted_artifact == "overlay" else
                  retained.relative_to(vault).as_posix() if granted_artifact == "history-file" else
                  "Knowledge Base/_Schema/private/relation-registry.yaml")
        if granted_artifact == "other-artifact":
            (vault / target).write_text("schema_version: 1\nextensions: {}\n", encoding="utf-8")
        compiled = policy.load(vault)
        assert not compiled.empty and not compiled.blocked
        assert contract.history_refusal(vault, spec)["available"] is False
        paths = tuple(sorted({overlay, target}))
        fingerprints = tuple(hashlib.sha256((vault / path).read_bytes()).hexdigest() for path in paths)
        manifest = tuple(authorization_session_authority.SessionMembership(
            path, fingerprint, tuple(sorted(membership.evaluate_path_only(vault, path, compiled).require_classified())),
        ) for path, fingerprint in zip(paths, fingerprints, strict=True))
        now = int(time.time())
        connection = store.open_authorization_session_connection(vault)
        try:
            token = authorization_session_authority.mint_escalation_token(
                connection, context=who.verified_authorization_session, signing_key=custody.keyring.active_key.key,
                audience=who.audience_id, purpose=None, max_level=6, org_ceiling=6,
                paths=paths, fingerprints=fingerprints, scope_ids=(SCOPE,), now=now, expires_at=now + 300,
            )
            authorization_session_authority.redeem_escalation_token(
                connection, token=token, context=who.verified_authorization_session,
                signing_key=custody.keyring.active_key.key, audience=who.audience_id, purpose=None,
                membership=manifest, policy_fingerprint=compiled.fingerprint, now=now,
            )
        finally:
            connection.close()
        assert egress.release_level_for_path_only(vault, overlay) == egress.LEVEL_FULL
        assert egress.release_level_for_path_only(vault, target) == egress.LEVEL_FULL
        assert contract.history_refusal(vault, spec)["available"] is False
        assert commands.op_schema_memory(
            vault, "inspect", subject="entity-types", registry_scope=SCOPE,
        )["available"] is False
        (vault / target).write_text("Changed private bytes", encoding="utf-8")
        assert egress.release_level_for_path_only(vault, target) == egress.LEVEL_NONE


@pytest.mark.parametrize("bound_private_session", ["deny-history-file"], indirect=True)
def test_bound_history_metadata_does_not_release_a_withheld_snapshot(
    bound_private_session, vault,
):
    """Readable directory metadata cannot disclose snapshot bytes without their own current grant."""
    import hashlib

    from exomem import commands
    from exomem.governance import authorization_session_authority, egress, membership, policy, store
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import instances

    who, custody = bound_private_session
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        directory = registry_history.history_dir(vault, spec.stem)
        directory.mkdir(parents=True)
        retained = directory / f"{registry_history.version_id(dt.datetime.now(dt.UTC), 'retained')}.yaml"
        retained.write_text('# exomem-history: {"why": "Retained private state"}\nschema_version: 1\n', encoding="utf-8")
        path = retained.relative_to(vault).as_posix()
        compiled = policy.load(vault)
        scopes = tuple(sorted(membership.evaluate_path_only(vault, path, compiled).require_classified()))
        assert len(scopes) == 2, (scopes, compiled)
        assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_NONE
        inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        assert inspected.get("available", True), inspected
        assert commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)["available"] is False
        with pytest.raises(registry_history.HistoryAdmissionRefused, match="^AUDIENCE_RESTRICTED: registry history is unavailable$"):
            registry_history.versions(vault, stem=spec.stem)
        with pytest.raises(registry_history.HistoryAdmissionRefused, match="^AUDIENCE_RESTRICTED: registry history is unavailable$"):
            registry_history.read_version(vault, stem=spec.stem, version=retained.stem)
        fingerprint = hashlib.sha256(retained.read_bytes()).hexdigest()
        now = int(time.time())
        connection = store.open_authorization_session_connection(vault)
        try:
            token = authorization_session_authority.mint_escalation_token(
                connection, context=who.verified_authorization_session, signing_key=custody.keyring.active_key.key,
                audience=who.audience_id, purpose=None, max_level=6, org_ceiling=6,
                paths=(path,), fingerprints=(fingerprint,), scope_ids=scopes, now=now, expires_at=now + 300,
            )
            authorization_session_authority.redeem_escalation_token(
                connection, token=token, context=who.verified_authorization_session,
                signing_key=custody.keyring.active_key.key, audience=who.audience_id, purpose=None,
                membership=(authorization_session_authority.SessionMembership(path, fingerprint, scopes),),
                policy_fingerprint=compiled.fingerprint, now=now,
            )
        finally:
            connection.close()
        history = commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)
        assert history["versions"][0]["why"] == "Retained private state"
        retained.write_text('# exomem-history: {"why": "Changed private state"}\nschema_version: 1\n', encoding="utf-8")
        assert commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)["available"] is False


@pytest.mark.parametrize("history_exists", [False, True])
@pytest.mark.parametrize("bound_private_session", ["deny-history-file"], indirect=True)
def test_history_refuses_a_denied_snapshot_created_after_preflight(
    bound_private_session, vault, monkeypatch, history_exists,
):
    """A safe empty history must not authorize a newly created snapshot's reason."""
    from exomem import commands
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import instances

    who, _custody = bound_private_session
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        directory = registry_history.history_dir(vault, spec.stem)
        if history_exists:
            directory.mkdir(parents=True)
        versions = registry_history.versions

        def create_then_read(root, *, stem):
            directory.mkdir(parents=True, exist_ok=True)
            name = registry_history.version_id(dt.datetime.now(dt.UTC), "new snapshot")
            (directory / f"{name}.yaml").write_text(
                '# exomem-history: {"why": "Withheld new state"}\nschema_version: 1\n',
                encoding="utf-8",
            )
            return versions(root, stem=stem)

        monkeypatch.setattr(registry_history, "versions", create_then_read)
        result = commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)
        assert result == {"subject": "entity-types", "available": False, "reason": "audience_restricted"}


@pytest.fixture
def granted_private_snapshot(bound_private_session, vault):
    import hashlib

    from exomem.governance import authorization_session_authority, membership, policy, store
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import instances

    who, custody = bound_private_session
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        directory = registry_history.history_dir(vault, spec.stem)
        directory.mkdir(parents=True)
        version = registry_history.version_id(dt.datetime.now(dt.UTC), "granted snapshot")
        retained = directory / f"{version}.yaml"
        raw = b'# exomem-history: {"why": "Granted private state"}\nschema_version: 1\nentity_types: {}\n'
        retained.write_bytes(raw)
        path = retained.relative_to(vault).as_posix()
        compiled = policy.load(vault)
        scopes = tuple(sorted(membership.evaluate_path_only(vault, path, compiled).require_classified()))
        fingerprint = hashlib.sha256(raw).hexdigest()
        now = int(time.time())
        connection = store.open_authorization_session_connection(vault)
        try:
            token = authorization_session_authority.mint_escalation_token(
                connection, context=who.verified_authorization_session, signing_key=custody.keyring.active_key.key,
                audience=who.audience_id, purpose=None, max_level=6, org_ceiling=6,
                paths=(path,), fingerprints=(fingerprint,), scope_ids=scopes, now=now, expires_at=now + 300,
            )
            authorization_session_authority.redeem_escalation_token(
                connection, token=token, context=who.verified_authorization_session,
                signing_key=custody.keyring.active_key.key, audience=who.audience_id, purpose=None,
                membership=(authorization_session_authority.SessionMembership(path, fingerprint, scopes),),
                policy_fingerprint=compiled.fingerprint, now=now,
            )
        finally:
            connection.close()
    return who, custody, spec, retained, raw, version


@pytest.mark.parametrize("bound_private_session", ["deny-history-file"], indirect=True)
@pytest.mark.parametrize("operation", ["history", "restore"])
@pytest.mark.parametrize("change", ["consumed-replacement", "closed-session"])
def test_history_and_restore_refuse_snapshot_authority_lost_after_preflight(
    granted_private_snapshot, private_writer, vault, monkeypatch, operation, change,
):
    """Consumed bytes and live custody must remain admitted before parsing or publication."""
    from exomem import _held_fs_posix, commands
    from exomem.governance import authorization_session_lifecycle, store
    from exomem.governance.principal import request_scope

    who, custody, spec, retained, raw, version = granted_private_snapshot
    manager, _control = private_writer
    read = _held_fs_posix.PosixHeldFilesystem.read
    changed = False
    consuming = False

    def begin_consumption(reader):
        def consume(*args, **kwargs):
            nonlocal consuming
            consuming = True
            return reader(*args, **kwargs)
        return consume

    def change_during_read(filesystem, file):
        nonlocal changed
        if not consuming or file.name != retained.name or changed:
            return read(filesystem, file)
        changed = True
        if change == "consumed-replacement":
            retained.write_bytes(raw.replace(b"Granted private state", b"Withheld changed state"))
            result = read(filesystem, file)
            retained.write_bytes(raw)
            return result
        result = read(filesystem, file)
        connection = store.open_authorization_session_connection(vault)
        try:
            authorization_session_lifecycle.close_verified_session(
                connection, custody=custody, context=who.verified_authorization_session, now=int(time.time()),
            )
        finally:
            connection.close()
        return result

    with request_scope(replace(who, purpose=None)):
        inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        assert commands.op_schema_memory(
            vault, "history", subject="entity-types", registry_scope=SCOPE,
        )["versions"][0]["why"] == "Granted private state"
        overlay = spec.overlay(vault).read_bytes()
        kept = sorted(retained.parent.iterdir())
        monkeypatch.setattr(_held_fs_posix.PosixHeldFilesystem, "read", change_during_read)
        reader = "history_view" if operation == "history" else "read_version"
        monkeypatch.setattr(registry_history, reader, begin_consumption(getattr(registry_history, reader)))
        arguments = {"operation": operation, "subject": "entity-types", "registry_scope": SCOPE}
        if operation == "restore":
            arguments.update(version=version, expected_hash=inspected["content_hash"], why="Restore admitted state")
            command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "schema_memory")
            result = manager.invoke(command, (vault,), arguments, idempotency_key="withheld-restore", read_only=False)
        else:
            result = commands.op_schema_memory(vault, **arguments)
        assert changed
        assert result == {"subject": "entity-types", "available": False, "reason": "audience_restricted"}
        assert spec.overlay(vault).read_bytes() == overlay
        assert sorted(retained.parent.iterdir()) == kept


def test_bootstrap_does_not_reuse_history_after_session_closes(
    bound_private_session, vault, monkeypatch,
):
    """A previously populated bootstrap must omit new keys after current custody is lost."""
    from exomem.governance import authorization_session_lifecycle, store
    from exomem.governance.principal import request_scope
    from exomem.vocabulary import bootstrap, instances

    who, custody = bound_private_session
    monkeypatch.setattr(bootstrap, "registry_specs", lambda: {"entity-types": entity_types.SPEC})
    with request_scope(who):
        spec = instances.select(vault, entity_types.SPEC)
        spec.overlay(vault).parent.mkdir(parents=True, exist_ok=True)
        spec.overlay(vault).write_text("schema_version: 1\nentity_types: {}\n", encoding="utf-8")
        directory = registry_history.history_dir(vault, spec.stem)
        directory.mkdir(parents=True)
        version = registry_history.version_id(dt.datetime.now(dt.UTC), "public addition")
        (directory / f"{version}.yaml").write_text(
            '# exomem-history: ' + json.dumps({
                "at": dt.datetime.now(dt.UTC).isoformat(), "added": {"entity-types": ["concept"]},
            }) + '\nschema_version: 1\n', encoding="utf-8",
        )
        populated = bootstrap.block(vault, inspect_route="schema_memory")
        assert populated["entity-types"].get("new") == ["concept"], populated
        recently_added = bootstrap.recently_added

        def close_then_read(root, spec, snapshot, *, since):
            if spec.name == "entity-types":
                connection = store.open_authorization_session_connection(vault)
                try:
                    authorization_session_lifecycle.close_verified_session(
                        connection, custody=custody, context=who.verified_authorization_session, now=int(time.time()),
                    )
                finally:
                    connection.close()
            return recently_added(root, spec, snapshot, since=since)

        monkeypatch.setattr(bootstrap, "recently_added", close_then_read)
        result = bootstrap.block(vault, inspect_route="schema_memory")
        assert "new" not in result["entity-types"]
        assert "top" in result["entity-types"]
        assert "new_since" not in result


@pytest.mark.parametrize(("client", "destination_allowed"), [
    ("full", False), ("limited", True), ("limited", False),
])
def test_first_use_capture_registers_in_selected_storage_and_can_rearm(
    private_instances, configured_boundary, vault, source_schema, client, destination_allowed,
):
    """A new source kind must remain usable without creating an unassigned overlay."""
    from test_source_taxonomy import _capture

    from exomem import add, source_taxonomy, state_migration
    from exomem.governance import principal
    from exomem.vocabulary import instances

    if destination_allowed:
        config, _authenticate = configured_boundary
        document = json.loads(config.read_text())
        document["capture_paths"].append("Knowledge Base/Sources/Field Notebook")
        config.write_text(json.dumps(document))
        authority = state_migration.assert_offline_migration_authority(source="capture fixture setup")
        state_migration.arm_connector_boundary_offline(vault, authority=authority)

    legacy = source_taxonomy.registry_path(vault)
    with principal.request_scope(private_instances(client)):
        selected = instances.select(vault, source_taxonomy.KIND_SPEC).overlay(vault)
        if client == "limited" and not destination_allowed:
            with pytest.raises(add.AddError) as denied:
                _capture(vault, source_schema, source_type="field-notebook", title="Bound source capture")
            assert denied.value.code == "WRITE_REFUSED"
            assert denied.value.reason
            assert not selected.exists() and not legacy.exists()
            assert source_taxonomy.load_taxonomy(vault).resolve_kind("field-notebook").status == "unregistered"
            return

        captured = _capture(vault, source_schema, source_type="field-notebook", title="Bound source capture")
        assert (vault / captured.path).is_file()
        assert selected.is_file() and not legacy.exists()
        assert source_taxonomy.load_taxonomy(vault).resolve_kind("field-notebook").status == "registered"

    authority = state_migration.assert_offline_migration_authority(source="capture rearm acceptance")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)


@pytest.mark.parametrize("failure", [None, "duplicate", "definition", "untouched-race", "selected-race"])
def test_complete_batch_uses_each_staged_instance_definition(private_instances, vault, warm_managed_cell, monkeypatch, failure):
    """A private entity must use its own staged folder and relation meaning."""
    from test_vocabulary_authority import (
        _activate,
        _approve_request,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    from exomem import (
        epistemic_graph,
        find,
        freshness,
        reserved_paths,
        vocabulary_authority,
        vocabulary_effects,
        vocabulary_gate,
    )
    from exomem import vault as vault_module
    from exomem.governance import principal

    images = []
    for instance, folder, identifier in (
        ("public", "PublicVenues", "11111111-1111-4111-8111-111111111111"),
        ("private", "PrivateVenues", "22222222-2222-4222-8222-222222222222"),
    ):
        images.extend((
            vocabulary_effects.CanonicalWriteImage(
                f"Knowledge Base/_Schema/{instance}/entity-types.yaml", None,
                ("schema_version: 1\nentity_types:\n  venue:\n"
                 f"    folder: {folder}\n    label: Venue\n    aliases: []\n"
                 "    capture_guidance: A recurring meeting place.\n").encode(),
            ),
            vocabulary_effects.CanonicalWriteImage(
                f"Knowledge Base/_Schema/{instance}/relation-registry.yaml", None,
                (b"schema_version: 1\nextensions:\n  venue.near:\n"
                 b"    parent: relates_to\n    description: Nearby places\n"),
            ),
            vocabulary_effects.CanonicalWriteImage(
                f"Knowledge Base/Entities/{folder}/station.md", None,
                (f"---\ntype: entity\nentity_type: venue\nexomem_id: {identifier}\n"
                 "title: Station\nstatus: active\n"
                 + (f"projects: [private-project]\nregistry_scope: {SCOPE}\n" if instance == "private" else "")
                 + "---\n# Station\n"
                 + ("\n- venue.near: [[Entities/PublicVenues/station]]\n" if instance == "private" else "")).encode(),
            ),
        ))
    if failure == "duplicate":
        image = images[-1]
        images[-1] = replace(image, after=image.after.replace(
            b"22222222-2222-4222-8222-222222222222", b"11111111-1111-4111-8111-111111111111",
        ), after_sha256=None)
    if failure == "definition":
        image = images[0]
        before = image.after.replace(b"label: Venue", b"label: Old venue")
        (vault / image.path).parent.mkdir(parents=True, exist_ok=True)
        (vault / image.path).write_bytes(before)
        images[0] = replace(image, before=before, before_sha256=None)
    with principal.request_scope(private_instances("full")):
        result = vocabulary_effects.classify_additive_effects(
            vault, images, identity_paths_for_id=lambda identifier: (),
        )
    if failure in ("duplicate", "definition"):
        assert any(reason.code == ("duplicate_batch_identity" if failure == "duplicate" else "entity_type_definition_changed")
                   for reason in result.reasons)
    else:
        assert result.state == "reviewed", result.reasons
        assert [effect.action for effect in result.effects].count("entity.create") == 2
        assert [effect.action for effect in result.effects].count("entity_type.add") == 2
        assert [effect.action for effect in result.effects].count("relation_type.add") == 2
        assert [effect.action for effect in result.effects].count("edge.add") == 1
    with principal.request_scope(private_instances("full")):
        warm_managed_cell(vault)
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        checkpoint = freshness.live_recall_checkpoint(vault, "vault")
        assert find.recall_resolver_snapshot(vault, allow_fallback=False, expected_checkpoint=checkpoint) is not None
        install_unit_session_boundary(monkeypatch)
        actor = _principal()
        authority = _store(vault)
        _activate(authority, actor)
        monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda root: authority)

        def write():
            with reserved_paths._owner_authority_scope("schema_memory"), vocabulary_gate.operation_context(
                vault, idempotency_key="complete-selected-batch", command_digest="a" * 64,
                receipt_id="complete-selected-receipt", principal=actor,
            ) as operation:
                vault_module.batch_atomic_write(
                    [vault_module.PlannedWrite(vault / image.path, image.after.decode(), create_only=image.before is None) for image in images],
                    vault_root=vault, post_commit_fanout=False,
                )
                return operation.uses

        with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied) as denied:
            write()
        for image in images:
            assert ((vault / image.path).read_bytes() if (vault / image.path).exists() else None) == image.before
        if failure in ("duplicate", "definition"):
            assert "vocabulary_request_id" not in denied.value.details
            return
        _approve_request(authority, actor, denied.value.details["vocabulary_request_id"])
        if failure in ("untouched-race", "selected-race"):
            original = vault_module._after_batch_destination_published

            def race(path):
                original(path)
                if path == vault / images[-1].path:
                    changed = (vault / "Knowledge Base/_Schema/private/semantic-language-registry.yaml"
                               if failure == "untouched-race" else vault / images[1].path)
                    changed.parent.mkdir(parents=True, exist_ok=True)
                    changed.write_text("schema_version: 1\ncategories: {}\nkinds: {}\n" if failure == "untouched-race"
                                       else images[1].after.decode() + "# concurrent edit\n")

            monkeypatch.setattr(vault_module, "_after_batch_destination_published", race)
            with pytest.raises(ValueError):
                write()
            for image in images:
                if failure == "selected-race" and image.path == images[1].path:
                    continue  # The writer preserves an external editor's replaced target during rollback.
                assert not (vault / image.path).exists()
            return
        uses = write()
        assert {use["action"] for use in uses[0]["authorities"]} == {
            "entity.create", "entity_type.add", "relation_type.add", "edge.add",
        }
        for image in images:
            assert (vault / image.path).read_bytes() == image.after


def test_identical_selected_proposals_review_and_apply_to_their_own_instance(private_instances, vault, tmp_path):
    """The queue must retain both proposals and apply each reviewed target exactly."""
    from exomem import commands, vocabulary_review, writer_lease
    from exomem.governance import principal
    from exomem.vocabulary import contract, instances
    from exomem.vocabulary_state import VocabularyState

    delta = {"upsert": {"venue": {"label": "Venue", "guidance": "A recurring place.",
                                  "attributes": {"folder": "Venues"}}}}
    full, limited = private_instances("full"), private_instances("limited")
    with principal.request_scope(limited):
        withheld_before = vocabulary_review.review(vault)
        public_before = commands.op_schema_memory(vault, "inspect", subject="entity-types")
    queued = {}
    with principal.request_scope(full):
        for scope in ("public", SCOPE):
            spec = instances.select(vault, entity_types.SPEC, scope, authoring=True)
            queued[scope] = contract.queue_for_owner(
                vault, spec, registry.load(spec, vault), delta, "Recurring places", "audience_restricted",
            )["item_ref"]
        assert queued["public"] != queued[SCOPE]
        assert {item["ref"] for item in vocabulary_review.review(vault)["items"]} == set(queued.values())
    with principal.request_scope(limited):
        assert vocabulary_review.review(vault) == withheld_before
        with pytest.raises(ValueError, match="VOCABULARY_ITEM_NOT_FOUND"):
            vocabulary_review.context(vault, ref=queued[SCOPE])
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "schema_memory")
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "proposal-leases"))
    with principal.request_scope(full):
        for scope in (SCOPE, "public"):
            context = vocabulary_review.context(vault, ref=queued[scope])
            item = context["item"]
            spec = instances.select(vault, entity_types.SPEC, scope)
            target = spec.overlay(vault).relative_to(vault).as_posix()
            assert context["targets"][0]["path"] == target
            decision = {"item_ref": item["ref"], "fingerprint": item["fingerprint"],
                        "family": item["family"], "registry_hashes": item["registry_hashes"],
                        "target_versions": item["target_versions"], "outcome": "propose-new",
                        "choice": {"canonical": "venue", "definition": delta["upsert"]["venue"]},
                        "rationale": "A recurring place needs a durable type."}
            vocabulary_review.decide(vault, ref=item["ref"], decision=decision)
            route = context["definitions"]["registration"]["save"].copy()
            assert route.pop("tool") == "schema_memory"
            assert route["registry_scope"] == scope
            result = manager.invoke(command, (vault,), route, idempotency_key=f"apply-{scope}", read_only=False)
            assert result["state"] == "committed", result
            applied = VocabularyState(vault).get(item["ref"])
            assert applied["state"] == "applied", applied
            assert applied["decision"]["application"]["result_currency"]["target_versions"][target] == registry.load(spec, vault).content_hash
            if scope == SCOPE:
                with principal.request_scope(limited):
                    assert commands.op_schema_memory(vault, "inspect", subject="entity-types") == public_before
                    assert vocabulary_review.review(vault) == withheld_before


def test_private_registry_evidence_checks_its_snapshots_without_loading_public_storage(
    private_instances, configured_boundary, vault, monkeypatch,
):
    """A private-only save must not consume public definitions or retain a changed binding."""
    from exomem import state_migration, vocabulary_gate_evidence
    from exomem.governance import principal
    from exomem.vocabulary import instances
    from exomem.vocabulary_effects import CanonicalWriteImage

    with principal.request_scope(private_instances("full")):
        spec = instances.select(vault, entity_types.SPEC, SCOPE, authoring=True)
        path = spec.overlay(vault).relative_to(vault).as_posix()
        raw = b"schema_version: 1\nentity_types:\n  venue:\n    folder: Venues\n    label: Venue\n    aliases: []\n    capture_guidance: Places.\n"
        original = registry.load

        def selected_only(selected, root):
            if root is not None:
                assert selected.instance_id == SCOPE, "private evidence consumed a public registry"
            return original(selected, root)

        monkeypatch.setattr(registry, "load", selected_only)
        evidence = vocabulary_gate_evidence.classify(vault, (CanonicalWriteImage(path, None, raw),))
        try:
            assert set(evidence.transitions) == {(entity_types.SPEC.name, SCOPE)}
            evidence.recheck(phase="before")
            spec.overlay(vault).parent.mkdir(parents=True, exist_ok=True)
            spec.overlay(vault).write_bytes(raw)
            evidence.recheck(phase="after")
            with pytest.raises(ValueError, match="operation definitions changed"):
                evidence.recheck(phase="before")
            config, _authenticate = configured_boundary
            document = json.loads(config.read_text())
            document["vocabulary"]["private"][SCOPE]["history"] = "private-next"
            config.write_text(json.dumps(document))
            authority = state_migration.assert_offline_migration_authority(source="disposable binding race")
            state_migration.arm_connector_boundary_offline(vault, authority=authority)
            with pytest.raises(ValueError, match="selected instance changed"):
                evidence.recheck(phase="after")
        finally:
            evidence.close()


@pytest.mark.parametrize("domain", [None, "meteorology"])
def test_capture_keeps_the_binding_used_to_resolve_a_registered_kind(
    private_instances, configured_boundary, vault, source_schema, monkeypatch, domain,
):
    """Rearming between resolution and planning must not repin a capture's meaning."""
    from test_source_taxonomy import _capture

    from exomem import source_taxonomy, state_migration
    from exomem.governance import principal
    from exomem.vocabulary import instances

    original = source_taxonomy.plan_registrations
    with principal.request_scope(private_instances("full")):
        selected = instances.select(vault, source_taxonomy.KIND_SPEC)
        source_taxonomy.register(vault, kind="field-notebook", domain=domain)
        before = selected.overlay(vault).read_bytes()

        def rearm_before_plan(*args, **kwargs):
            config, _authenticate = configured_boundary
            document = json.loads(config.read_text())
            document["vocabulary"]["public"]["namespace"] = "Knowledge Base/_Schema/public-next"
            config.write_text(json.dumps(document))
            authority = state_migration.assert_offline_migration_authority(source="disposable resolution binding race")
            state_migration.arm_connector_boundary_offline(vault, authority=authority)
            return original(*args, **kwargs)

        monkeypatch.setattr(source_taxonomy, "plan_registrations", rearm_before_plan)
        with pytest.raises(ValueError):
            _capture(vault, source_schema, source_type="field-notebook", title="Retained resolution", domain=domain)
        assert selected.overlay(vault).read_bytes() == before
        assert not instances.select(vault, source_taxonomy.KIND_SPEC).overlay(vault).exists()
        assert not list((vault / "Knowledge Base/Sources").rglob("*retained-resolution.md"))


@pytest.mark.parametrize("race", ["planning", "prepared", "same-path"])
def test_reclassification_keeps_its_binding_through_source_publication(
    private_instances, configured_boundary, vault, source_schema, monkeypatch, race,
):
    """A stopped rearm must refuse a correction and restore an already renamed source."""
    from test_source_taxonomy import _capture

    from exomem import move_file, reclassify_source, source_taxonomy, state_migration
    from exomem.governance import principal
    from exomem.vocabulary import instances

    with principal.request_scope(private_instances("full")):
        config, _authenticate = configured_boundary
        document = json.loads(config.read_text())
        document["capture_paths"] = [path for path in document["capture_paths"] if "/_Schema/" not in path]
        config.write_text(json.dumps(document))
        authority = state_migration.assert_offline_migration_authority(source="disposable unrestricted registry destination")
        state_migration.arm_connector_boundary_offline(vault, authority=authority)
        selected = instances.select(vault, source_taxonomy.KIND_SPEC)
        captured = _capture(vault, source_schema, source_type="field-notebook", title="Original classification")
        source_before = (vault / captured.path).read_bytes()
        registry_before = selected.overlay(vault).read_bytes()

        def rearm_selection():
            document = json.loads(config.read_text())
            document["vocabulary"]["public"]["namespace"] = "Knowledge Base/_Schema/public-next"
            config.write_text(json.dumps(document))
            authority = state_migration.assert_offline_migration_authority(source="disposable correction binding race")
            state_migration.arm_connector_boundary_offline(vault, authority=authority)

        if race == "prepared":
            original = move_file.batch_atomic_write

            def rearm_before_batch(*args, **kwargs):
                rearm_selection()
                return original(*args, **kwargs)

            monkeypatch.setattr(move_file, "batch_atomic_write", rearm_before_batch)
        else:
            original = source_taxonomy.plan_registrations

            def rearm_after_plan(*args, **kwargs):
                planned = original(*args, **kwargs)
                rearm_selection()
                return planned

            monkeypatch.setattr(source_taxonomy, "plan_registrations", rearm_after_plan)

        with pytest.raises(ValueError):
            reclassify_source.reclassify(
                vault, path=captured.path,
                source_kind="field-notebook" if race == "same-path" else "observation-diary",
                reason="Correct the source kind",
            )
        assert (vault / captured.path).read_bytes() == source_before
        assert selected.overlay(vault).read_bytes() == registry_before
        assert not instances.select(vault, source_taxonomy.KIND_SPEC).overlay(vault).exists()
        assert not list((vault / "Knowledge Base/Sources/Observation Diary").rglob("*.md"))


def test_capture_refuses_a_binding_rearmed_after_its_registration_plan(
    private_instances, configured_boundary, vault, source_schema, monkeypatch,
):
    """A retained capture plan cannot publish into storage from a previous binding."""
    from test_source_taxonomy import _capture

    from exomem import add, source_taxonomy, state_migration
    from exomem.governance import principal
    from exomem.vocabulary import instances

    original = add.batch_atomic_write
    with principal.request_scope(private_instances("full")):
        selected = instances.select(vault, source_taxonomy.KIND_SPEC)

        def rearm_then_publish(*args, **kwargs):
            config, _authenticate = configured_boundary
            document = json.loads(config.read_text())
            document["vocabulary"]["public"]["namespace"] = "Knowledge Base/_Schema/public-next"
            config.write_text(json.dumps(document))
            authority = state_migration.assert_offline_migration_authority(source="disposable capture binding race")
            state_migration.arm_connector_boundary_offline(vault, authority=authority)
            return original(*args, **kwargs)

        monkeypatch.setattr(add, "batch_atomic_write", rearm_then_publish)
        with pytest.raises(ValueError):
            _capture(vault, source_schema, source_type="field-notebook", title="Rearmed registration")
        assert not selected.overlay(vault).exists()
        assert not source_taxonomy.registry_path(vault).exists()
        assert not list((vault / "Knowledge Base/Sources").rglob("*rearmed-registration.md"))


@pytest.mark.parametrize(("relation", "changed"), [("venue.near", True), ("relates_to", False)])
def test_instance_moves_change_extension_meaning_but_keep_core_meaning(private_instances, vault, relation, changed):
    """A move between instances must not relabel a private extension as an unchanged public edge."""
    from exomem import memory_refs, relation_registry, vocabulary_effects
    from exomem.governance import principal
    from exomem.vocabulary import instances

    path = "Knowledge Base/Notes/Insights/connection.md"
    target = "Knowledge Base/Notes/Insights/target.md"
    before = ("---\ntype: insight\nstatus: active\nexomem_id: 11111111-1111-4111-8111-111111111111\n"
              f"title: Connection\n---\n# Connection\n\n- {relation}: [[Notes/Insights/target]]\n")
    after = before.replace("title: Connection\n", f"title: Connection\nprojects: [private-project]\nregistry_scope: {SCOPE}\n")
    with principal.request_scope(private_instances("full")):
        for scope in ("public", SCOPE):
            spec = instances.select(vault, relation_registry.SPEC, scope)
            spec.overlay(vault).parent.mkdir(parents=True, exist_ok=True)
            spec.overlay(vault).write_text("schema_version: 1\nextensions:\n  venue.near:\n"
                                          "    parent: relates_to\n    description: Nearby places\n")
        result = vocabulary_effects.classify_additive_effects(
            vault, (vocabulary_effects.CanonicalWriteImage(path, before.encode(), after.encode()),),
            resolver_entries=((target, "Target"),),
            identity_reader=lambda current: memory_refs.memory_ref("22222222-2222-4222-8222-222222222222"),
        )
    assert any(reason.code == "edge_removed_or_changed" for reason in result.reasons) == changed
    assert bool(result.effects) == changed


@pytest.mark.parametrize("operation", ["save-entity-types", "save-relations"])
def test_legacy_queued_registry_request_applies_to_selected_public_storage(private_instances, vault, tmp_path, operation):
    """A legacy request retains its original public writer and review schema."""
    from exomem import commands, relation_registry, vocabulary_review, writer_lease
    from exomem.governance import principal
    from exomem.vocabulary import contract, instances

    if operation == "save-entity-types":
        base = entity_types.SPEC
        canonical = "venue"
        definition = {"folder": "Venues", "label": "Venue", "aliases": [], "capture_guidance": "Recurring places."}
        document = {"schema_version": 1, "entity_types": {canonical: definition}}
    else:
        base = relation_registry.SPEC
        canonical = "venue.near"
        definition = {"parent": "relates_to", "description": "Nearby places"}
        document = {"upsert": {canonical: definition}}
    with principal.request_scope(private_instances("full")):
        spec = instances.select(vault, base)
        queued = contract.queue_for_owner(vault, base, registry.load(spec, vault),
                                         document, "Recurring places", "audience_restricted", operation=operation)
        item = vocabulary_review.context(vault, ref=queued["item_ref"])["item"]
        vocabulary_review.decide(vault, ref=item["ref"], decision={
            "item_ref": item["ref"], "fingerprint": item["fingerprint"], "family": item["family"],
            "registry_hashes": item["registry_hashes"], "target_versions": item["target_versions"],
            "outcome": "propose-new", "choice": {"canonical": canonical, "definition": definition},
            "rationale": "A recurring place needs a durable type.",
        })
        manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "legacy-leases"))
        command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "schema_memory")
        result = manager.invoke(command, (vault,), {"operation": operation, "subject": base.name,
                                "proposal": document, "expected_hash": next(iter(item["target_versions"].values())),
                                "why": "Recurring places", "vocabulary_ref": item["ref"],
                                "vocabulary_fingerprint": item["fingerprint"]},
                                idempotency_key="legacy-selected-public", read_only=False)
        assert result["state"] == "committed", result
        assert spec.overlay(vault).exists()
        assert not base.overlay(vault).exists()


def test_authenticated_private_promotion_cannot_change_public_observations(private_instances, vault):
    from exomem import commands
    from exomem.governance import egress, principal

    limited, full = private_instances("limited"), private_instances("full")
    proposal = {"upsert": {"venue": {
        "label": "Venue", "guidance": "A place for recurring meetings.", "parent": "concept",
        "aliases": ["meeting-place"], "attributes": {"folder": "Venues"},
    }}}
    with principal.request_scope(limited):
        before = commands.op_schema_memory(vault, "inspect", subject="entity-types")
        proposed_before = commands.op_schema_memory(vault, "propose", subject="entity-types", proposal=proposal)
    shared_log = (vault / "Knowledge Base/log.md").read_bytes()
    with principal.request_scope(full):
        private = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", registry_scope=SCOPE, proposal=proposal,
            expected_hash=private["content_hash"], why="Private recurring places",
        )
        assert saved["valid"]
        assert entity_types.load_entity_types(vault, registry_scope=SCOPE).resolve("meeting-place").folder == "Venues"
    assert (vault / "Knowledge Base/log.md").read_bytes() == shared_log
    with principal.request_scope(limited):
        assert commands.op_schema_memory(vault, "inspect", subject="entity-types") == before
        assert commands.op_schema_memory(vault, "propose", subject="entity-types", proposal=proposal) == proposed_before
        for path in (saved["saved"]["path"], saved["saved"]["history"]["snapshot"]):
            assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_NONE
        history_note = Path(saved["saved"]["history"]["snapshot"]).parent / "notes.md"
        (vault / history_note).write_text("# Private history explanation\n", encoding="utf-8")
        assert not egress.quick_page_visible(vault, history_note.as_posix(), principal=limited)
        assert commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)["available"] is False
        public_saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", proposal=proposal,
            expected_hash=before["content_hash"], why="Public recurring places",
        )
        assert public_saved["valid"]
    with principal.request_scope(full):
        private_after = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        assert private_after["content_hash"] == saved["saved"]["content_hash"]
        history = commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)
        assert history["versions"][0]["why"] == "Private recurring places"
        restored = commands.op_schema_memory(
            vault, "restore", subject="entity-types", registry_scope=SCOPE,
            version=history["versions"][0]["version"], expected_hash=private_after["content_hash"],
            why="Retain only the private core",
        )
        assert restored["valid"]
        assert entity_types.load_entity_types(vault, registry_scope=SCOPE).resolve("venue") is None
        assert entity_types.load_entity_types(vault).resolve("venue") is not None
    assert b"Private recurring places" not in (vault / "Knowledge Base/log.md").read_bytes()
    assert b"Retain only the private core" not in (vault / "Knowledge Base/log.md").read_bytes()
    assert b"Public recurring places" in (vault / "Knowledge Base/log.md").read_bytes()
    assert shared_log != (vault / "Knowledge Base/log.md").read_bytes()


def test_prospective_selector_never_overrides_canonical_destination(private_instances, vault):
    from exomem.governance import principal
    from exomem.vocabulary import instances

    with principal.request_scope(private_instances("full")):
        with pytest.raises(registry.RegistryError, match="binding is ambiguous"):
            instances.page_scope(vault, "", {"type": "entity"}, prospective=True)
        assert instances.page_scope(
            vault, "", {"type": "entity"}, registry_scope="public", prospective=True
        ) is None
        assert instances.page_scope(
            vault, "", {"type": "entity", "project": "private-project"}, prospective=True
        ) == SCOPE
        with pytest.raises(registry.RegistryError, match="binding is ambiguous"):
            instances.page_scope(
                vault, "Knowledge Base/Entities/Venues/private.md",
                {"type": "entity", "project": "private-project"}, registry_scope="public",
            )


def test_entity_creation_selects_instance_before_type_derived_folder(private_instances, vault):
    from exomem import commands
    from exomem.governance import principal

    with principal.request_scope(private_instances("full")):
        for selector, folder in (("public", "Public Venues"), (SCOPE, "Private Venues")):
            inspected = commands.op_schema_memory(
                vault, "inspect", subject="entity-types", registry_scope=selector
            )
            saved = commands.op_schema_memory(
                vault, "save", subject="entity-types", registry_scope=selector,
                expected_hash=inspected["content_hash"], why="Keep instance-specific venue destinations",
                proposal={"upsert": {"venue": {
                    "label": "Venue", "guidance": "A stable meeting place.", "parent": "concept",
                    "aliases": ["meeting-place"],
                    "attributes": {"folder": folder, "optional_frontmatter": ["project"]},
                }}},
            )
            assert saved["valid"]
        with pytest.raises(ValueError, match="REGISTRY_UNAVAILABLE"):
            commands.op_connect_memory(
                vault, "create-entity", entity_type="meeting-place", name="Unselected venue",
                summary="A meeting place with no selected instance.",
            )
        public = commands.op_connect_memory(
            vault, "create-entity", entity_type="meeting-place", name="Public venue",
            summary="A place for public meetings.", registry_scope="public",
        )
        private = commands.op_connect_memory(
            vault, "create-entity", entity_type="meeting-place", name="Private venue",
            summary="A place for private meetings.", project="private-project", registry_scope=SCOPE,
        )
    assert Path(public["path"]).parent.name == "Public Venues"
    assert Path(private["path"]).parent.name == "Private Venues"


def test_legacy_armed_registry_requires_explicit_assignment(configured_boundary, vault):
    from exomem import commands, state_migration
    from exomem.governance import principal

    _, authenticate = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="legacy instance assignment fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        result = commands.op_schema_memory(vault, "inspect", subject="entity-types")
    assert result == {"subject": "entity-types", "available": False, "reason": "registry_assignment_required"}


def test_explicit_public_history_does_not_override_an_unsafe_companion(private_instances, vault):
    from exomem.governance import egress, principal

    path = "Knowledge Base/_Schema/history/public/entity-types/retained.yaml"
    history = vault / path
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("schema_version: 1\n", encoding="utf-8")
    with principal.request_scope(private_instances("limited")):
        assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_FULL
        companion = vault / f"{path}.md"
        companion.symlink_to("missing-companion.md")
        assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_NONE


def test_arming_requires_existing_history_assignment(private_instances, vault):
    from exomem import state_migration
    from exomem.governance import connector_boundary

    legacy = registry_history.history_dir(vault, entity_types.SPEC.stem)
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "retained.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    authority = state_migration.assert_offline_migration_authority(source="existing registry history fixture")
    with pytest.raises(registry.RegistryError, match="legacy history needs explicit assignment"):
        state_migration.arm_connector_boundary_offline(vault, authority=authority)
    assert connector_boundary.read_requirement(vault)["version"] == 2


def test_colliding_extensions_keep_their_own_cached_meaning(vault: Path) -> None:
    """A public promotion cannot redirect a private type's alias or folder."""
    public_path = entity_types.SPEC.overlay(vault)
    private_path = public_path.with_name("private-types.yaml")
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(
        "schema_version: 1\nentity_types:\n  venue:\n    parent: concept\n    label: Venue\n    capture_guidance: A place.\n"
        "    folder: Public Places\n    aliases: [meeting-place]\n", encoding="utf-8",
    )
    private_path.write_text(
        "schema_version: 1\nentity_types:\n  venue:\n    parent: concept\n    label: Venue\n    capture_guidance: A place.\n"
        "    folder: Private Places\n    aliases: [meeting-place]\n", encoding="utf-8",
    )
    private = replace(
        entity_types.SPEC, instance_id="private-scope", stem="private-types",
        overlay=lambda root: root / private_path.relative_to(vault),
    )
    public = registry.load(entity_types.SPEC, vault)
    hidden = registry.load(private, vault)
    assert public.typed.resolve("meeting-place").folder == "Public Places"
    assert hidden.typed.resolve("meeting-place").folder == "Private Places"
    public_path.write_text(
        public_path.read_text().replace("Public Places", "Public Venues"), encoding="utf-8"
    )
    registry.invalidate(vault, path=public_path)
    assert registry.load(entity_types.SPEC, vault).typed.resolve("venue").folder == "Public Venues"
    unchanged = registry.load(private, vault)
    assert unchanged.typed.resolve("venue").folder == "Private Places"
    assert unchanged.content_hash == hidden.content_hash
    assert unchanged.effective_digest == hidden.effective_digest


def test_private_history_preserves_reason_without_touching_shared_logs(vault: Path) -> None:
    """A private save must succeed without consuming or rotating the public log."""
    shared_log = vault / "Knowledge Base/log.md"
    # An unreadable shared log cannot become a dependency of private history.
    shared_log.write_bytes(b"\xff public log awaiting repair\n")
    previous = "schema_version: 1\n"
    rendered = "schema_version: 1\nentries: {}\n"
    overlay = vault / "Knowledge Base/_Schema/private-example.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(previous, encoding="utf-8")
    reason = "Private definition\nRetain the full reason."

    with library_scope():
        saved = registry_history.commit(
            vault,
            path=overlay,
            stem="private-example",
            rendered=rendered,
            operation="save",
            why=reason,
            before_hash=content_hash(previous),
            after_hash=content_hash(rendered),
            previous=previous,
            private=True,
        )
        history = registry_history.history_view(vault, stem="private-example")
        restored = registry_history.read_version(
            vault, stem="private-example", version=saved["version"]
        )

    assert overlay.read_text(encoding="utf-8") == rendered
    assert restored == previous
    assert history["versions"][0]["why"] == reason
    assert history["versions"][0]["before_hash"] == content_hash(previous)
    assert history["versions"][0]["after_hash"] == content_hash(rendered)
    assert shared_log.read_bytes() == b"\xff public log awaiting repair\n"


def test_private_heading_read_uses_its_instance_without_publishing_that_meaning(private_instances, vault):
    from exomem import commands, epistemic_graph, freshness, semantic_contract, semantic_index
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    path = "Knowledge Base/Notes/Insights/private-interpretation.md"
    source = (
        "---\ntype: insight\nproject: private-project\n"
        "exomem_id: 44444444-4444-4444-8444-444444444444\n---\n"
        "# Private interpretation\n\n## Container\n\n### Protocol\n"
        "- id: local-protocol\n\nPrivate procedure with a stable address.\n"
    )
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(source)
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        assert semantic_index.build_parent_index_state(vault, path).document.units == ()
        selected = semantic_contract.build_page_state(vault, path, source)
        assert [unit.kind for unit in selected.document.units] == ["protocol"]
        assert semantic_index.from_semantic_page_state(selected).document.units == ()
        assert all(freshness.rebaseline(vault).values())
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        context = commands.op_graph_context(vault, path=path, depth=1)
        assert any(node["kind"] == "protocol" for node in context["nodes"]), context
        unit_ref = selected.document.units[0].unit_ref
        exact = commands.op_graph_context(vault, unit_ref=unit_ref, depth=0)
        assert exact["unit_status"] == "found"
        assert exact["seeds"][0]["metadata"]["unit_ref"] == unit_ref
    with principal.request_scope(private_instances("limited")):
        assert commands.op_graph_context(vault, path=path)["nodes"] == []


def test_a_public_unit_filter_never_names_a_private_extension(private_instances, vault):
    """A private kind spelled like a public filter value is another entry."""
    from exomem import find as find_module
    from exomem import freshness, lexstore
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    path = "Knowledge Base/Notes/Insights/private-filter.md"
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(
        "---\ntype: insight\nproject: private-project\n"
        "exomem_id: 55555555-5555-4555-8555-555555555555\n---\n"
        "# Private filter\n\n## Container\n\n### Protocol\n"
        "- id: local-protocol\n\nPrivate procedure with a stable address.\n"
    )
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        assert all(freshness.rebaseline(vault).values())
        lexstore.ensure_fresh(vault)

        def kinds(filters):
            hits = find_module.find(
                vault, query="private procedure", scope="kb-only", mode="keyword",
                graph=False, result_level="unit", filters=filters, limit=10,
            )
            return [hit.kind for hit in hits]

        # Find is a public operation: its `protocol` names the public entry.
        assert kinds({"unit.kind": {"$eq": "protocol"}}) == []
        assert kinds({"unit.kind": {"$ne": "protocol"}}) == ["protocol"]


def test_vector_candidates_take_their_selected_meaning_before_the_result_limit(private_instances, vault):
    """Hidden or unrecognized occurrences spend no slot; unpublished coverage is never a miss."""
    import numpy as np

    from exomem import embedding_index, semantic_index
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    heading = "## Container\n\n### Protocol\n- id: local-protocol\n\nShared procedure text.\n"
    pages = {
        "private": ("Knowledge Base/Notes/Insights/vector-private.md", "project: private-project\n", heading),
        "public": ("Knowledge Base/Notes/Insights/vector-public.md", "", "- [decision] Public procedure.\n\n" + heading),
        "unpublished": ("Knowledge Base/Notes/Insights/vector-unpublished.md", "", "Plain prose only.\n"),
    }
    for number, (path, extra, body) in enumerate(pages.values(), start=6):
        (vault / path).parent.mkdir(parents=True, exist_ok=True)
        (vault / path).write_text(
            f"---\ntype: insight\n{extra}exomem_id: {number}6666666-6666-4666-8666-666666666666\n---\n"
            f"# Vector {number}\n\n{body}"
        )
    index = embedding_index.EmbeddingIndex(vault)
    query = np.eye(index.dim, dtype=np.float32)[0]
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        for name in ("private", "public"):
            state = semantic_index.build_parent_index_state(vault, pages[name][0])
            # Every protocol heading is nearer the query than the public decision.
            vectors = np.asarray([
                query if occurrence.kind_raw == "Protocol" else np.eye(index.dim, dtype=np.float32)[1] + 0.5 * query
                for occurrence in state.occurrences
            ], dtype=np.float32)
            index.upsert_semantic_units(state, vectors, 0.0)

        def search(allowed):
            incomplete: list[str] = []
            hits = index.search_semantic_units(
                query, 1, allowed_parent_paths=allowed, validate=False, incomplete_out=incomplete
            )
            return [(hit.parent_path, hit.unit_ref) for hit in hits], incomplete

        published = {pages["private"][0], pages["public"][0]}
        private_unit = semantic_index.selected_parent_index_state(vault, pages["private"][0]).document.units[0]
        assert private_unit.kind == "protocol"
        assert search(published) == ([(pages["private"][0], private_unit.unit_ref)], [])
        public_units = semantic_index.selected_parent_index_state(vault, pages["public"][0]).document.units
        assert [unit.category for unit in public_units] == ["decision"]

    with principal.request_scope(private_instances("limited")):
        # The hidden private page and the public page's unrecognized heading outrank
        # the decision, yet neither takes the single slot.
        assert search(published) == ([(pages["public"][0], public_units[0].unit_ref)], [])
        allowed = published | {pages["unpublished"][0]}
        assert search(allowed)[1] == [pages["unpublished"][0]]
        unpublished = semantic_index.build_parent_index_state(vault, pages["unpublished"][0])
        assert unpublished.occurrences == ()
        index.upsert_semantic_units(unpublished, np.zeros((0, index.dim), dtype=np.float32), 0.0)
        assert search(allowed) == ([(pages["public"][0], public_units[0].unit_ref)], [])


STORAGE_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAX"


@pytest.fixture
def storage_scope(configured_boundary, vault):
    """A Scope over the private instance's storage that the host may deny later.

    It exists before arming, so a later denial is a live configuration change.
    """
    config, _ = configured_boundary
    (vault / "Knowledge Base/_Governance/scopes/vocabulary-storage.yaml").write_text(
        f"governance_version: 1\nid: {STORAGE_SCOPE}\n"
        'paths: ["_Schema/private/**", "_Schema/history/private/**"]\n', encoding="utf-8",
    )
    data = json.loads(config.read_text())
    data["default_denied_scope_ids"].append(STORAGE_SCOPE)
    config.write_text(json.dumps(data))
    return STORAGE_SCOPE


def test_a_live_revocation_withholds_warm_private_units_but_keeps_the_page_readable(
    configured_boundary, storage_scope, private_instances, vault
):
    """Warm private meaning never outlives the caller's admission; the raw page stays useful.

    The host denies the full client the Scope over the private instance's storage.
    The private page stays readable, but its private definitions are withheld.
    This catches a warm projection that keeps serving the private interpretation,
    and a refusal that withholds the readable page along with its definitions.
    """
    from exomem import commands, epistemic_graph, freshness, lexstore, semantic_contract
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    path = "Knowledge Base/Notes/Insights/revocable-interpretation.md"
    source = (
        "---\ntype: insight\nproject: private-project\n"
        "exomem_id: 77777777-7777-4777-8777-777777777777\n---\n"
        "# Revocable interpretation\n\n- [decision] Ship the shared rollout.\n\n"
        "## Container\n\n### Protocol\n- id: local-protocol\n\nPrivate procedure with a stable address.\n"
    )
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(source)
    full = private_instances("full")

    def observe():
        with principal.request_scope(full):
            found = commands.op_find(
                vault, query="private procedure", mode="keyword", graph=False, result_level="unit", limit=10,
            )
            # A degraded answer arrives as an envelope around the same hits.
            hits = found["hits"] if isinstance(found, dict) else found
            return {
                "page": commands.op_read_memory(vault, path=path),
                "protocol": commands.op_read_memory(vault, path=path, unit_ref=units["protocol"]),
                "decision": commands.op_read_memory(vault, path=path, unit_ref=units["decision"]),
                "graph": commands.op_graph_context(vault, path=path, depth=1),
                "kinds": sorted(str(hit.get("kind")) for hit in hits),
                "degraded": found.get("degraded", []) if isinstance(found, dict) else [],
            }

    with principal.request_scope(full):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        selected = semantic_contract.build_page_state(vault, path, source)
        units = {
            "protocol": next(unit.unit_ref for unit in selected.document.units if unit.kind == "protocol"),
            "decision": next(unit.unit_ref for unit in selected.document.units if unit.category == "decision"),
        }
        assert all(freshness.rebaseline(vault).values())
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        lexstore.ensure_fresh(vault)
    warm = observe()
    assert warm["protocol"]["status"] == "found"
    assert any(node["kind"] == "protocol" for node in warm["graph"]["nodes"])
    assert "protocol" in warm["kinds"]

    config, _ = configured_boundary
    data = json.loads(config.read_text())
    data["clients"][1]["denied_scope_ids"] = [storage_scope]
    config.write_text(json.dumps(data))
    revoked = observe()

    assert "Private procedure with a stable address." in revoked["page"]["body"]
    assert revoked["decision"]["status"] == "found"
    assert revoked["protocol"]["status"] == "unavailable"
    assert not any(node["kind"] == "protocol" for node in revoked["graph"]["nodes"])
    assert "protocol" not in revoked["kinds"]
    # The withheld units make the lexical unit answer incomplete, never a proved miss.
    assert "semantic_units_lexical" in revoked["degraded"]


def test_a_hidden_page_with_a_private_status_never_changes_a_limited_clients_find_consumers(
    configured_boundary, vault, tmp_path, monkeypatch
):
    """Evolution, context and link suggestions rank only what their caller may see.

    Both vaults carry the same private status definition; only the second has a
    hidden page that uses it. Unadmitted, the hidden page's status refused the
    limited client's evolution and context and emptied its suggestions.
    """
    import shutil

    from exomem import commands, freshness, lexstore, state_migration
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    config, authenticate = configured_boundary
    data = json.loads(config.read_text())
    data["vocabulary"] = {
        "public": {"namespace": "Knowledge Base/_Schema/public", "history": "public", "overrides": {}},
        "private": {SCOPE: {"namespace": "Knowledge Base/_Schema/private", "history": "private"}},
        "destinations": {}, "selections": {},
    }
    data["capture_paths"] += ["Knowledge Base/_Schema/public", "Knowledge Base/_Schema/history/public"]
    config.write_text(json.dumps(data))
    public = "Knowledge Base/Notes/public.md"
    (vault / public).write_text(
        "---\nproject: public-project\ntype: insight\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n# Public\n\n"
        "- [decision] public information decision.\n\npublic information\n", encoding="utf-8")
    twin = tmp_path / "twin"
    shutil.copytree(vault, twin)
    (twin / "Knowledge Base/Notes/private.md").write_text(
        "---\ntype: insight\nproject: private-project\nstatus: secret-lifecycle\n"
        "exomem_id: 99999999-9999-4999-8999-999999999999\n---\n# Hidden\n\n"
        "- [decision] public information decision from the hidden page.\n\n"
        "Links [[Notes/public]].\n" + "public information\n" * 20, encoding="utf-8")
    observed = []
    for root in (vault, twin):
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        stop = state_migration.assert_offline_migration_authority(source="hidden status twin")
        state_migration.arm_connector_boundary_offline(root, authority=stop)
        with principal.request_scope(authenticate("full")):
            overlay = instances.select(root, registry_spec("statuses"), SCOPE).overlay(root)
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text("schema_version: 1\nentries:\n  secret-lifecycle:\n    attributes: {class: live}\n")
            freshness.rebaseline(root)
            lexstore.ensure_fresh(root)
        with principal.request_scope(authenticate("limited")):
            context = commands.op_connect_memory(root, operation="context", query="public information")
            observed.append({
                "evolution": commands.op_evolution(root, query="public information"),
                "review": commands.op_review_memory(root, mode="evolution", query="public information"),
                "context": {key: context[key] for key in ("available", "claims")},
                "suggestions": commands.op_connect_memory(
                    root, operation="suggest-links",
                    draft_title="public information", draft_body="public information decision",
                ),
            })

    assert observed[0] == observed[1]
    assert [item["path"] for item in observed[1]["suggestions"]] == [public]
    assert list(observed[1]["context"]["claims"]) == [public]


def test_a_limited_client_validates_a_planning_value_from_the_bound_public_overlay(private_instances, vault):
    """Planning admission checks the overlay the bound public instance reads.

    It used to admit the unbound default path, which a limited client cannot read,
    and so refused a value the public instance defines.
    """
    from exomem import planning, planning_values
    from exomem.governance import principal
    from exomem.vocabulary import instances

    overlay = instances.select(vault, planning_values.SPEC).overlay(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries:\n  status.parked:\n    attributes: {class: open}\n")
    with principal.request_scope(private_instances("limited")):
        item = planning.normalize_item({"title": "Later", "status": "parked"}, vault_root=vault)
    assert item["status"] == "parked"


def test_a_legacy_armed_vault_refuses_an_unassigned_planning_value_and_keeps_shipped_ones(
    configured_boundary, vault
):
    """Version 1 assigns no instance, so the legacy overlay's values are unavailable.

    The selection error used to escape the Planning validator instead of its refusal.
    """
    from exomem import planning, planning_values, state_migration
    from exomem.governance import principal
    from exomem.structured_collections import CollectionError

    _, authenticate = configured_boundary
    overlay = planning_values.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries:\n  status.parked:\n    attributes: {class: open}\n")
    authority = state_migration.assert_offline_migration_authority(source="legacy planning values fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        shipped = planning.normalize_item({"title": "Now", "status": "candidate"}, vault_root=vault)
        with pytest.raises(CollectionError) as refused:
            planning.normalize_item({"title": "Later", "status": "parked"}, vault_root=vault)
    assert shipped["status"] == "candidate"
    assert refused.value.code == "PLANNING_VALUES_UNAVAILABLE"


MEETING_TYPE_OVERLAY = (
    "schema_version: 1\nentries:\n  meeting-note:\n"
    "    attributes: {role: compiled, folder: Notes/Meetings}\n"
)


def _blocking_codes(vault, path, page_type, *, overwrite=False):
    """What the write gate blocks for a draft page with no semantic unit."""
    from exomem import commands

    validation = commands.op_manage_memory_file(
        vault, operation="create", path=path, content="# Sync\n\nThe crew met.\n",
        frontmatter={"type": page_type, "status": "active"}, overwrite=overwrite, validate_only=True,
    )
    return {finding["code"] for finding in validation["contract_result"]["blocking_findings"]}


def test_a_limited_client_classifies_a_note_type_from_the_bound_public_overlay(private_instances, vault):
    """Note-type admission checks the overlay the bound public instance reads.

    It used to admit the unbound default path, which a limited client cannot read,
    and so judged a type that the public instance defines as no type at all.
    """
    from exomem import note_types
    from exomem.governance import principal
    from exomem.vocabulary import instances

    overlay = instances.select(vault, note_types.SPEC).overlay(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(MEETING_TYPE_OVERLAY, encoding="utf-8")
    # A limited client creates only in capture paths, so it edits an existing page.
    page = "Knowledge Base/Notes/Meetings/2026-10-sync.md"
    (vault / page).parent.mkdir(parents=True, exist_ok=True)
    (vault / page).write_text("---\ntype: meeting-note\n---\n# Sync\n", encoding="utf-8")
    with principal.request_scope(private_instances("limited")):
        codes = _blocking_codes(vault, page, "meeting-note", overwrite=True)
    # The bound type is compiled, so the write gate asks for a semantic unit.
    assert "missing_semantic_unit" in codes


def test_a_legacy_armed_vault_refuses_an_unassigned_note_type_and_keeps_shipped_ones(
    configured_boundary, vault
):
    """Version 1 assigns no instance, so the legacy overlay's types are unavailable.

    The selection error used to escape the write gate instead of its refusal.
    """
    from exomem import note_types, state_migration
    from exomem.cli_ops import OpError
    from exomem.governance import principal

    _, authenticate = configured_boundary
    overlay = note_types.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(MEETING_TYPE_OVERLAY, encoding="utf-8")
    authority = state_migration.assert_offline_migration_authority(source="legacy note types fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        shipped = _blocking_codes(vault, "Knowledge Base/Notes/Insights/2026-10-sync.md", "insight")
        with pytest.raises(OpError) as refused:
            _blocking_codes(vault, "Knowledge Base/Notes/Meetings/2026-10-sync.md", "meeting-note")
    assert "missing_semantic_unit" in shipped
    assert refused.value.code == "NOTE_TYPE_DEFINITION_UNAVAILABLE"
    # The overlay itself is valid: the remedy is assignment, not its inspect findings.
    assert "arming" in refused.value.remediation


def test_a_legacy_armed_vault_reports_unassigned_registries_instead_of_failing_bootstrap(
    configured_boundary, vault
):
    """Version 1 assigns no instance, so legacy definitions make their registries unavailable.

    The selection error used to escape the bootstrap's entity, relation and
    source-taxonomy blocks and fail the whole bootstrap.
    """
    from exomem import commands, entity_types, relation_registry, source_taxonomy, state_migration
    from exomem.governance import principal

    _, authenticate = configured_boundary
    for spec in (entity_types.SPEC, relation_registry.SPEC, source_taxonomy.KIND_SPEC):
        spec.overlay(vault).parent.mkdir(parents=True, exist_ok=True)
        spec.overlay(vault).write_text("schema_version: 1\n", encoding="utf-8")
    authority = state_migration.assert_offline_migration_authority(source="legacy registries fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        bootstrap = commands.op_bootstrap(vault, profile="full")
        resolved = commands.op_schema_memory(
            vault, "resolve-entity-type", subject="entity-types", requested_type="person"
        )
    assert bootstrap["entity_registry"]["reason"] == "registry_assignment_required"
    assert bootstrap["source_taxonomy"]["reason"] == "registry_assignment_required"
    assert resolved == {
        "subject": "entity-types", "available": False, "reason": "registry_assignment_required",
    }


def test_a_limited_client_reads_entity_types_from_the_bound_public_overlay(private_instances, vault):
    """Bootstrap and schema_memory admit the overlay the bound public instance reads.

    They used to admit the unbound default path, which a limited client cannot
    read, and so withheld a type that the public instance defines.
    """
    from exomem import commands
    from exomem.governance import principal

    proposal = {"upsert": {"venue": {
        "label": "Venue", "guidance": "A place for recurring meetings.", "parent": "concept",
        "attributes": {"folder": "Venues"},
    }}}
    with principal.request_scope(private_instances("full")):
        inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types")
        saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", proposal=proposal,
            expected_hash=inspected["content_hash"], why="Public recurring places",
        )
    assert saved["valid"]
    with principal.request_scope(private_instances("limited")):
        bootstrap = commands.op_bootstrap(vault, profile="full")
        resolved = commands.op_schema_memory(
            vault, "resolve-entity-type", subject="entity-types", requested_type="venue"
        )
    assert "venue" in [item["id"] for item in bootstrap["entity_registry"]["types"]]
    assert [match["id"] for match in resolved["exact_matches"]] == ["venue"]


def test_a_limited_client_resolves_and_observes_the_bound_public_registries(
    private_instances, vault
):
    """Relation and entity resolution, entity creation and review hashes admit the bound overlays.

    They used to admit the unbound default paths, which a limited client cannot
    read, and so refused registries that the public instance defines.
    """
    from exomem import commands, vocabulary_review
    from exomem.governance import principal

    full, limited = private_instances("full"), private_instances("limited")
    with principal.request_scope(full):
        for subject, proposal in (
            ("entity-types", {"upsert": {"venue": {
                "label": "Venue", "guidance": "A place for recurring meetings.",
                "parent": "concept", "attributes": {"folder": "Venues"},
            }}}),
            ("relations", {"upsert": {"vault.hosts": {
                "parent": "relates_to", "description": "A venue hosts an event.",
            }}}),
        ):
            inspected = commands.op_schema_memory(vault, "inspect", subject=subject)
            saved = commands.op_schema_memory(
                vault, "save", subject=subject, proposal=proposal,
                expected_hash=inspected["content_hash"], why="Public meeting places",
            )
            assert saved["valid"]
        relation_for_full = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="vault.hosts"
        )
        hashes_for_full = vocabulary_review.registry_hashes(vault)
    with principal.request_scope(limited):
        relation = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="vault.hosts"
        )
        created = commands.op_connect_memory(
            vault, "create-entity", entity_type="venue", name="Harbour Hall",
            summary="A hall by the harbour.", registry_scope="public",
        )
        entity = commands.op_connect_memory(
            vault, "resolve-entity", name="Harbour Hall", entity_type="venue"
        )
        hashes = vocabulary_review.registry_hashes(vault)
    assert relation == relation_for_full
    assert [match["canonical"] for match in relation["exact_matches"]] == ["vault.hosts"]
    assert entity["status"] == "match"
    assert entity["candidates"][0]["path"] == created["path"]
    assert hashes == hashes_for_full


def test_a_private_relation_label_resolves_only_for_a_caller_admitted_to_its_instance(
    private_instances, vault
):
    """A caller who cannot admit the private instance gets what an unregistered label gets."""
    from exomem import commands, relation_registry
    from exomem.governance import principal
    from exomem.vocabulary import instances

    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, relation_registry.SPEC, SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            "schema_version: 1\nextensions:\n  vault.has_pet:\n    parent: relates_to\n"
            "    description: A person keeps an animal.\n    aliases: [has_pet]\n",
            encoding="utf-8",
        )
        admitted = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="has_pet", registry_scope=SCOPE
        )
        without_scope = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="has_pet"
        )
    with principal.request_scope(private_instances("limited")):
        withheld = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="has_pet", registry_scope=SCOPE
        )
        unknown = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="has_pet",
            registry_scope="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        )
        unregistered = commands.op_connect_memory(
            vault, "resolve-relation", requested_relation="has_pet"
        )

    assert [match["canonical"] for match in admitted["exact_matches"]] == ["vault.has_pet"]
    assert without_scope["exact_matches"] == []
    assert withheld == unknown == unregistered
    assert "vault.has_pet" not in json.dumps(withheld)


def test_queue_selection_resolves_a_label_in_the_instance_of_the_source_page(
    private_instances, vault
):
    """A private page accepts its instance's label; a public page does not know it."""
    from exomem import relation_queue, relation_registry
    from exomem.governance import principal
    from exomem.vocabulary import instances

    notes = vault / "Knowledge Base/Notes"
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, relation_registry.SPEC, SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            "schema_version: 1\nextensions:\n  vault.has_pet:\n    parent: relates_to\n"
            "    description: A person keeps an animal.\n    aliases: [has_pet]\n",
            encoding="utf-8",
        )
        for name, front in (
            ("owner", f"project: private-project\nregistry_scope: {SCOPE}\n"),
            ("visitor", ""),
            ("pet", ""),
        ):
            (notes / f"{name}.md").write_text(
                f"---\ntype: insight\nstatus: active\n{front}---\n# {name}\n\nBody.\n",
                encoding="utf-8",
            )
        candidate = {
            "from": "Knowledge Base/Notes/owner.md",
            "to": "Knowledge Base/Notes/pet.md",
            "relation_type": "relates_to",
        }
        selected = relation_queue.selected_relation(vault, candidate, "has_pet")
        with pytest.raises(ValueError, match="INVALID_SELECTED_RELATION"):
            relation_queue.selected_relation(
                vault, {**candidate, "from": "Knowledge Base/Notes/visitor.md"}, "has_pet"
            )

    assert (selected["relation_type"], selected["authored_label"]) == ("vault.has_pet", "has_pet")


def test_bootstrap_vocabulary_describes_one_admitted_instance_and_hides_the_rest(
    private_instances, vault
):
    """A scope the caller cannot admit reads the same whether it is withheld or absent."""
    from exomem.governance import principal
    from exomem.vocabulary import bootstrap, instances

    absent = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    with principal.request_scope(private_instances("full")):
        spec = instances.select(vault, entity_types.SPEC, SCOPE)
        spec.overlay(vault).parent.mkdir(parents=True, exist_ok=True)
        spec.overlay(vault).write_text("schema_version: 1\nentity_types: {}\n", encoding="utf-8")
        private = bootstrap.block(vault, inspect_route="schema_memory", registry_scope=SCOPE)
        public = bootstrap.block(vault, inspect_route="schema_memory")
    with principal.request_scope(private_instances("limited")):
        withheld = bootstrap.block(vault, inspect_route="schema_memory", registry_scope=SCOPE)
        unknown = bootstrap.block(vault, inspect_route="schema_memory", registry_scope=absent)

    assert "top" in private["entity-types"] and "top" in public["entity-types"]
    assert withheld == unknown
    assert withheld["entity-types"] == {"unavailable": "audience_restricted"}


def test_curation_creates_an_entity_in_its_private_instance_and_binds_that_instance(
    private_instances, vault
):
    """A create-entity step carries `aliases` and `registry_scope`; a registry move blocks it."""
    from exomem import commands, curation
    from exomem.governance import principal

    step = {"step_id": "venue", "kind": "create-entity", "args": {
        "entity_type": "meeting-place", "name": "Private venue",
        "summary": "A hall for private meetings.",
        "project": "private-project", "registry_scope": SCOPE, "aliases": ["the private hall"],
    }}
    plan = {"version": 1, "title": "Private venue", "steps": [step]}
    with principal.request_scope(private_instances("full")):
        inspected = commands.op_schema_memory(
            vault, "inspect", subject="entity-types", registry_scope=SCOPE
        )
        commands.op_schema_memory(
            vault, "save", subject="entity-types", registry_scope=SCOPE,
            expected_hash=inspected["content_hash"], why="Private venue destinations",
            proposal={"upsert": {"venue": {
                "label": "Venue", "guidance": "A stable meeting place.", "parent": "concept",
                "aliases": ["meeting-place"],
                "attributes": {"folder": "Private Venues", "optional_frontmatter": ["project"]},
            }}},
        )
        proposed = curation.propose(vault, plan)
        moved = commands.op_schema_memory(
            vault, "inspect", subject="entity-types", registry_scope=SCOPE
        )
        commands.op_schema_memory(
            vault, "save", subject="entity-types", registry_scope=SCOPE,
            expected_hash=moved["content_hash"], why="A private registry move after sealing",
            proposal={"upsert": {"hall": {
                "label": "Hall", "guidance": "A large room.", "parent": "concept",
                "attributes": {"folder": "Halls"},
            }}},
        )
        with pytest.raises(curation.CurationError) as refused:
            curation.apply(
                vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"],
                expected_plan_fingerprint=proposed["plan_fingerprint"], why="Approved exact plan.",
            )
        assert refused.value.code == "CURATION_REGISTRY_CHANGED"
        fresh = curation.propose(vault, plan)
        applied = curation.apply(
            vault, run_id=fresh["run_id"], plan_id=fresh["plan_id"],
            expected_plan_fingerprint=fresh["plan_fingerprint"], why="Approved exact plan.",
        )
    created = (vault / applied["step"]["path"]).read_text(encoding="utf-8")
    assert Path(applied["step"]["path"]).parent.name == "Private Venues"
    assert "the private hall" in created
    with principal.request_scope(private_instances("limited")):
        with pytest.raises(ValueError, match="REGISTRY_UNAVAILABLE"):
            curation.propose(vault, plan)


def test_registry_saves_and_bootstrap_name_the_bound_public_overlay(private_instances, vault):
    """Each response names the overlay that the bound public instance reads and writes.

    They used to name the unbound default path while the bytes went to the bound
    path, and the relation save gated a limited client on that default path.
    """
    from exomem import commands, relation_registry
    from exomem.governance import principal

    venue = {"schema_version": 1, "entity_types": {"venue": {
        "parent": "concept", "folder": "Venues", "label": "Venue", "aliases": [],
        "capture_guidance": "A place for recurring meetings.", "status": "active",
    }}}
    with principal.request_scope(private_instances("full")):
        entities = commands.op_schema_memory(
            vault, operation="save-entity-types", proposal=venue, why="Public meeting places",
        )
    with principal.request_scope(private_instances("limited")):
        relations = commands.op_schema_memory(
            vault, subject="relations", operation="save-relations", why="Public meeting places",
            expected_hash=relation_registry.load_registry(vault).extension_hash,
            proposal={"upsert": {"vault.hosts": {
                "parent": "relates_to", "description": "A venue hosts an event.",
            }}},
        )
        bootstrap = commands.op_bootstrap(vault, profile="full")
    assert entities["saved"]["path"] == "Knowledge Base/_Schema/public/entity-types.yaml"
    assert "venue" in (vault / entities["saved"]["path"]).read_text(encoding="utf-8")
    assert relations["saved"]["path"] == "Knowledge Base/_Schema/public/relation-registry.yaml"
    assert "vault.hosts" in (vault / relations["saved"]["path"]).read_text(encoding="utf-8")
    taxonomy_path = bootstrap["source_taxonomy"]["registry"]
    assert taxonomy_path == "Knowledge Base/_Schema/public/source-taxonomy.yaml"
