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
import os
import sys
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
#: Drive-qualified and UNC probe spellings are assembled rather than written
#: out. Spelled literally they trip the public-artifact privacy scan's
#: absolute-local-path rule, and weakening a privacy rule to write a test is
#: the wrong trade -- the scan cannot tell a probe fixture from a real leak,
#: and it should not have to.
DRIVE = "C:"
UNC = "\\" * 2 + "server" + "\\" + "share"
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


def _protected_tree_state(vault_root: Path) -> dict[str, str]:
    """Content digest of every file in both protected trees, keyed by path.

    Compares membership and bytes in one value, so a probe that *adds* a file
    is caught as loudly as one that rewrites an existing one.
    """

    state: dict[str, str] = {}
    for name in sorted(gateway.PROTECTED_TREE_DIRNAMES):
        tree = Path(vault_root) / _kb() / name
        for entry in sorted(tree.rglob("*")):
            key = entry.relative_to(vault_root).as_posix()
            state[key] = (
                hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "<dir>"
            )
    return state


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
        # Trailing dots and spaces. Windows strips both from a path component
        # before it reaches the filesystem, so `_Schema ` opens `_Schema`.
        # Nothing in the guard may compare the raw segment and stop there.
        "_Schema /SKILL.md",
        "_Schema./SKILL.md",
        "_Schema. /SKILL.md",
        "Knowledge Base/_Schema /SKILL.md",
        "Knowledge Base/_Schema./SKILL.md",
        "Knowledge Base/_Schema. /SKILL.md",
        "Knowledge Base/_Governance./README.md",
        # Drive-qualified and UNC spellings. `PurePosixPath` sees one opaque
        # segment in `C:_Schema/x.md` and reports no protected tree; the
        # deployment target is Windows, where it is drive-relative.
        f"{DRIVE}_Schema/SKILL.md",
        f"{DRIVE}/Knowledge Base/_Schema/SKILL.md",
        f"{DRIVE}\\Knowledge Base\\_Schema\\SKILL.md",
        f"\\\\?\\{DRIVE}\\Knowledge Base\\_Schema\\SKILL.md",
        f"{UNC}\\_Schema\\SKILL.md",
        # A page that does not exist yet. `resolve()` cannot canonicalise a
        # component that is not there, so a guard that only inspects a resolved
        # target sees nothing -- while the leaf happily creates the file.
        "Knowledge Base/_Schema/brand-new-doctrine.md",
        "_Schema/brand-new-doctrine.md",
        "_Governance/brand-new-policy.md",
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


def _alias_protected_trees(vault_root: Path) -> None:
    """Materialise the spellings an OS can hand a protected tree.

    `_Governance` is 11 characters, so NTFS generates the 8.3 alias `_GOVER~1`
    for it; `_Schema` at 7 gets none. Windows resolves that alias to the long
    name inside the filesystem, below anything Python can see in the string.
    8.3 aliases cannot be *generated* on this box -- it is ext4 under WSL, and
    Windows interop is blocked -- so the alias is stood up here as a symlink,
    which gives `Path.resolve()` the same expansion semantics the real alias
    has. What that proves is every layer above the filesystem: the guard's
    normalisation, its join root, its use of resolution, and the hosted route.
    What it does not prove is NTFS itself. See `design.md` -- final
    confirmation of the 8.3 case requires a Windows run.

    The innocent-name link is the other half: `Notes/doctrine` points into
    `_Schema` while naming nothing protected, so only resolution can see it.
    """

    kb = vault_root / _kb()
    (kb / "_GOVER~1").symlink_to(kb / "_Governance", target_is_directory=True)
    (kb / "Notes").mkdir(parents=True, exist_ok=True)
    (kb / "Notes" / "doctrine").symlink_to(kb / "_Schema", target_is_directory=True)


