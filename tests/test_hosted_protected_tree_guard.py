"""Hosted profiles must not hand a tenant a write primitive over its own doctrine.

`hosted-alpha-agent-v1` kept `_Schema` safe purely by *not exposing*
`edit_memory` or `replace_memory`. `hosted-alpha-agent-v3` exposes both, so that
protection has to be re-established as an actual control. These tests drive the
real hosted forwarding route -- trusted-context auth, coercion, profile
resolution, lifecycle admission, real command leaf -- not the leaf directly.

Local and CLI behaviour is deliberately untouched: a single-user vault
legitimately customises its own `_Schema`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from exomem import commands as commands_module
from exomem import hosted_gateway as gateway
from exomem import schema
from exomem.hosted_runtime import HostedCellConfig, HostedCellLifecycle, HostedResourceLimits
from exomem.server_hosted import register_hosted_routes

REPO_ROOT = Path(__file__).resolve().parents[1]
V3_PROFILE = "hosted-alpha-agent-v3"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = (
    base64.urlsafe_b64encode(hashlib.sha256(b"protected-tree-principal").digest())
    .rstrip(b"=")
    .decode()
)


class _ProfileConfig(HostedCellConfig):
    """A cell whose operator has selected the epistemic profile.

    Only the profile *selection* input is stubbed. Everything downstream --
    routing, auth, coercion, admission, the command leaf -- is the real thing.
    """

    @property
    def active_agent_profile(self) -> str:
        return V3_PROFILE


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


@contextmanager
def _mutation_guard(_vault_root: Path) -> Iterator[None]:
    yield


def _cell(tmp_path: Path, *, profile: str = V3_PROFILE) -> tuple[Any, HostedCellConfig]:
    from exomem.init import init_vault

    vault_root = tmp_path / "vault"
    init_vault(vault_root)
    factory = _ProfileConfig if profile == V3_PROFILE else HostedCellConfig
    config = factory(
        cell_id="cell-protected-tree",
        vault_root=vault_root,
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential="protected-tree-private-service-credential-01",
        enforce_transfer_v1_compatibility=False,
        records_reader_version=2,
        lifecycle_actions_enabled=(profile == commands_module.HOSTED_ALPHA_AGENT_V2_PROFILE),
        resource_limits=HostedResourceLimits(
            storage_bytes=4 * 1024 * 1024, upload_bytes=4096, worker_count=0
        ),
    )
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True, mutation_authority_ready=True, service_auth_ready=True
    )
    app = FastMCP("test-protected-tree")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=schema.load_source_schema(vault_root),
        mutation_guard_factory=_mutation_guard,
    )
    return app.http_app(), config


def _headers(config: HostedCellConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.service_credential}",
        gateway.CELL_HEADER: config.cell_id,
        gateway.PROTOCOL_HEADER: config.protocol_version,
        gateway.REQUEST_HEADER: REQUEST_ID,
        gateway.PRINCIPAL_HEADER: PRINCIPAL,
    }


def _call(app: Any, config: HostedCellConfig, command: str, body: dict[str, Any]) -> httpx.Response:
    return _request(
        app,
        "POST",
        f"/private/exomem/v1/agent/{V3_PROFILE}/command/{command}",
        json=body,
        headers=_headers(config),
    )


def _kb() -> str:
    from exomem.kbdir import kb_dirname

    return kb_dirname()


def _schema_doc(vault_root: Path, name: str = "SKILL.md") -> Path:
    return vault_root / _kb() / "_Schema" / name


def _schema_rel(config: HostedCellConfig, name: str = "SKILL.md") -> str:
    return _schema_doc(config.vault_root, name).relative_to(config.vault_root).as_posix()


# --- The two commands v3 adds -------------------------------------------------


def test_hosted_v3_refuses_edit_memory_against_the_schema_tree(tmp_path: Path) -> None:
    app, config = _cell(tmp_path)
    target = _schema_doc(config.vault_root)
    assert target.is_file(), "vault scaffold should ship a governing schema document"
    before = target.read_bytes()

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": f"{target.relative_to(config.vault_root).as_posix()}",
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert target.read_bytes() == before


def test_hosted_v3_refuses_replace_memory_against_the_schema_tree(tmp_path: Path) -> None:
    app, config = _cell(tmp_path)
    target = _schema_doc(config.vault_root)
    before = target.read_bytes()

    response = _call(
        app,
        config,
        "replace_memory",
        {
            "old_path": target.relative_to(config.vault_root).as_posix(),
            "content": "hijacked doctrine",
            "title": "Hijacked",
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert target.read_bytes() == before


# --- The bypasses a path guard is usually wrong about -------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "Knowledge Base/_schema/SKILL.md",
        "Knowledge Base/_SCHEMA/SKILL.md",
        "Knowledge Base/_Schema/",
        "Knowledge Base/_Schema//references/frontmatter.md",
        "Knowledge Base/_Schema/references/page-types.md",
        "Knowledge Base/Notes/../_Schema/SKILL.md",
        "Knowledge Base\\_Schema\\SKILL.md",
        "./Knowledge Base/_Schema/SKILL.md",
        "Knowledge Base/_Governance/rules/policy.md",
        "Knowledge Base/_governance/scopes/scope.md",
        # Leading separators. The write leaf treats a rooted-looking path as
        # vault-relative and strips it, so anything that reads the leading `/`
        # as "absolute, therefore not our problem" hands the tenant its
        # doctrine back. One character used to be the whole bypass.
        "/Knowledge Base/_Schema/SKILL.md",
        "/Knowledge Base/_Schema/references/frontmatter.md",
        "/_Schema/references/frontmatter.md",
        "\\Knowledge Base\\_Schema\\SKILL.md",
        "//Knowledge Base/_Schema/SKILL.md",
        "/Knowledge Base/_Governance/README.md",
        "_Schema/SKILL.md",
        "/_Governance/README.md",
    ],
)
def test_hosted_v3_protected_tree_refusal_is_not_bypassable(
    tmp_path: Path, candidate: str
) -> None:
    app, config = _cell(tmp_path)

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": candidate,
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text


def _guarded_body(command: str, target: str) -> dict[str, Any]:
    if command == "edit_memory":
        return {
            "path": target,
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        }
    if command == "replace_memory":
        return {"old_path": target, "content": "hijacked doctrine", "title": "Hijacked"}
    return {"action": "describe", "manifest_path": target}


@pytest.mark.parametrize("command", sorted(gateway.PROTECTED_TREE_PATH_ARGUMENTS))
@pytest.mark.parametrize(
    "candidate",
    [
        "/Knowledge Base/_Schema/SKILL.md",
        "/Knowledge Base/_Schema/references/frontmatter.md",
        "/_Schema/references/frontmatter.md",
        "\\Knowledge Base\\_Schema\\SKILL.md",
        "//Knowledge Base/_Schema/SKILL.md",
        "/Knowledge Base/_Governance/README.md",
    ],
)
def test_every_guarded_command_refuses_a_leading_separator_path(
    tmp_path: Path, command: str, candidate: str
) -> None:
    """The guard must refuse, not merely happen to fail in the leaf.

    `replace_memory` returned `OLD_NOT_FOUND` and `plan_memory` returned
    `INVALID_COLLECTION_PATH` for these shapes while the guard was failing
    open -- saved by leaf behaviour the guard neither depends on nor asserts.
    Assert the 403.
    """

    app, config = _cell(tmp_path)
    schema_doc = _schema_doc(config.vault_root, "references/frontmatter.md")
    before = schema_doc.read_bytes()

    response = _call(app, config, command, _guarded_body(command, candidate))

    assert response.status_code == 403, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert schema_doc.read_bytes() == before


def test_hosted_v3_refuses_an_absolute_path_resolving_inside_the_schema_tree(
    tmp_path: Path,
) -> None:
    app, config = _cell(tmp_path)
    absolute = _schema_doc(config.vault_root).as_posix()

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": absolute,
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text


# --- What must keep working ---------------------------------------------------


def test_hosted_v3_still_edits_ordinary_governed_pages(tmp_path: Path) -> None:
    """The guard must be a scalpel, not a blanket refusal of `edit_memory`."""

    app, config = _cell(tmp_path)
    body = (
        "A durable conclusion used as a positive control.\n\n"
        "## Observations\n\n- [operating constraint] Keep retries bounded #reliability\n"
    )
    created = commands_module.op_remember(
        config.vault_root, title="Ordinary conclusion", content=body, note_type="insight"
    )
    operation = {
        "kind": "replace_string",
        "old_string": "Keep retries bounded",
        "new_string": "Keep retries bounded and logged",
        "validate_only": True,
    }

    ordinary = _call(
        app,
        config,
        "edit_memory",
        {"path": created["path"], "why": "ordinary page stays editable", "operation": operation},
    )
    protected = _call(
        app,
        config,
        "edit_memory",
        {
            "path": f"{_kb()}/_Schema/SKILL.md",
            "why": "doctrine must not be editable",
            "operation": operation,
        },
    )

    # The ordinary page reaches the command; the protected one never does.
    assert ordinary.status_code < 400, ordinary.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" not in ordinary.text
    assert protected.status_code == 403, protected.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in protected.text


def test_hosted_v3_still_reads_the_schema_tree(tmp_path: Path) -> None:
    """The guard is a mutation control, not a read control."""

    app, config = _cell(tmp_path)
    target = _schema_doc(config.vault_root)

    response = _call(
        app,
        config,
        "read_memory",
        {"path": target.relative_to(config.vault_root).as_posix()},
    )

    assert "HOSTED_PROTECTED_TREE_MUTATION" not in response.text


def test_local_surface_still_customises_its_own_schema(tmp_path: Path) -> None:
    """A single-user local vault owns its `_Schema`; the guard must not reach it."""

    from exomem.init import init_vault

    vault_root = tmp_path / "local-vault"
    init_vault(vault_root)
    target = _schema_doc(vault_root)
    before = target.read_text(encoding="utf-8")

    commands_module.op_edit_memory(
        vault_root,
        path=target.relative_to(vault_root).as_posix(),
        why="local operator customises the vault contract",
        operation={"kind": "replace_body", "new_body": before + "\nLocal addition.\n"},
    )

    assert "Local addition." in target.read_text(encoding="utf-8")


def test_governance_readme_is_a_real_prevented_write(tmp_path: Path) -> None:
    """`_Governance` was reachable too -- an existing policy file proves it."""

    app, config = _cell(tmp_path)
    target = config.vault_root / _kb() / "_Governance" / "README.md"
    assert target.is_file(), "vault scaffold should ship a policy-tree document"
    before = target.read_bytes()

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": target.relative_to(config.vault_root).as_posix(),
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked policy\n"},
        },
    )

    assert response.status_code == 403, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert target.read_bytes() == before


def test_guard_refuses_protected_trees_without_over_refusing_lookalikes() -> None:
    """The two failure directions, pinned together.

    Under-refusing costs the tenant its doctrine. Over-refusing would make
    `_Schemas/`, `_Schema_backup/` or a note *about* the schema unwritable.
    """

    absent_vault = Path("/nonexistent/vault")

    for candidate in (
        "Knowledge Base/_Schema/SKILL.md",
        "/Knowledge Base/_Schema/SKILL.md",
        "/_Schema/references/frontmatter.md",
        "\\Knowledge Base\\_Schema\\SKILL.md",
        "//Knowledge Base/_Schema/SKILL.md",
        "/Knowledge Base/_Governance/README.md",
        "_Schema/SKILL.md",
        "/_Governance/README.md",
        "Knowledge Base/_schema/SKILL.md",
        "Knowledge Base/_SCHEMA/SKILL.md",
        "Knowledge Base/Notes/../_Schema/x.md",
        "./Knowledge Base/_Schema/SKILL.md",
        "Knowledge Base/_Schema/",
        "Knowledge Base/_Schema//references/frontmatter.md",
    ):
        assert gateway._names_a_protected_tree(candidate, vault_root=absent_vault), candidate

    for candidate in (
        "Knowledge Base/Notes/Insights/x.md",
        "Knowledge Base/_Schemas/x.md",
        "Knowledge Base/_Schema_backup/x.md",
        "Knowledge Base/my_Schema/x.md",
        "Knowledge Base/Notes/about_Schema-design.md",
        "Knowledge Base/Records/Reader/_collection.md",
        "/Knowledge Base/Notes/ordinary.md",
        "",
        None,
    ):
        assert not gateway._names_a_protected_tree(candidate, vault_root=absent_vault), candidate


def test_guard_follows_a_symlink_that_never_names_a_protected_tree(tmp_path: Path) -> None:
    """The one shape the literal readings cannot see.

    A caller who can create a link inside the vault can name a target whose
    text is innocent. Resolution against the vault root is what catches it, so
    it must run for relative text too, not only for absolute-shaped text.
    """

    _app, config = _cell(tmp_path)
    root = Path(config.vault_root)
    link = root / _kb() / "Notes" / "doctrine"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(root / _kb() / "_Schema", target_is_directory=True)

    for candidate in (
        f"{_kb()}/Notes/doctrine/SKILL.md",
        f"/{_kb()}/Notes/doctrine/SKILL.md",
        str(link / "SKILL.md"),
    ):
        assert gateway._names_a_protected_tree(candidate, vault_root=root), candidate

    # The same link is still innocent when the guard has no vault to resolve
    # against -- the literal readings correctly see nothing, and the guard does
    # not invent a match.
    assert not gateway._names_a_protected_tree(
        f"{_kb()}/Notes/doctrine/SKILL.md", vault_root=None
    )


def test_target_constrained_mutations_are_actually_constrained(tmp_path: Path) -> None:
    """Turn the classification from a claim into a repo guarantee.

    `TARGET_CONSTRAINED_MUTATIONS` silences the startup classifier, so adding a
    name to it must not become a way to wave a command through. Every member is
    checked here, one of two ways: structurally, when the command exposes no
    caller-supplied path-ish argument at all, or behaviourally, when it does --
    in which case its own leaf must leave a protected-tree target untouched
    *without* the gateway guard firing. The guard not firing is the point: if
    the refusal came from the guard, the name would not belong in this set.
    """

    schemas = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json").read_text(encoding="utf-8")
    )

    def path_arguments(name: str) -> set[str]:
        return {
            key for key in schemas[name]["inputSchema"]["properties"] if "path" in key.casefold()
        }

    app, config = _cell(tmp_path)

    # A manifest the parser actually accepts, so `record_memory` is refused on
    # its *target* rather than bouncing off manifest validation first. Taken
    # from the command's own published contract.
    manifest_text = (
        _call(app, config, "record_memory", {"action": "describe"})
        .json()["data"]["examples"]["minimal"]["manifest_text"]
    )

    # The mutating shape of each command that does take a path, aimed straight
    # at the schema tree. `connect_memory` reads its path to decide what to
    # write elsewhere; `observe_memory` treats it as a write target but its own
    # leaf refuses a compiled governing page; `record_memory` is pinned under
    # the Records layer by `_require_profile_layer`.
    behavioural = {
        "observe_memory": lambda target: {
            "path": target,
            "operation": "add",
            "category": "note",
            "content": "injected observation",
        },
        "connect_memory": lambda target: {
            "operation": "create-entity",
            "path": target,
            "name": "Probe Entity",
            "entity_type": "person",
        },
        "record_memory": lambda target: {
            "action": "create",
            "manifest_path": f"{target.rsplit('/', 1)[0]}/_collection.md",
            "manifest_text": manifest_text,
            "why": "probe",
        },
    }

    target = _schema_doc(config.vault_root)
    before = target.read_bytes()
    schema_tree = target.parent
    tree_before = sorted(p.name for p in schema_tree.rglob("*"))
    checked_structurally: set[str] = set()
    checked_behaviourally: set[str] = set()

    for name in sorted(gateway.TARGET_CONSTRAINED_MUTATIONS):
        arguments = path_arguments(name)
        if not arguments:
            checked_structurally.add(name)
            continue
        assert name in behavioural, f"{name} takes {arguments} but is unproven"
        response = _call(app, config, name, behavioural[name](_schema_rel(config)))
        # Refused by the leaf, not by the guard -- that is the claim being made.
        assert response.status_code >= 400, f"{name}: {response.text}"
        assert "HOSTED_PROTECTED_TREE_MUTATION" not in response.text, name
        assert target.read_bytes() == before, name
        assert sorted(p.name for p in schema_tree.rglob("*")) == tree_before, name
        checked_behaviourally.add(name)

    assert checked_structurally | checked_behaviourally == set(
        gateway.TARGET_CONSTRAINED_MUTATIONS
    )
    assert checked_behaviourally == set(behavioural)


def test_guarded_set_is_exactly_what_the_widening_newly_exposes() -> None:
    """Encode the classification rule instead of leaving it to inspection.

    `plan_memory.manifest_path` is guarded while `record_memory.manifest_path`
    -- the same parameter under the same `_require_profile_layer` constraint --
    is not. The rule is not about the parameter. It is that this change guards
    every caller-supplied write target it *newly* exposes, and leaves
    already-published surface classified as it was, so no shipped profile
    changes behaviour.
    """

    v2 = set(commands_module.PRODUCT_SURFACE_PROFILES["hosted-alpha-agent-v2"].command_names)
    v3 = set(commands_module.PRODUCT_SURFACE_PROFILES[V3_PROFILE].command_names)

    assert set(gateway.PROTECTED_TREE_PATH_ARGUMENTS) == v3 - v2
    assert gateway.TARGET_CONSTRAINED_MUTATIONS.isdisjoint(gateway.PROTECTED_TREE_PATH_ARGUMENTS)


def test_plan_memory_is_guarded_as_defence_in_depth_not_as_the_only_control() -> None:
    """Say out loud which guarded arguments the guard is load-bearing for.

    Both collection profiles pin a manifest to their own placement layer, so
    `plan_memory` could not have reached `_Schema` regardless. Recording that
    keeps the next reader from mistaking uniform guarding for evidence that
    every guarded argument was independently exploitable -- only `edit_memory`
    and `replace_memory` were.
    """

    from exomem import collection_profiles, structured_collections

    for profile in ("records", "planning"):
        layer = collection_profiles.profile_for(profile).placement_layer
        with pytest.raises(structured_collections.CollectionError) as excinfo:
            structured_collections._require_profile_layer(
                profile, f"{_kb()}/_Schema/_collection.md", "manifest"
            )
        assert excinfo.value.code == "INVALID_COLLECTION_PATH"
        # And the layer it does allow is not a protected tree.
        structured_collections._require_profile_layer(
            profile, f"{_kb()}/{layer}/Probe/_collection.md", "manifest"
        )
        assert layer not in gateway.PROTECTED_TREE_DIRNAMES


def test_a_profile_with_an_unclassified_mutation_refuses_to_serve() -> None:
    """Fail closed: the next widening cannot reopen this hole by silence."""

    from types import MappingProxyType

    original = commands_module.PRODUCT_SURFACE_PROFILES
    widened = dict(original)
    widened["hosted-test-unclassified"] = commands_module.ProductSurfaceProfile(
        name="hosted-test-unclassified",
        command_names=("bootstrap", "ask_memory", "schema_memory"),
    )
    commands_module.PRODUCT_SURFACE_PROFILES = MappingProxyType(widened)
    try:
        with pytest.raises(gateway.HostedGatewayError) as raised:
            gateway.assert_profile_mutations_are_classified("hosted-test-unclassified")
        assert raised.value.code == "HOSTED_SURFACE_PROFILE_UNSUPPORTED"

        # Every currently registered profile is fully classified.
        for profile in original:
            gateway.assert_profile_mutations_are_classified(profile)
    finally:
        commands_module.PRODUCT_SURFACE_PROFILES = original
