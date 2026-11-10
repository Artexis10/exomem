"""An agent names a page by its title or a short id wherever a tool takes a page path.

The end-to-end test drives the real MCP server over a temporary vault. The restricted-caller test
drives the dispatcher a connector uses. The unit tests cover the branches the workflow does not.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from _public_call import public_call
from conftest import FIXTURE_VAULT, initialize_vault_state_offline
from fastmcp import Client
from test_derived_identifier_egress import (
    _WITHHELD_TARGET,
    _WRITE_SCOPE,
    NOTES,
    WITHHELD_DIR,
    _call,
    _materialize,
    _page,
    _principal,
    _reset,
    _write_door_fixture,
)

from exomem import call_ledger, command_surface, find, page_names, writer_lease
from exomem import server as server_module
from exomem import vault as vault_module
from exomem.governance.principal import request_scope

_ID = re.compile(r"[a-z2-7]{7}\.[a-z2-7]{4}")


@pytest.fixture
def build(vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_MCP_TOOL_SHAPE", raising=False)
    call_ledger.reset_chain_cache()
    yield lambda: server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


async def _tool(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(*public_call(name, arguments), raise_on_error=False)
    assert result.structured_content is not None
    return result.structured_content


def _new_note(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "content": (
            f"# {title}\n\n## Observations\n\n"
            "- [operating constraint] Keep retries bounded #reliability\n"
        ),
        "type": "insight",
        "unlinked_reason": "standalone",
    }


def _replace(old: str, new: str) -> dict[str, Any]:
    return {"action": "replace_text", "old_text": old, "new_text": new}


def test_an_agent_names_pages_by_title_and_id_end_to_end(build) -> None:
    async def run() -> None:
        async with Client(build()) as client:
            created = await _tool(client, "create_note", _new_note("Retry policy"))
            assert created["user_summary"].startswith("Saved -> Retry policy (")
            assert created["title"] == "Retry policy"
            assert _ID.fullmatch(created["id"])
            assert "path" not in created

            # A page created a second ago resolves by its title, and read replies carry the same id.
            read = await _tool(client, "read_page", {"page": "Retry policy"})
            assert "Keep retries bounded" in read["body"]
            assert read["id"] == created["id"]

            # An edit names the page by its id: the id is also the version the edit is checked against.
            edited = await _tool(
                client,
                "edit_note",
                {"note": created["id"], **_replace("bounded", "capped"), "why": "tighten"},
            )
            assert edited["title"] == "Retry policy"
            head = created["id"].partition(".")[0]
            assert edited["id"].startswith(f"{head}.") and edited["id"] != created["id"]

            # An id for a version the page moved past refuses with the current id.
            # One retry using that refusal's own arguments succeeds.
            stale_call = {"note": created["id"], **_replace("capped", "limited"), "why": "tighten"}
            refused = await _tool(client, "edit_note", stale_call)
            assert refused["error"]["code"] == "STALE_EDIT"
            assert edited["id"] in refused["error"]["message"]
            fix = refused["error"]["retry"]["arguments"]
            assert fix["note"] == edited["id"]
            retried = await _tool(client, "edit_note", {**stale_call, **fix})
            assert retried["title"] == "Retry policy"
            assert "path" not in retried

            # An edit by title takes the page as it is now and needs no id.
            by_title = await _tool(
                client,
                "edit_note",
                {"note": "Retry policy", **_replace("limited", "bounded"), "why": "restore"},
            )
            assert by_title["title"] == "Retry policy"

            # The tag writer compares the id's version too: the version before `by_title`
            # is refused, and the version it left is taken.
            tag = {"action": "set_tags", "tags": ["ops"], "why": "tag"}
            stale_tag = await _tool(client, "edit_note", {"note": retried["id"], **tag})
            assert stale_tag["error"]["code"] == "STALE_EDIT"
            patched = await _tool(client, "edit_note", {"note": by_title["id"], **tag})
            assert patched["title"] == "Retry policy"

            # Two pages share the title: the refusal offers both, the picked id resolves.
            await _tool(client, "create_note", _new_note("Retry policy"))
            ambiguous = await _tool(client, "read_page", {"page": "Retry policy"})
            error = ambiguous["error"]
            assert error["code"] == "AMBIGUOUS_REFERENCE"
            assert error["message"] == "2 pages are titled “Retry policy”."
            assert [set(row) for row in error["candidates"]] == [
                {"title", "type", "date", "id"}
            ] * 2
            assert error["retry"]["arguments"]["page"] == "<id>"
            picked = await _tool(client, "read_page", {"page": error["candidates"][0]["id"]})
            assert picked["id"] == error["candidates"][0]["id"]

    asyncio.run(run())


def test_a_restricted_caller_cannot_tell_a_withheld_title_from_an_unknown_one(
    tmp_path: Path,
) -> None:
    open_pages = {
        f"{NOTES}/shared.md": _page("Shared plan", "Visible twin.", type="insight"),
        f"{NOTES}/open.md": _page("Open plan", "Visible text.", type="insight"),
    }
    withheld_pages = {
        f"{WITHHELD_DIR}/shared.md": _page("Shared plan", "Withheld twin.", type="insight"),
        f"{WITHHELD_DIR}/secret.md": _page("Secret plan", "Withheld text.", type="insight"),
    }
    vaults = {
        "without": _materialize(tmp_path / "without" / "vault", dict(open_pages), "external"),
        "with": _materialize(
            tmp_path / "with" / "vault", {**open_pages, **withheld_pages}, "external"
        ),
    }

    answers = {
        name: {
            title: _call(vault, _principal("external"), "read_memory", path=title)
            for title in ("Secret plan", "Shared plan", "Nonexistent plan")
        }
        for name, vault in vaults.items()
    }

    # A title only withheld pages hold refuses as the vault without those pages refuses it.
    assert answers["with"]["Secret plan"] == answers["without"]["Secret plan"]
    # A withheld page of the same title neither makes the title ambiguous nor is a candidate.
    assert answers["with"]["Shared plan"] == answers["without"]["Shared plan"]
    assert "Visible twin." in answers["with"]["Shared plan"]["body"]
    # And an unknown title refuses in the same shape.
    assert answers["with"]["Secret plan"].keys() == answers["with"]["Nonexistent plan"].keys()


def test_a_restricted_mcp_caller_gets_named_replies_as_if_the_withheld_pages_were_absent(
    build, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    open_pages = {
        f"{NOTES}/shared.md": _page("Shared plan", "Visible twin.", type="insight"),
        f"{NOTES}/open.md": _page("Open plan", "Visible text.", type="insight"),
    }
    withheld_pages = {
        f"{WITHHELD_DIR}/shared.md": _page("Shared plan", "Withheld twin.", type="insight"),
        f"{WITHHELD_DIR}/secret.md": _page("Secret plan", "Withheld text.", type="insight"),
    }

    def server_vault(name: str, files: dict[str, str]) -> Path:
        # The server needs a real vault (schema and state), which the bare twin vaults above lack.
        root = tmp_path / name / "vault"
        shutil.copytree(FIXTURE_VAULT, root)
        initialize_vault_state_offline(root, source="restricted caller twin vault")
        return _materialize(root, files, "external")

    vaults = {
        "without": server_vault("without", dict(open_pages)),
        "with": server_vault("with", {**open_pages, **withheld_pages}),
    }

    async def tool(mcp, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await mcp.call_tool(name, arguments, run_middleware=False)
        assert result.structured_content is not None
        return result.structured_content

    async def ask(mcp) -> dict[str, Any]:
        return {
            "read": {
                title: await tool(mcp, "read_memory", {"path": title})
                for title in ("Secret plan", "Shared plan", "Nonexistent plan")
            },
            "search": await tool(mcp, "ask_memory", {"query": "plan", "limit": 20}),
        }

    answers = {}
    for name, vault in vaults.items():
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
        writer_lease.reset_managers_for_tests()
        _reset()
        # `run_middleware=False` skips the token check an in-process caller has no token for, and the
        # tool wrapper then runs the command, the egress release and the naming pass as this principal.
        with request_scope(_principal("external")):
            answers[name] = asyncio.run(ask(build()))

    # The naming pass runs after egress, so it must not name a page the filter dropped.
    assert answers["with"]["read"] == answers["without"]["read"]
    assert "Visible twin." in answers["with"]["read"]["Shared plan"]["body"]
    assert _ID.fullmatch(answers["with"]["read"]["Shared plan"]["id"])
    assert answers["with"]["read"]["Secret plan"].keys() == answers["with"]["read"]["Nonexistent plan"].keys()
    hits = answers["with"]["search"]["result"]["hits"]
    # Corpus statistics reorder hits between the twins, so compare the hits and not their order.
    by_id = {hit["id"]: hit for hit in hits}
    assert by_id == {hit["id"]: hit for hit in answers["without"]["search"]["result"]["hits"]}
    assert "Withheld" not in json.dumps(hits)
    assert {"Shared plan", "Open plan"} <= {hit["title"] for hit in hits}
    assert all(_ID.fullmatch(hit["id"]) and "path" not in hit for hit in hits)


async def _keyed(
    client: Client, name: str, arguments: dict[str, Any], key: str
) -> dict[str, Any]:
    meta = {command_surface.MCP_IDEMPOTENCY_META_KEY: key}
    result = await client.call_tool(*public_call(name, arguments), meta=meta, raise_on_error=False)
    assert result.structured_content is not None
    return result.structured_content


async def _overwrite_is_refused(client: Client, guard: str) -> None:
    """An edit that names the version `guard` is refused once another writer has changed the page."""
    refused = await _tool(
        client,
        "edit_note",
        {"note": guard, **_replace("limited", "dropped"), "why": "overwrite"},
    )
    assert refused["error"]["code"] == "STALE_EDIT"


def test_a_replayed_write_names_the_version_it_wrote_not_the_page_as_it_is_now(build) -> None:
    async def run() -> None:
        async with Client(build()) as client:
            created = await _tool(client, "create_note", _new_note("Retry policy"))
            first_edit = {"note": created["id"], **_replace("bounded", "capped"), "why": "tighten"}
            first = await _keyed(client, "edit_note", first_edit, "first-edit-key")
            # Another writer changes the page after the first edit.
            await _keyed(
                client,
                "edit_note",
                {"note": first["id"], **_replace("capped", "limited"), "why": "tighten more"},
                "second-edit-key",
            )

            replayed = await _keyed(client, "edit_note", first_edit, "first-edit-key")
            assert replayed["id"] == first["id"]

            # The id of the replayed reply cannot guard an edit of the other writer's version.
            await _overwrite_is_refused(client, replayed["id"])

    asyncio.run(run())


def test_a_replayed_create_names_the_version_it_wrote_not_the_page_as_it_is_now(build) -> None:
    async def run() -> None:
        async with Client(build()) as client:
            create = _new_note("Retry policy")
            created = await _keyed(client, "create_note", create, "create-key")
            # Another writer changes the page after the create.
            await _tool(
                client,
                "edit_note",
                {"note": created["id"], **_replace("bounded", "limited"), "why": "tighten"},
            )

            replayed = await _keyed(client, "create_note", create, "create-key")
            assert replayed["id"] == created["id"]

            # The id of the replayed reply cannot guard an edit of the other writer's version.
            await _overwrite_is_refused(client, replayed["id"])

    asyncio.run(run())


def test_a_replayed_supersede_names_the_successor_it_wrote(build) -> None:
    async def run() -> None:
        async with Client(build()) as client:
            old = await _tool(client, "create_note", _new_note("Retry policy"))
            supersede = {**_new_note("Retry policy v2"), "note": old["id"], "why": "revise"}
            first = await _keyed(client, "supersede_note", supersede, "supersede-key")
            successor = first["pages"][1]
            # Another writer changes the successor after the supersede.
            await _tool(
                client,
                "edit_note",
                {"note": successor["id"], **_replace("bounded", "limited"), "why": "tighten"},
            )

            replayed = await _keyed(client, "supersede_note", supersede, "supersede-key")
            assert replayed["pages"] == first["pages"]

            # The id of the replayed successor cannot guard an edit of the other writer's version.
            await _overwrite_is_refused(client, replayed["pages"][1]["id"])

    asyncio.run(run())


_FAKE_ID = "aaaaaaa.aaaa"


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        (
            "edit_memory",
            {
                "path": _WITHHELD_TARGET,
                "why": "fix",
                "operation": {
                    "kind": "replace_string",
                    "old_string": "Withheld",
                    "new_string": "X",
                    "expected_hash": _FAKE_ID,
                },
            },
        ),
        (
            "observe_memory",
            {
                "path": _WITHHELD_TARGET,
                "operation": "update",
                "unit_ref": "exomem://memory/0192f0a4-6b7c-4d8e-9f10-a1b2c3d4e5f6#finding",
                "expected_hash": _FAKE_ID,
                "expected_fingerprint": "0" * 64,
                "category": "finding",
                "content": "New.",
            },
        ),
    ],
    ids=["edit", "observe"],
)
def test_an_id_guard_does_not_tell_a_restricted_caller_a_withheld_page_exists(
    tmp_path: Path, command: str, arguments: dict[str, Any]
) -> None:
    base, withheld = _write_door_fixture()
    vaults = {
        name: _materialize(tmp_path / name / "vault", files, "external", scope=_WRITE_SCOPE)
        for name, files in (("without", dict(base)), ("with", {**base, **withheld}))
    }

    answers = {
        name: _call(vault, _principal("external"), command, **arguments)
        for name, vault in vaults.items()
    }

    assert answers["with"] == answers["without"]
    assert "STALE" not in json.dumps(answers["with"])


def test_a_frontmatter_id_guard_adds_nothing_a_restricted_caller_learns_of_a_withheld_page(
    tmp_path: Path,
) -> None:
    base, withheld = _write_door_fixture()
    vault = _materialize(
        tmp_path / "vault", {**base, **withheld}, "external", scope=_WRITE_SCOPE
    )
    current = page_names.read_identity(vault, _WITHHELD_TARGET)
    assert current is not None

    def patch(guard: str | None) -> Any:
        operation = {"kind": "patch_frontmatter", "field": "tags", "value": ["x"]}
        if guard is not None:
            operation["expected_hash"] = guard
        return _call(
            vault, _principal("external"), "edit_memory",
            path=_WITHHELD_TARGET, why="fix", operation=operation,
        )

    # This door answers a withheld path differently from an absent one with or without a guard; the
    # guard must change nothing: neither a stale-version answer nor the current id.
    unguarded = patch(None)
    assert patch(_FAKE_ID) == unguarded
    assert patch(current.id) == unguarded
    assert "STALE" not in json.dumps(unguarded) and current.id not in json.dumps(unguarded)


def test_a_visible_page_titled_like_a_withheld_path_is_no_oracle_for_that_path(
    tmp_path: Path,
) -> None:
    visible = {
        f"{NOTES}/lookalike.md": _page("Notes/Withheld/target", "Visible text.", type="insight"),
    }
    vaults = {
        "without": _materialize(tmp_path / "without" / "vault", dict(visible), "external"),
        "with": _materialize(
            tmp_path / "with" / "vault", {**visible, **_write_door_fixture()[1]}, "external",
            scope=_WRITE_SCOPE,
        ),
    }

    answers = {
        name: _call(vault, _principal("external"), "read_memory", path="Notes/Withheld/target")
        for name, vault in vaults.items()
    }

    assert answers["with"] == answers["without"]
    assert "Visible text." in answers["with"]["body"]


def test_a_path_a_title_and_a_short_id_name_pages_in_that_order(tmp_path: Path) -> None:
    root = _materialize(
        tmp_path / "vault",
        {
            f"{NOTES}/alpha.md": _page("Gamma", "Named by its path.", type="insight"),
            f"{NOTES}/beta.md": _page("Notes/alpha.md", "Titled like the path.", type="insight"),
            f"{NOTES}/delta.md": _page("Epsilon", "Named by its title.", type="insight"),
        },
        "external",
    )

    # A value that names a file is a path even when another page carries it as its title.
    by_path = page_names.resolve_page(root, "Notes/alpha.md")
    by_title = page_names.resolve_page(root, "Epsilon")
    by_id = page_names.resolve_page(root, page_names.read_identity(root, by_title).id)

    assert vault_module.title_key("Notes/alpha.md") in find.shared_resolver(root).titles
    assert by_path.removesuffix(".md").endswith("Notes/alpha")
    assert by_title == f"{NOTES}/delta.md"
    assert by_id == by_title


def test_pages_that_share_one_id_are_told_apart_by_their_paths(tmp_path: Path) -> None:
    copied = _page(
        "Copied plan", "Same bytes.", type="insight", exomem_id="0192f0a4-6b7c-4d8e-9f10-a1b2c3d4e5f6"
    )
    root = _materialize(
        tmp_path / "vault",
        {f"{NOTES}/original.md": copied, f"{NOTES}/copy.md": copied},
        "external",
    )

    with pytest.raises(page_names.AmbiguousTitle) as refusal:
        page_names.resolve_page(root, "Copied plan")

    details = refusal.value.details
    assert sorted(row["path"] for row in details["candidates"]) == [
        f"{NOTES}/copy.md",
        f"{NOTES}/original.md",
    ]
    assert details["retry_with"] == {"path": "<path>"}


def test_a_title_key_folds_unicode_form_and_case_but_not_distinct_letters() -> None:
    assert vault_module.title_key("Café Plan ") == vault_module.title_key("CAFÉ plan")
    assert vault_module.title_key("Straße") != vault_module.title_key("Strasse")


def test_raw_material_gives_way_to_an_editable_page_of_the_same_title(tmp_path: Path) -> None:
    root = _materialize(
        tmp_path / "vault",
        {
            f"{NOTES}/plan.md": _page("Rollout plan", "A note.", type="insight"),
            "Knowledge Base/Sources/plan.md": _page("Rollout plan", "Raw capture.", type="source"),
        },
        "external",
    )

    with pytest.raises(page_names.AmbiguousTitle):
        page_names.resolve_page(root, "Rollout plan")

    edit = page_names.resolve_page(root, "Rollout plan", prefer=page_names.prefer_editable)
    assert edit == f"{NOTES}/plan.md"