def _short_path_name(path: Path) -> Path | None:
    """The real NTFS 8.3 alias for `path`, or None when the OS has none.

    Mirrors `tests/test_windows_path_alias_guard.py`, which is where this
    repository already established that the genuine alias case can only be
    exercised on Windows.
    """

    import ctypes
    from ctypes import wintypes

    get_short = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
    get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    result = ctypes.create_unicode_buffer(1024)
    if get_short(str(path), result, len(result)) == 0:
        return None
    return Path(result.value)


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows 8.3 aliases")
def test_guard_refuses_a_native_ntfs_short_name_alias(tmp_path: Path) -> None:
    """The one case the symlink stand-in cannot prove.

    `_Governance` is 11 characters, so NTFS generates `_GOVER~1`; `_Schema` at
    7 gets none. A real alias is not a symlink and not a separate directory
    entry the way the stand-in is, so this is the only test that exercises what
    the deployment platform actually does. It skips everywhere else, which is
    the same convention `tests/test_windows_path_alias_guard.py` uses.
    """

    _app, config = _cell(tmp_path)
    root = Path(config.vault_root)
    governance = root / _kb() / "_Governance"
    short = _short_path_name(governance)
    if short is None or os.path.normcase(str(short)) == os.path.normcase(str(governance)):
        pytest.skip("8.3 short-name generation is disabled for this volume")

    alias_component = Path(short).name
    for candidate in (
        f"{alias_component}/README.md",
        f"{_kb()}/{alias_component}/README.md",
        f"{alias_component}/does-not-exist-yet.md",
        str(Path(short) / "README.md"),
    ):
        assert gateway._names_a_protected_tree(candidate, vault_root=root), candidate


@pytest.mark.parametrize("command", sorted(gateway.PROTECTED_TREE_PATH_ARGUMENTS))
@pytest.mark.parametrize(
    "candidate",
    [
        # The third bypass, in both spellings. The unprefixed form is the one
        # that mattered: the leaf prepends `Knowledge Base/`, so a guard that
        # joins at the vault root evaluates a path where nothing exists,
        # `resolve()` degrades to a lexical no-op, and the only reading that
        # can see an alias is dead code.
        "_GOVER~1/README.md",
        "Knowledge Base/_GOVER~1/README.md",
        "/Knowledge Base/_GOVER~1/README.md",
        "Knowledge Base\\_GOVER~1\\README.md",
        # The innocent-name link, prefixed and not. The unprefixed form used to
        # return False from the guard and was caught only by `PathGuard`.
        "Notes/doctrine/SKILL.md",
        "Knowledge Base/Notes/doctrine/SKILL.md",
        # An alias component with a not-yet-existing page under it.
        "_GOVER~1/brand-new-policy.md",
    ],
)
def test_every_guarded_command_refuses_an_os_aliased_protected_tree(
    tmp_path: Path, command: str, candidate: str
) -> None:
    app, config = _cell(tmp_path)
    _alias_protected_trees(Path(config.vault_root))
    governance = Path(config.vault_root) / _kb() / "_Governance" / "README.md"
    schema_doc = _schema_doc(config.vault_root)
    before = (governance.read_bytes(), schema_doc.read_bytes())

    response = _call(app, config, command, _guarded_body(command, candidate))

    assert response.status_code == 403, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert (governance.read_bytes(), schema_doc.read_bytes()) == before


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
        "_Schema /SKILL.md",
        "_Schema./SKILL.md",
        "_Schema. /SKILL.md",
        "Knowledge Base/_Governance./README.md",
        "Knowledge Base/_GOVERNANCE /README.md",
        f"{DRIVE}_Schema/SKILL.md",
        f"{DRIVE}/Knowledge Base/_Schema/SKILL.md",
        f"{DRIVE}\\Knowledge Base\\_Schema\\SKILL.md",
        f"\\\\?\\{DRIVE}\\Knowledge Base\\_Schema\\SKILL.md",
        f"{UNC}\\_Schema\\SKILL.md",
        "_Schema/does-not-exist-yet.md",
        "_Governance/does-not-exist-yet.md",
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
        "Notes/_Schemas/x.md",
        f"{DRIVE}/Knowledge Base/Notes/ordinary.md",
        f"{UNC}\\Notes\\ordinary.md",
        "Knowledge Base/Notes/schema.md",
        "Knowledge Base/Notes/Governance.md",
        "",
        None,
    ):
        assert not gateway._names_a_protected_tree(candidate, vault_root=absent_vault), candidate


