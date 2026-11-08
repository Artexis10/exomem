"""The owner's approval operation, through the routes a person or an agent uses.

`approve_procedure` and the `procedure_approvals` review listing run here through the real MCP
server and the generated CLI, against a temporary vault copy. The ledger, key and verdict have
their own tests in `test_procedure_approval.py`. All page text and names are invented.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client
from mcp.types import Implementation
from test_confined_writers import confined as confined
from test_confined_writers import unarmed as unarmed
from test_connector_boundary import configured_boundary as configured_boundary
from test_procedure_approval import (
    KB,
    METHOD,
    OTHER_ID,
    REF,
    REF_ID,
    entry_names,
    facts_of,
    ref_directory,
    write_page,
    write_raw,
)

from exomem import (
    call_ledger,
    commands,
    procedure_approval,
    procedure_approval_operation,
    writer_lease,
)
from exomem import server as server_module
from exomem.__main__ import main
from exomem.cli_ops import OpError
from exomem.delete_file import delete_file
from exomem.edit import edit
from exomem.governance import egress, principal
from exomem.governance.authorization_request import CredentialRule, credential_rule

STRANGER = principal.RequestPrincipal(audience_id="principal:person-a", surface="mcp")


@pytest.fixture
def mcp(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    for name in ("EXOMEM_DISABLE_TIER2", "EXOMEM_MCP_TOOL_SHAPE", "EXOMEM_MCP_LEGACY_COMPAT"):
        monkeypatch.delenv(name, raising=False)
    call_ledger.reset_chain_cache()
    yield server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def call(mcp, tool: str, operation: dict) -> dict:
    """One MCP tool call; a refusal comes back as its error envelope."""

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(tool, {"operation": operation}, raise_on_error=False)
            return result.structured_content

    return asyncio.run(run())


def approve(mcp, ref: str, version: str) -> dict:
    return call(mcp, "approve_procedure", {"kind": "approve", "ref": ref, "expected_version": version})


def listing(mcp) -> dict:
    return call(mcp, "read_review", {"kind": "procedure_approvals"})


def cli(argv: list[str], capsys) -> dict:
    """The generated CLI route; its JSON answer."""
    try:
        main(argv)
    except SystemExit:
        pass
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def invoke(name: str, vault: Path, who, **arguments):
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
    with principal.request_scope(who):
        return writer_lease.invoke_command(command, vault, **arguments)


def page_text(vault: Path, rel: str) -> str:
    return (vault / rel).read_text(encoding="utf-8")


# --- the owner's route ------------------------------------------------------------------------------


def test_an_approve_leads_with_a_sentence_and_the_listing_then_reads_it_approved(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version

    answer = approve(mcp, REF, version)

    assert next(iter(answer)) == "user_summary"
    assert answer["user_summary"].startswith("Approved this version of the procedure.")
    assert (answer["state"], answer["mutated"]) == ("committed", True)
    assert answer["changes"] == {"summary": "This is the first approval of this page."}
    assert answer["diagnostics"]["ref"] == REF
    assert answer["diagnostics"]["entry_id"] in entry_names(vault)
    shown = listing(mcp)
    assert shown["outcome"] == "1 procedure with approval entries: 1 approved, 0 stale, 0 unapproved."
    assert [(row["ref"], row["path"], row["verdict"]) for row in shown["approvals"]] == [
        (REF, rel, "approved")
    ]
    assert shown["findings"] == []
    assert shown["approvals"][0]["latest_entry"]["principal"] == "owner"
    assert shown["approvals"][0]["latest_entry"]["client"] == "mcp"


def test_the_entry_records_the_client_name_from_the_mcp_handshake(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version

    async def named_client_approves():
        async with Client(mcp, client_info=Implementation(name="claude-ai", version="9.9")) as client:
            await client.call_tool(
                "approve_procedure",
                {"operation": {"kind": "approve", "ref": REF, "expected_version": version}},
            )

    asyncio.run(named_client_approves())

    assert listing(mcp)["approvals"][0]["latest_entry"]["client"] == "claude-ai"


def test_a_repeat_through_the_other_route_adds_nothing(vault, mcp, capsys) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    first = cli(
        ["approve_procedure", "--action", "approve", "--ref", REF,
         "--expected-version", version, "--json"],
        capsys,
    )

    repeat = approve(mcp, REF, version)

    assert first["data"]["diagnostics"]["appended"] is True
    # A repeat commits nothing, so no terminal adds a summary: the leaf's own sentence leads.
    assert next(iter(repeat)) == "outcome"
    assert repeat["outcome"] == "This version is already approved. Nothing was added."
    assert repeat["diagnostics"]["entry_id"] == first["data"]["diagnostics"]["entry_id"]
    assert "changes" not in repeat
    assert len(entry_names(vault)) == 1


def test_an_edit_makes_the_page_stale_and_the_next_approve_shows_what_changed(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    first = approve(mcp, REF, facts_of(vault, rel).version)
    edit(
        vault, path=rel, why="sign the tag",
        old_string="Tag the release from a clean checkout",
        new_string="Tag the release from a clean checkout and sign it",
    )
    edit(vault, path=rel, why="name the branch", old_string="# release-steps", new_string="# release-steps on main")

    assert listing(mcp)["approvals"][0]["verdict"] == "stale"
    second = approve(mcp, REF, facts_of(vault, rel).version)

    assert second["diagnostics"]["previous_approved_version"] == first["diagnostics"]["page_version"]
    assert "The diff follows." in second["changes"]["summary"]
    diff = second["changes"]["diff"]
    assert "--- previously approved" in diff
    # Both edits show, so the diff runs from the approved version and not from the latest edit.
    assert "+- [technique] Tag the release from a clean checkout and sign it ^tag" in diff
    assert "+# release-steps on main" in diff


def test_a_change_that_history_does_not_hold_is_said_not_shown(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    approve(mcp, REF, facts_of(vault, rel).version)
    (vault / rel).write_text(page_text(vault, rel) + "\nAn untracked edit.\n", encoding="utf-8")

    second = approve(mcp, REF, facts_of(vault, rel).version)

    assert "diff" not in second["changes"]
    assert "not in this page's retained history" in second["changes"]["summary"]


def test_approving_again_an_unchanged_version_after_a_revoke_says_so(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    approve(mcp, REF, version)
    call(mcp, "approve_procedure", {"kind": "revoke", "ref": REF})

    again = approve(mcp, REF, version)

    assert again["changes"] == {"summary": "The page is unchanged since the version that was approved before."}
    assert len(entry_names(vault)) == 3


# --- refusals ------------------------------------------------------------------------------------------


def test_a_stale_version_is_refused_and_names_the_fix(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    (vault / rel).write_text(page_text(vault, rel) + "\nA later edit.\n", encoding="utf-8")

    refused = approve(mcp, REF, version)

    assert refused["error"]["code"] == "APPROVE_PROCEDURE_STALE_VERSION"
    assert refused["error"]["message"] == "The page changed since it was read."
    assert refused["error"]["remediation"] == "Read the page again, then approve its current content_hash."
    assert not ref_directory(vault).exists()


def test_an_approve_with_a_malformed_version_names_the_shape_and_not_a_page_change(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")

    refused = approve(mcp, REF, facts_of(vault, rel).version.upper())

    assert refused["error"]["code"] == "APPROVE_PROCEDURE_INVALID"
    assert "64 lowercase hex characters" in refused["error"]["message"]
    assert "pass its content_hash as expected_version" in refused["error"]["remediation"]
    assert not ref_directory(vault).exists()


def test_a_revoke_of_a_deleted_page_with_a_malformed_version_writes_nothing(vault, mcp) -> None:
    """No page is left to compare against, so only the shape check keeps an unreadable entry out of the ledger."""
    rel = write_page(vault, "release-steps")
    approve(mcp, REF, facts_of(vault, rel).version)
    delete_file(vault, path=rel, confirm=True)
    before = entry_names(vault)

    refused = call(mcp, "approve_procedure", {"kind": "revoke", "ref": REF, "expected_version": "an old version"})

    assert refused["error"]["code"] == "APPROVE_PROCEDURE_INVALID"
    assert "64 lowercase hex characters" in refused["error"]["message"]
    assert entry_names(vault) == before


def test_a_page_that_repeats_its_id_is_refused_with_the_fix_for_that_condition(vault, mcp) -> None:
    first = write_page(vault, "first-copy")
    write_page(vault, "second-copy")

    refused = approve(mcp, REF, facts_of(vault, first).version)

    assert refused["error"]["code"] == "APPROVE_PROCEDURE_INELIGIBLE"
    assert "Give the copy a new exomem_id" in refused["error"]["remediation"]
    assert not ref_directory(vault).exists()


def test_a_ref_that_no_page_holds_is_refused_and_writes_no_key(vault, mcp) -> None:
    refused = approve(mcp, f"exomem://memory/{OTHER_ID}", "0" * 64)

    assert refused["error"]["code"] == "APPROVE_PROCEDURE_INELIGIBLE"
    assert "Check the ref in the page's frontmatter" in refused["error"]["remediation"]
    assert not procedure_approval.key_path(vault).exists()


def test_a_page_that_the_owner_cannot_read_in_full_reads_as_absent(vault, mcp, monkeypatch) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    monkeypatch.setattr(egress, "release_level_for", lambda *args, **kwargs: egress.DISCLOSURE_MIN)

    refused = approve(mcp, REF, version)

    assert refused["error"]["code"] == "APPROVE_PROCEDURE_INELIGIBLE"
    assert "No page holds this ref, or the page is not readable here" in refused["error"]["message"]
    assert not ref_directory(vault).exists()


@pytest.mark.parametrize(
    ("arguments", "fix"),
    [
        (["--action", "approve", "--ref", "not-a-ref", "--expected-version", "a"], "exomem://memory/<uuid>"),
        (["--action", "approve", "--ref", REF], "pass its content_hash as expected_version"),
        (["--action", "revoke"], "Pass the procedure page's ref"),
        (["--action", "quarantine", "--expected-version", "a"], "Remove expected_version"),
    ],
)
def test_every_route_refuses_an_input_that_no_action_accepts(vault, capsys, arguments, fix) -> None:
    answer = cli(["approve_procedure", *arguments, "--json"], capsys)

    error = answer["error"]
    assert error["code"] == "APPROVE_PROCEDURE_INVALID"
    assert fix in f"{error['message']} {error['remediation']}"
    assert not (vault / KB / "_Approvals").exists()


def test_the_shared_check_refuses_an_action_that_is_not_an_approval_action() -> None:
    with pytest.raises(OpError) as refused:
        procedure_approval_operation.check_arguments("bogus", None, None)

    assert refused.value.code == "APPROVE_PROCEDURE_INVALID"
    assert "approve, revoke or quarantine" in refused.value.remediation


# --- who may call it ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("approve_procedure", {"action": "approve", "ref": REF, "expected_version": "a" * 64}),
        ("approve_procedure", {"action": "revoke", "ref": REF}),
        ("approve_procedure", {"action": "quarantine"}),
        ("review_memory", {"mode": "procedure_approvals"}),
    ],
)
def test_a_caller_who_is_not_the_owner_is_refused_and_changes_nothing(vault, name, arguments) -> None:
    rel = write_page(vault, "release-steps")
    arguments = {**arguments, **({"expected_version": facts_of(vault, rel).version}
                                 if arguments.get("action") == "approve" else {})}

    with pytest.raises(OpError) as refused:
        invoke(name, vault, STRANGER, **arguments)

    assert refused.value.code == "APPROVE_PROCEDURE_OWNER_ONLY"
    assert not (vault / KB / "_Approvals").exists()
    assert not procedure_approval.key_path(vault).exists()


@pytest.fixture(params=["confined-writer", "limited-reader"])
def fenced_owner(request):
    """The owner signed in through a connector client that the host boundary fences from some scopes.

    Its page `Notes/Insights/own-*` is readable in full: it is the confined writer's own matter and
    lies in the limited reader's public scope. A `private-project` page is hidden from both.
    """
    if request.param == "confined-writer":
        _config, authenticate = request.getfixturevalue("confined")
        return authenticate("writer")
    _config, authenticate = request.getfixturevalue("configured_boundary")
    return authenticate("limited")


def _hidden_procedure(vault: Path) -> str:
    rel = f"{KB}/Notes/hidden-release.md"
    (vault / rel).write_text(
        f"---\ntype: insight\nstatus: active\nproject: private-project\nexomem_id: {OTHER_ID}\n---\n\n"
        f"# hidden-release\n\n{METHOD}",
        encoding="utf-8",
    )
    return rel


def _ledger_bytes(vault: Path) -> dict[str, bytes]:
    root = vault / KB / "_Approvals"
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_a_fenced_owner_connection_can_neither_approve_revoke_quarantine_nor_list(vault, fenced_owner) -> None:
    """The owner audience behind a fenced connector reads as a restricted reader, so it holds no ledger power."""
    rel = write_page(vault, "own-release")
    hidden = _hidden_procedure(vault)
    owner = principal.owner_principal(surface="cli")
    for ref, path in ((REF, rel), (f"exomem://memory/{OTHER_ID}", hidden)):
        invoke("approve_procedure", vault, owner, action="approve", ref=ref, expected_version=facts_of(vault, path).version)
    write_raw(ref_directory(vault, f"exomem://memory/{OTHER_ID}"), "1" * 32, b"not json {")
    # The fenced connection may edit its own page, so it could otherwise endorse text the owner never read.
    (vault / rel).write_text(page_text(vault, rel) + "\nAn edit that the owner never approved.\n", encoding="utf-8")
    before = _ledger_bytes(vault)
    key = procedure_approval.key_path(vault).read_bytes()

    for name, arguments in (
        ("approve_procedure", {"action": "approve", "ref": REF, "expected_version": facts_of(vault, rel).version}),
        ("approve_procedure", {"action": "revoke", "ref": f"exomem://memory/{OTHER_ID}"}),
        ("approve_procedure", {"action": "quarantine"}),
        ("approve_procedure", {"action": "quarantine", "ref": f"exomem://memory/{OTHER_ID}"}),
        ("review_memory", {"mode": "procedure_approvals"}),
    ):
        with pytest.raises(OpError) as refused:
            invoke(name, vault, fenced_owner, **arguments)
        assert refused.value.code == "APPROVE_PROCEDURE_OWNER_ONLY", arguments
        assert OTHER_ID not in f"{refused.value.message} {refused.value.remediation}"

    assert _ledger_bytes(vault) == before
    assert procedure_approval.key_path(vault).read_bytes() == key


def test_the_command_is_owner_only_and_off_cloud_with_the_declared_annotations(mcp) -> None:
    async def tools():
        async with Client(mcp) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    listed = asyncio.run(tools())
    tool = listed["approve_procedure"]
    kinds = {branch["properties"]["kind"]["const"]
             for branch in tool.input_schema["properties"]["operation"]["oneOf"]}
    review_kinds = {branch["properties"]["kind"]["const"]
                    for branch in listed["read_review"].input_schema["properties"]["operation"]["oneOf"]}

    assert kinds == {"approve", "revoke", "quarantine"}
    assert "procedure_approvals" in review_kinds
    hints = tool.annotations
    assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint) == (False, False, True)
    for surface in ("mcp", "rest", "cli"):
        assert "approve_procedure" in {c.name for c in commands.product_commands_for(surface)}
    assert "approve_procedure" not in {c.name for c in commands.product_commands_for("mcp", cloud=True)}
    assert credential_rule("approve_procedure", {"action": "approve"}) is CredentialRule.NON_AUTHORIZING


# --- retire, repair and move -----------------------------------------------------------------------------------


def test_a_revoke_works_after_the_page_is_deleted(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    approve(mcp, REF, facts_of(vault, rel).version)
    delete_file(vault, path=rel, confirm=True)

    revoked = call(mcp, "approve_procedure", {"kind": "revoke", "ref": REF})

    assert revoked["user_summary"].startswith("Revoked the approval.")
    assert revoked["diagnostics"]["page_version"] is None
    row = listing(mcp)["approvals"][0]
    assert (row["path"], row["verdict"]) == (None, "unapproved")


def test_a_revoke_still_works_when_a_second_page_repeats_the_id(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    approve(mcp, REF, version)
    write_page(vault, "aaa-copy", body=METHOD + "- [technique] Check the changelog ^log\n")

    revoked = call(mcp, "approve_procedure", {"kind": "revoke", "ref": REF, "expected_version": version})

    assert revoked["user_summary"].startswith("Revoked the approval.")


def test_the_listing_pages_by_limit_and_says_when_it_is_empty(vault, mcp) -> None:
    assert listing(mcp)["outcome"] == "No procedure has approval entries."
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve(mcp, REF, facts_of(vault, first).version)
    approve(mcp, f"exomem://memory/{OTHER_ID}", facts_of(vault, second).version)

    page = call(mcp, "read_review", {"kind": "procedure_approvals", "limit": 1})

    assert (page["returned"], page["total"], page["truncated"]) == (1, 2, True)
    assert "showing 1" in page["outcome"]


def test_quarantine_moves_a_damaged_entry_aside_and_the_listing_stops_reporting_it(vault, mcp) -> None:
    rel = write_page(vault, "release-steps")
    approve(mcp, REF, facts_of(vault, rel).version)
    write_raw(ref_directory(vault), "1" * 32, b"not json {")
    before = listing(mcp)

    moved = call(mcp, "approve_procedure", {"kind": "quarantine"})
    after = listing(mcp)
    idle = call(mcp, "approve_procedure", {"kind": "quarantine"})

    assert [(item["ref"], item["kind"]) for item in before["findings"]] == [(REF, "unreadable")]
    assert before["outcome"].endswith(" 1 finding needs your attention.")
    assert moved["user_summary"].startswith("Moved 1 unreadable entry aside.")
    assert [row["file"] for row in moved["diagnostics"]["moved_aside"]] == ["1" * 32]
    assert after["findings"] == []
    assert after["approvals"][0]["verdict"] == "approved"
    assert "1" * 32 not in entry_names(vault)
    assert next(iter(idle)) == "outcome"
    assert idle["outcome"] == "No approval entries needed moving. Nothing changed."


def test_approvals_checked_with_another_key_read_foreign_until_quarantined_and_approved_again(
    vault, mcp
) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    approve(mcp, REF, version)
    # A vault that moves to another path gets a new key, so the old entries no longer verify.
    procedure_approval.key_path(vault).write_bytes(os.urandom(32))

    foreign = listing(mcp)
    moved = call(mcp, "approve_procedure", {"kind": "quarantine", "ref": REF})
    again = approve(mcp, REF, version)

    assert foreign["approvals"][0]["verdict"] == "unapproved"
    assert [(item["ref"], item["kind"]) for item in foreign["findings"]] == [
        (REF, "foreign_or_tampered")
    ]
    assert moved["user_summary"].startswith("Moved 1 foreign or tampered entry aside.")
    assert again["diagnostics"]["appended"] is True
    assert listing(mcp)["approvals"][0]["verdict"] == "approved"


# --- the key ----------------------------------------------------------------------------------------------------


def test_no_surface_returns_the_key_or_where_it_is_kept(vault, mcp, capsys) -> None:
    rel = write_page(vault, "release-steps")
    version = facts_of(vault, rel).version
    answers = [
        approve(mcp, REF, version),
        approve(mcp, REF, "0" * 64),
        listing(mcp),
        call(mcp, "approve_procedure", {"kind": "quarantine"}),
        call(mcp, "approve_procedure", {"kind": "revoke", "ref": REF}),
        cli(["review_memory", "--mode", "procedure_approvals", "--json"], capsys),
        cli(["approve_procedure", "--action", "approve", "--ref", REF,
             "--expected-version", version, "--json"], capsys),
    ]
    key = procedure_approval.key_path(vault).read_bytes()

    shown = json.dumps(answers)

    assert len(key) == 32 and procedure_approval.key_path(vault).exists()
    assert key.hex() not in shown
    assert base64.b64encode(key).decode() not in shown
    assert base64.urlsafe_b64encode(key).decode() not in shown
    assert procedure_approval.KEY_FILENAME not in shown
    assert str(procedure_approval.key_path(vault).parent) not in shown
    assert REF_ID in shown
