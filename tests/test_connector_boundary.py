"""Owner clients keep their identity while receiving different content ceilings."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp.server import dependencies

from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.governance import egress, policy, principal
from exomem.session_oauth import ExomemSessionOAuthProxy

ISSUER = "https://memory.example.test"
SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PUBLIC = "Knowledge Base/Notes/public.md"
PRIVATE = "Knowledge Base/Notes/private.md"


@pytest.fixture
def configured_boundary(vault, tmp_path, monkeypatch):
    scopes = vault / "Knowledge Base/_Governance/scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "private.yaml").write_text(
        f'governance_version: 1\nid: {SCOPE}\nprojects: [private-project]\n', encoding="utf-8",
    )
    for path, project, content in ((PUBLIC, "public-project", "public information"),
                                   (PRIVATE, "private-project", "protected canary")):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\nproject: {project}\n---\n{content}\n", encoding="utf-8")
    # Host maintenance establishes the existing immutable global activation
    # manifest before a limited connector can perform ordinary edits.
    from exomem.activation_manifest import ensure_manifest

    with principal.request_scope(principal.owner_principal(surface="library")):
        ensure_manifest(vault)
    config = tmp_path / "host-boundary.json"
    config.write_text(json.dumps({
        "version": 1, "default_denied_scope_ids": [SCOPE],
        "capture_paths": ["Knowledge Base/Capture"],
        "clients": [{"issuer": ISSUER, "client_id": client, "denied_scope_ids": denied}
                    for client, denied in (("limited", [SCOPE]), ("full", []))],
    }), encoding="utf-8")
    monkeypatch.setenv("EXOMEM_CONNECTOR_BOUNDARY_CONFIG", str(config))
    monkeypatch.setenv("EXOMEM_BASE_URL", ISSUER)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "4242")
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "github:4242")
    authority = SessionAuthority.local(directory=tmp_path / "sessions",
        signing_root="temporary-boundary-signing-root", issuer=ISSUER, audience=f"{ISSUER}/mcp")
    from exomem import server_auth

    monkeypatch.setattr(server_auth, "build_session_authority", lambda **kwargs: authority)

    def authenticate(client):
        async def load():
            bearer, _ = await authority.issue(client_id=client, scopes=["exomem:read", "exomem:write"],
                identity=SessionIdentity(github_user_id=4242, github_login="fixture-owner"))
            return await ExomemSessionOAuthProxy.load_access_token(
                SimpleNamespace(_session_authority=authority), bearer)
        token = asyncio.run(load())
        monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
        return principal.resolve_mcp_principal()

    return config, authenticate


def test_same_owner_clients_have_different_nonbypassable_content(configured_boundary, vault):
    _, authenticate = configured_boundary
    limited, full = authenticate("limited"), authenticate("full")
    assert limited.audience_id == full.audience_id == principal.OWNER_AUDIENCE
    for who in (limited, full):
        assert egress.quick_page_visible(vault, PUBLIC, principal=who)
    assert not egress.quick_page_visible(vault, PRIVATE, principal=limited)
    assert egress.quick_page_visible(vault, PRIVATE, principal=full)
    keep = egress.restricted_release_filter(vault, principal=limited)
    assert keep is not None and keep(PUBLIC) and not keep(PRIVATE)
    assert egress.owner_only_aggregate(vault, principal=limited) == {
        "available": False, "reason": "audience_restricted"}


@pytest.mark.parametrize("surface", ["rest", "transfer"])
def test_shared_and_legacy_owner_ingress_gets_restricted_default(configured_boundary, vault, surface):
    who = principal.owner_principal(surface=surface)
    assert egress.quick_page_visible(vault, PUBLIC, principal=who)
    assert not egress.quick_page_visible(vault, PRIVATE, principal=who)


def test_live_configuration_change_restricts_an_existing_filter(configured_boundary, vault):
    config, authenticate = configured_boundary
    who = authenticate("limited")
    keep = egress.release_walk_filter(vault, principal=who)
    assert keep(PUBLIC)
    data = json.loads(config.read_text())
    data["clients"][0]["denied_scope_ids"] = []
    config.write_text(json.dumps(data))
    assert keep(PRIVATE)
    data["clients"][0]["denied_scope_ids"] = [SCOPE]
    config.write_text(json.dumps(data))
    assert not keep(PRIVATE)


def test_unknown_scope_never_turns_configuration_into_unrestricted_access(configured_boundary, vault):
    config, authenticate = configured_boundary
    who = authenticate("full")
    data = json.loads(config.read_text())
    data["default_denied_scope_ids"].append("01ARZ3NDEKTSV4RRFFQ69G5FAW")
    config.write_text(json.dumps(data))
    assert not egress.quick_page_visible(vault, PRIVATE, principal=who)
    assert not egress.quick_page_visible(vault, PUBLIC, principal=who)
    assert not policy.load(vault).blocked


def test_limited_owner_edits_allowed_content_without_review_queue(configured_boundary, vault):
    from exomem.edit import EditError, edit

    _, authenticate = configured_boundary
    with principal.request_scope(authenticate("limited")):
        edit(vault, path=PUBLIC, why="correct allowed note", old_string="public information",
             new_string="corrected public information")
        with pytest.raises(EditError) as hidden:
            edit(vault, path=PRIVATE, why="attempt private edit", new_body="replacement")
    assert "corrected public information" in (vault / PUBLIC).read_text()
    assert hidden.value.code == "NOT_FOUND"
    assert "protected canary" in (vault / PRIVATE).read_text()


def test_proposed_membership_cannot_move_allowed_content_under_the_denied_scope(configured_boundary, vault):
    from exomem import vault as vault_module

    _, authenticate = configured_boundary
    original = (vault / PUBLIC).read_text()
    with principal.request_scope(authenticate("limited")):
        with pytest.raises(ValueError, match="WRITE_REFUSED"):
            vault_module.batch_atomic_write([
                vault_module.PlannedWrite(vault / PUBLIC, original.replace("public-project", "private-project")),
            ], vault_root=vault)
    assert (vault / PUBLIC).read_text() == original


def test_global_policy_operations_are_unavailable_before_private_observations(configured_boundary, vault):
    from exomem import reserved_paths
    from exomem.governance import tool

    _, authenticate = configured_boundary
    with principal.request_scope(authenticate("limited")), reserved_paths._owner_authority_scope("govern_memory"):
        for operation, arguments in (("list", {}), ("propose", {"documents": {"scopes/change.yaml": "invalid"}})):
            with pytest.raises(tool.GovernanceError) as result:
                tool.op_govern_memory(vault, operation, **arguments)
            assert result.value.code == "GOVERNANCE_OPERATION_UNAVAILABLE"


def test_unknown_and_hosted_clients_cannot_borrow_configured_owner_access(configured_boundary, vault):
    _, authenticate = configured_boundary
    callers = (authenticate("re-registered-client"), principal.resolve_hosted_principal("tenant-owner"),
               principal.RequestPrincipal("owner", surface="transfer"))
    for who in callers:
        assert egress.quick_page_visible(vault, PUBLIC, principal=who)
        assert not egress.quick_page_visible(vault, PRIVATE, principal=who)


def test_explicit_local_administration_does_not_travel_in_a_download_capability(configured_boundary, vault):
    from exomem import upload_tokens

    for surface in ("cli", "mcp", "library"):
        who = principal.owner_principal(surface=surface)
        assert egress.quick_page_visible(vault, PRIVATE, principal=who)
        signed = upload_tokens.mint_principal("temporary-host-signing-root", who)
        delegated = upload_tokens.bound_principal(signed, "temporary-host-signing-root")
        assert delegated.audience_id == who.audience_id
        assert not egress.quick_page_visible(vault, PRIVATE, principal=delegated)


@pytest.mark.parametrize("invalid", ["duplicate-key", "unknown-field", "empty-default", "wider-default"])
def test_malformed_host_configuration_is_an_unavailable_boundary(configured_boundary, vault, invalid):
    config, authenticate = configured_boundary
    who = authenticate("full")
    data = json.loads(config.read_text())
    if invalid == "duplicate-key":
        config.write_text(config.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    else:
        if invalid == "unknown-field":
            data["allow_owner"] = True
        elif invalid == "empty-default":
            data["default_denied_scope_ids"] = []
        else:
            data["clients"][0]["denied_scope_ids"].append("01ARZ3NDEKTSV4RRFFQ69G5FAW")
        config.write_text(json.dumps(data))
    assert not egress.quick_page_visible(vault, PUBLIC, principal=who)
    assert egress.owner_only_aggregate(vault, principal=who)["available"] is False


def test_capture_namespace_preserves_useful_creation_without_hidden_collision_observations(configured_boundary, vault):
    from exomem import state_migration
    from exomem.create_file import CreateFileError, create_file

    _, authenticate = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="isolated capture stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    outcomes = []
    with principal.request_scope(authenticate("limited")):
        for present in (True, False):
            if not present:
                (vault / PRIVATE).unlink()
            with pytest.raises(CreateFileError) as error:
                create_file(vault, path=PRIVATE, content="Allowed new note", frontmatter={"project": "public-project"})
            outcomes.append(error.value.as_dict())
        result = create_file(vault, path="Knowledge Base/Capture/new.md", content="Allowed new note",
                             frontmatter={"project": "public-project"})
    assert outcomes[0] == outcomes[1] == {"code": "WRITE_REFUSED", "reason": "target is unavailable"}
    assert result.path == "Knowledge Base/Capture/new.md"
    assert "Allowed new note" in (vault / result.path).read_text()


def test_capture_namespace_rejects_private_writes_even_from_unrestricted_owner(configured_boundary, vault):
    from exomem import state_migration
    from exomem import vault as vault_module

    authority = state_migration.assert_offline_migration_authority(source="isolated capture stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    target = vault / "Knowledge Base/Capture/private.md"
    with principal.request_scope(principal.owner_principal(surface="cli")):
        with pytest.raises(ValueError, match="WRITE_REFUSED"):
            vault_module.batch_atomic_write([
                vault_module.PlannedWrite(target, "---\nproject: private-project\n---\nprivate body\n"),
            ], vault_root=vault)
    assert not target.exists()


def test_unresolved_configuration_stays_unavailable_to_reads_and_capture(configured_boundary, vault):
    from exomem import state_migration
    from exomem import vault as vault_module
    from exomem.governance import egress

    _, authenticate = configured_boundary
    target = vault / "Knowledge Base/_Schema/context-roles.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("roles: {}\n")
    authority = state_migration.assert_offline_migration_authority(source="isolated configuration admission")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("limited")):
        assert not egress.quick_page_visible(vault, str(target.relative_to(vault)))
    capture = vault / "Knowledge Base/Capture/configuration.yaml"
    with principal.request_scope(principal.owner_principal(surface="cli")):
        with pytest.raises(ValueError, match="WRITE_REFUSED"):
            vault_module.batch_atomic_write([
                vault_module.PlannedWrite(capture, target.read_text()),
            ], vault_root=vault)
    assert not capture.exists()


def test_rest_discards_a_result_when_configuration_changes_during_computation(configured_boundary, vault, monkeypatch):
    from starlette.testclient import TestClient

    from exomem import server, state_migration, writer_lease

    config, _ = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="isolated result-consumption stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "temporary-boundary-rest-key")
    original = writer_lease.invoke_command

    def change_after_computation(*args, **kwargs):
        result = original(*args, **kwargs)
        data = json.loads(config.read_text())
        data["clients"][0]["denied_scope_ids"] = []
        config.write_text(json.dumps(data))
        return result

    with TestClient(server.build_server(require_auth=False).http_app()) as client:
        payload = {"query": "public information", "mode": "keyword", "detail": "full"}
        headers = {"Authorization": "Bearer temporary-boundary-rest-key"}
        before = client.post("/api/ask_memory", json=payload, headers=headers)
        assert before.status_code == 200, before.text
        assert "public.md" in before.text
        monkeypatch.setattr(writer_lease, "invoke_command", change_after_computation)
        after = client.post("/api/ask_memory", json=payload, headers=headers)
        assert not after.json()["success"]
        assert after.json()["error"]["code"] == "AUTHORIZATION_SESSION_UNAVAILABLE"
        assert "public information" not in after.text


def test_atomic_artifact_capture_uses_proposed_companion_and_cannot_be_reclassified_private(configured_boundary, vault):
    from exomem import preserve, state_migration
    from exomem import vault as vault_module

    config, authenticate = configured_boundary
    data = json.loads(config.read_text())
    data["capture_paths"].append("Knowledge Base/Evidence/Workspace/Files")
    config.write_text(json.dumps(data))
    authority = state_migration.assert_offline_migration_authority(source="isolated artifact capture stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("limited")):
        result = preserve.preserve(vault, scope="Workspace", category="Files", filename="allowed.txt", content="allowed artifact")
    assert (vault / result.path).read_text() == "allowed artifact"
    assert egress.release_allows_download(vault, result.path, principal=authenticate("limited"))
    page = vault / result.sidecar_path
    original = page.read_text()
    proposed = original.replace("    projects: []", "    projects: [private-project]")
    assert proposed != original
    with principal.request_scope(principal.owner_principal(surface="cli")):
        with pytest.raises(ValueError, match="WRITE_REFUSED"):
            vault_module.batch_atomic_write([vault_module.PlannedWrite(page, proposed)], vault_root=vault)
    assert page.read_text() == original


def test_unrestricted_writer_cannot_populate_a_capture_namespace_alias(configured_boundary, vault):
    """An unrestricted writer cannot introduce private portable collisions after arming."""
    from exomem import state_migration
    from exomem import vault as vault_module

    authority = state_migration.assert_offline_migration_authority(source="isolated capture alias stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    target = vault / "Knowledge Base/capture/private.md"
    with principal.request_scope(principal.owner_principal(surface="library")):
        with pytest.raises(ValueError, match="WRITE_REFUSED"):
            vault_module.batch_atomic_write([
                vault_module.PlannedWrite(target, "---\nproject: private-project\n---\nprivate alias\n"),
            ], vault_root=vault)
    assert not target.exists()


def test_adoption_resources_do_not_disclose_run_presence_or_inventory(configured_boundary, vault):
    """Global run selection and counts must not reveal protected inventories through MCP decorators."""
    from fastmcp import FastMCP

    from exomem import adoption_run, server

    _, authenticate = configured_boundary
    mcp = FastMCP("connector-adoption-proof")
    server.register_adoption_mcp(mcp, vault_root=vault)

    async def observe(run_id):
        prompt = await mcp.render_prompt("continue_adoption")
        runs = await mcp.read_resource("exomem://adoption/runs")
        run = await mcp.read_resource(f"exomem://adoption/run/{run_id}")
        return (json.loads(prompt.messages[0].content.text),
                json.loads(runs.contents[0].content), json.loads(run.contents[0].content))

    authenticate("limited")
    absent = asyncio.run(observe("missing"))
    with principal.request_scope(principal.owner_principal(surface="library")):
        legacy = vault / "Legacy"
        legacy.mkdir()
        (legacy / "private.md").write_text("protected inventory canary")
        run = adoption_run.start(vault, path="Legacy")
    authenticate("limited")
    present = asyncio.run(observe(run["run_id"]))
    assert absent == present == ({"available": False, "reason": "audience_restricted"},) * 3
    authenticate("full")
    result = asyncio.run(mcp.read_resource("exomem://adoption/runs"))
    assert json.loads(result.contents[0].content)["runs"][0]["run_id"] == run["run_id"]


@pytest.mark.parametrize("surface", ["resource", "prompt"])
def test_mcp_content_is_withheld_when_origin_session_ends_during_read(configured_boundary, vault, monkeypatch, surface):
    from fastmcp import FastMCP
    from fastmcp.exceptions import PromptError, ResourceError

    from exomem import server_auth
    from exomem.governance.authorization_transport import AuthorizationSessionMiddleware

    _, authenticate = configured_boundary
    who = authenticate("full")
    authority = server_auth.build_session_authority()
    mcp = FastMCP("content-origin-proof")
    mcp.add_middleware(AuthorizationSessionMiddleware(vault))
    revoke = False

    async def read():
        content = (vault / PUBLIC).read_text()
        if revoke:
            await authority.tombstone(who.origin_session.session_id, reason="operator")
        return content

    if surface == "resource":
        mcp.resource("exomem://proof/public")(read)
        error_type = ResourceError
    else:
        mcp.prompt(name="public")(read)
        error_type = PromptError

    async def invoke():
        if surface == "resource":
            return await mcp.read_resource("exomem://proof/public")
        return await mcp.render_prompt("public")

    assert "public information" in str(asyncio.run(invoke()))
    revoke = True
    with pytest.raises(error_type, match="authority changed"):
        asyncio.run(invoke())


def test_private_registries_cannot_change_resolution_bootstrap_or_save_outcomes(configured_boundary, vault):
    """Global vocabulary hashes, aliases and collisions stay unavailable until domains isolate them."""
    from exomem import commands, entity_types, relation_registry, traversal_profiles

    _, authenticate = configured_boundary
    limited = authenticate("limited")

    def observe():
        with principal.request_scope(limited):
            return (
                commands.op_schema_memory(vault, operation="resolve-entity-type", subject="entity-types", query="person"),
                commands.op_connect_memory(vault, operation="resolve-relation", query="supports"),
                commands.op_schema_memory(vault, operation="save-entity-types", proposal=entity_types.empty_proposal(),
                                          expected_hash="untrusted-client-guard", why="save public definition"),
                commands.op_schema_memory(vault, subject="traversal-profiles", operation="diff",
                                          proposal={"schema_version": 1, "profiles": {}}),
                commands.op_bootstrap(vault, profile="full"),
            )

    before = observe()
    with principal.request_scope(principal.owner_principal(surface="library")):
        entity_types.save_registry(vault, {"schema_version": 1, "entity_types": {"private-kind": {
            "parent": "concept", "folder": "PrivateKinds", "label": "Private kind", "aliases": ["private-alias"],
            "capture_guidance": "A durable private concept.", "status": "active",
        }}}, expected_hash=None, observed_ids=())
        relation_registry.save_registry(vault, {"schema_version": 1, "extensions": {"private.correlates": {
            "parent": "supports", "description": "Private correlation", "aliases": ["private-correlation"],
        }}}, expected_hash=None)
        traversal_profiles.save_profiles(vault, {"schema_version": 1, "profiles": {"private-hop": {
            "extends": "provenance", "direction": "outgoing", "max_nodes": 20,
        }}})
    after = observe()
    assert before == after
    assert all(result["available"] is False for result in after[:4])
    assert "private-kind" not in json.dumps(after)
    assert "private-hop" not in json.dumps(after)


def test_limited_owner_can_update_a_wholly_admitted_public_relation_registry(configured_boundary, vault):
    """A ceiling alone must not put an independent public registry update into review."""
    from exomem import commands, registry_history, relation_registry, state_migration

    config, authenticate = configured_boundary
    scope = vault / "Knowledge Base/_Governance/scopes/private.yaml"
    scope.write_text(scope.read_text().replace("projects: [private-project]", "paths: [Notes/private.md]"))
    with principal.request_scope(principal.owner_principal(surface="library")):
        relation_registry.save_registry(vault, relation_registry.empty_proposal())
    # A public save creates canonical history, so its namespace must satisfy the
    # same all-writer visibility rule as other connector creation destinations.
    host = json.loads(config.read_text())
    host["capture_paths"].append(registry_history.history_dir(vault, relation_registry.SPEC.stem).relative_to(vault).as_posix())
    # Arming assigns the existing public definitions and history explicitly.
    host["vocabulary"] = {"public": {
        "namespace": "Knowledge Base/_Schema/public", "history": "public",
        "overrides": {relation_registry.SPEC.stem: {
            "overlay": relation_registry.SPEC.overlay(vault).relative_to(vault).as_posix(),
            "history": relation_registry.SPEC.stem,
        }},
    }, "private": {}, "destinations": {}, "selections": {}}
    config.write_text(json.dumps(host))
    authority = state_migration.assert_offline_migration_authority(source="isolated public history stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    owner = principal.owner_principal(surface="library")
    with principal.request_scope(owner):
        before = relation_registry.load_registry(vault).extension_hash
    with principal.request_scope(authenticate("limited")):
        result = commands.op_schema_memory(vault, subject="relations", operation="save-relations", expected_hash=before,
            why="retain a public relation", proposal={"upsert": {"vault.correlates": {
                "parent": "relates_to", "description": "A public correlation.", "direction": "directed",
            }}})
    assert result["valid"] is True
    assert result["saved"]["previous_hash"] == before
    with principal.request_scope(owner):
        assert "vault.correlates" in relation_registry.load_registry(vault).extensions


def test_private_registry_blocks_vocabulary_currency_before_work_item_lookup(configured_boundary, vault):
    """A private registry cannot surface through review hashes or missing-item distinctions."""
    from exomem import entity_candidates, vocabulary_review

    _, authenticate = configured_boundary
    with principal.request_scope(authenticate("limited")):
        for operation in (
            lambda: vocabulary_review.registry_hashes(vault),
            lambda: vocabulary_review.review(vault),
            lambda: vocabulary_review.context(vault, ref="missing-work-item"),
            lambda: vocabulary_review.decide(vault, ref="missing-work-item", decision={}),
            lambda: entity_candidates.resolve_entity_candidate(vault, name="known alias", entity_type="private-alias"),
        ):
            with pytest.raises(ValueError, match="^GOVERNANCE_OPERATION_UNAVAILABLE:"):
                operation()


@pytest.mark.parametrize("implicit", [False, True], ids=["explicit", "implicit"])
def test_file_retry_separates_clients_and_survives_reauthentication(configured_boundary, vault, monkeypatch, implicit):
    """Owner clients cannot borrow terminals, while the same client's lost response remains replayable."""
    from exomem import commands, writer_lease
    from exomem.governance import connector_boundary

    _, authenticate = configured_boundary
    # Client isolation also corrects existing unarmed mutation caching.
    monkeypatch.delenv(connector_boundary.CONFIG_ENV)
    assert connector_boundary.snapshot(vault) is None
    command = next(item for item in commands.COMMANDS if item.name == "append_to_file")
    marker = ({"implicit_idempotency_scope": "principal:same-owner"} if implicit else {
        "idempotency_key": "same-public-retry-key", "idempotency_principal_scope": "principal:same-owner",
    })

    def append(who):
        with principal.request_scope(who):
            return writer_lease.invoke_command(command, vault, path=PUBLIC,
                content="one actual append", allow_curated=True, **marker)

    append(principal.owner_principal(surface="library"))
    assert (vault / PUBLIC).read_text().count("one actual append") == 1
    limited = authenticate("limited")
    first = append(limited)
    assert (vault / PUBLIC).read_text().count("one actual append") == 2
    writer_lease.reset_managers_for_tests()
    refreshed = authenticate("limited")
    assert refreshed.origin_session != limited.origin_session
    assert append(refreshed) == first
    assert (vault / PUBLIC).read_text().count("one actual append") == 2
    append(authenticate("full"))
    assert (vault / PUBLIC).read_text().count("one actual append") == 3


@pytest.mark.parametrize("operation", ["upload", "download"])
def test_transfer_issuance_never_persists_a_retry_terminal(configured_boundary, vault, monkeypatch, operation):
    """Explicit and implicit request keys must not persist or replay bearer capability results."""
    import sqlite3

    from exomem import commands, writer_lease

    _, authenticate = configured_boundary
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "temporary-transfer-bearer")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "temporary-private-transfer-signing-root")
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "transfer_artifact")
    with principal.request_scope(authenticate("limited")):
        for _ in range(2):
            result = writer_lease.invoke_command(command, vault, operation=operation,
                idempotency_key="public-capability-retry", implicit_idempotency_scope="principal:same-owner")
            assert result["token"]
    with sqlite3.connect(writer_lease.get_manager().idempotency.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM mutations").fetchone()[0] == 0