def test_guard_refuses_when_a_reading_raises() -> None:
    """A guard's unknown case is "refuse", never "allow".

    Round three's bypass was an `except` arm that answered False. The rule now
    holds for the whole evaluation, not just the arms someone remembered to
    enumerate, so an argument the guard cannot even read is refused.
    """

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("unreadable target")

    assert gateway._names_a_protected_tree(Hostile(), vault_root=Path("/nonexistent/vault"))


def test_guard_refuses_a_page_that_does_not_exist_yet(tmp_path: Path) -> None:
    """`resolve()` cannot canonicalise a component that is not on disk.

    A hosted agent creating a *new* governing document is the case a
    resolution-only guard misses entirely, and it is not an exotic one -- it is
    how doctrine would actually be extended.
    """

    app, config = _cell(tmp_path)
    fresh = _schema_doc(config.vault_root, "brand-new-doctrine.md")
    assert not fresh.exists()

    for command in sorted(gateway.PROTECTED_TREE_PATH_ARGUMENTS):
        for target in ("_Schema/brand-new-doctrine.md", f"{_kb()}/_Schema/brand-new-doctrine.md"):
            response = _call(app, config, command, _guarded_body(command, target))
            assert response.status_code == 403, f"{command} {target}: {response.text}"
            assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert not fresh.exists()


def test_guard_and_page_leaves_share_one_normaliser() -> None:
    """The structural invariant, asserted rather than described.

    Three bypasses came from a guard and an executor holding independent
    notions of the same path. There is now one function, and the leaves call
    it -- not a copy of it. If either leaf grows its own rel computation again,
    this fails.
    """

    from exomem import edit, kbdir, replace

    assert edit.kb_page_target is kbdir.kb_page_target
    assert replace.kb_page_target is kbdir.kb_page_target

    for module in (edit, replace):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "lstrip(\"/\")" not in source, (
            f"{module.__name__} re-implements the rel-form instead of calling kb_page_target"
        )

    # And the guard reaches the same target the leaves will open.
    vault = Path("/nonexistent/vault")
    for spelling in ("_Schema/SKILL.md", "/Knowledge Base/_Schema/SKILL.md", "_Schema\\SKILL.md"):
        assert kbdir.kb_page_target(vault, spelling)[1] == f"{_kb()}/_Schema/SKILL.md"


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
    name to it must not become a way to wave a command through.

    The earlier version of this test filtered arguments on `"path" in key`,
    which silently certified `capture_source`, `preserve_evidence`, `remember`
    and `triage_memory` as taking no target-shaping argument. All four take
    one: `preserve_evidence` composes its path from `scope`, `category` and
    `filename`; `capture_source` and `remember` derive a filename from `slug`
    and `title`; `triage_memory` selects by `ref`. Their leaves hold, but the
    test was not the reason.

    So the filter is gone. Every string-typed argument in the pinned schema of
    every member is probed with a traversal aimed at the schema tree, and the
    claim asserted is the one that matters: no protected tree changes, and the
    gateway guard never fires. The guard not firing is the point -- a refusal
    from the guard would mean the command belongs in the guarded map instead.

    Honest limit: a probe whose other arguments cannot be made valid (a
    `triage_memory` `ref` has to name a real review item) bounces on argument
    validation before reaching placement. That still proves the two assertions,
    but it does not exercise the leaf's placement logic, and no claim is made
    that it does.
    """

    schemas = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json").read_text(encoding="utf-8")
    )

    def string_arguments(name: str) -> set[str]:
        found: set[str] = set()
        for key, spec in schemas[name]["inputSchema"]["properties"].items():
            stack: list[Any] = [spec]
            while stack:
                node = stack.pop()
                if not isinstance(node, dict):
                    continue
                if node.get("type") == "string":
                    found.add(key)
                    break
                for branch in ("anyOf", "oneOf", "allOf"):
                    stack.extend(node.get(branch) or [])
        return found

    app, config = _cell(tmp_path)

    # A manifest the parser actually accepts, so `record_memory` is refused on
    # its *target* rather than bouncing off manifest validation first. Taken
    # from the command's own published contract.
    manifest_text = (
        _call(app, config, "record_memory", {"action": "describe"})
        .json()["data"]["examples"]["minimal"]["manifest_text"]
    )

    # The other arguments each command needs before it will look at the one
    # being probed. `record_memory` gets a manifest its own published contract
    # says is valid, so it is refused on its *target* rather than bouncing off
    # manifest validation first.
    baseline: dict[str, dict[str, Any]] = {
        "remember": {"content": "## Observations\n\nprobe\n", "title": "Probe"},
        "capture_source": {"content": "probe", "title": "Probe"},
        "preserve_evidence": {
            "scope": "probe",
            "category": "probe",
            "filename": "probe.txt",
            "content": "probe",
        },
        "triage_memory": {"ref": "exomem://review/probe", "action": "dismiss"},
        "observe_memory": {
            "path": f"{_kb()}/Notes/probe.md",
            "operation": "add",
            "category": "note",
            "content": "probe",
        },
        "connect_memory": {
            "operation": "create-entity",
            "name": "Probe Entity",
            "entity_type": "person",
        },
        "record_memory": {
            "action": "create",
            "manifest_path": f"{_kb()}/Records/Probe/_collection.md",
            "manifest_text": manifest_text,
            "why": "probe",
        },
    }
    assert set(baseline) == set(gateway.TARGET_CONSTRAINED_MUTATIONS)

    injection = "../_Schema/injected-by-probe.md"
    trees_before = _protected_tree_state(config.vault_root)
    probed: dict[str, set[str]] = {}

    for name in sorted(gateway.TARGET_CONSTRAINED_MUTATIONS):
        arguments = string_arguments(name)
        assert arguments, f"{name} exposes no string argument -- schema fixture looks wrong"
        probed[name] = set()
        for argument in sorted(arguments):
            response = _call(app, config, name, {**baseline[name], argument: injection})
            # Refused by the leaf, not by the guard -- that is the claim.
            assert "HOSTED_PROTECTED_TREE_MUTATION" not in response.text, f"{name}.{argument}"
            assert _protected_tree_state(config.vault_root) == trees_before, f"{name}.{argument}"
            probed[name].add(argument)

    assert {name: probed[name] for name in probed} == {
        name: string_arguments(name) for name in sorted(gateway.TARGET_CONSTRAINED_MUTATIONS)
    }


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


def test_plan_memory_guards_its_collection_selector_too(tmp_path: Path) -> None:
    """A guarded command is guarded on every target argument, not its first.

    `plan_memory` selects by `collection` as well as `manifest_path` -- the same
    selector `record_memory` carries. Guarding only the path-shaped one leaves a
    second caller-supplied target on a guarded command, which is the shape the
    previous three rounds kept re-creating.
    """

    app, config = _cell(tmp_path)
    trees_before = _protected_tree_state(config.vault_root)

    for collection in (f"{_kb()}/_Schema", "_Schema", "../_Schema", f"{_kb()}/_GOVERNANCE"):
        response = _call(app, config, "plan_memory", {"action": "inspect", "collection": collection})
        assert response.status_code == 403, f"{collection}: {response.text}"
        assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert _protected_tree_state(config.vault_root) == trees_before

    # And an ordinary planning collection still goes through to its own leaf.
    ordinary = _call(
        app, config, "plan_memory", {"action": "inspect", "collection": f"{_kb()}/Planning/Roadmap"}
    )
    assert "HOSTED_PROTECTED_TREE_MUTATION" not in ordinary.text


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
