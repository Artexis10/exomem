"""`episode_memory`: record a bounded recap, bind it, and inspect the ledger.

One leaf behind MCP, REST and the CLI. `record` writes one canonical recap per
content change through the ordinary Source writer and binds it to the caller's
own episode ledger by the writer's receipt; `inspect` reads that ledger back.
Nothing else is accepted: no curation leaves, proposals or dispositions.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from exomem import commands, curation, episode_capture, episode_recovery, memory_refs, server
from exomem import schema as schema_module
from exomem.__main__ import main as cli_main
from exomem.episode_model import EpisodeError
from exomem.governance import egress
from exomem.governance.principal import (
    RequestPrincipal,
    owner_principal,
    request_scope,
)

KEY = "ep-" + "a1" * 16
RESULT_KEYS = {
    "operation",
    "episode",
    "revision",
    "source",
    "idempotent",
    "recovery",
    "ledger",
    "about_skipped",
}


def _audience(name: str) -> RequestPrincipal:
    return RequestPrincipal(audience_id=name, surface="mcp")


def _record(vault: Path, **overrides: object) -> dict:
    args: dict[str, object] = {
        "action": "record",
        "episode": KEY,
        "subject": "Harbor Lamp purchase",
        "summary": "Chose the brass lamp; delivery date still open.",
        "worked_on": ["Compared two lamps for Project Alpha"],
        "decided": ["Buy the brass Harbor Lamp"],
        "open": ["Confirm the delivery date"],
    }
    args.update(overrides)
    schema = schema_module.load_source_schema(vault)
    return commands.op_episode_memory(vault, schema, **args)


def _episodes(vault: Path) -> list[Path]:
    folder = vault / "Knowledge Base" / "Sources" / "Episodes"
    return sorted(folder.glob("*.md")) if folder.exists() else []


def _frontmatter(path: Path) -> dict:
    head = path.read_text(encoding="utf-8").removeprefix("---\n").partition("\n---\n")[0]
    return yaml.safe_load(head)


def _write_note(vault: Path, name: str, memory_id: str) -> str:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"exomem_id: {memory_id}\n"
        f"title: {name}\n"
        "status: active\n"
        "created: 2026-09-20\n"
        "updated: 2026-09-20\n"
        "sources: []\n"
        "tags: []\n"
        "---\n\n"
        f"# {name}\n\nA note.\n",
        encoding="utf-8",
    )
    return memory_refs.memory_ref(memory_id)


def _withhold_notes_from(vault: Path, audience: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "withheld-notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAC\n"
        "name: Withheld notes\n"
        'paths: ["Notes/Insights/withheld-*.md"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld-notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAD\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAC"]\n'
        f"audience: {audience}\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    from exomem.governance import membership, policy

    membership.clear_memo()
    policy._CACHE.clear()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_the_tool_is_registered_on_all_three_surfaces() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    command = product["episode_memory"]

    assert command.surfaces >= {"mcp", "rest", "cli"}
    assert command.tier == 1
    assert command.cli_writes is True
    assert command.needs_schema is True
    assert command.leaf is commands.op_episode_memory
    assert commands.invocation_is_read_only(command, {"action": "inspect"}) is True
    assert commands.invocation_is_read_only(command, {"action": "record"}) is False


def test_hosted_surfaces_exclude_the_tool() -> None:
    assert "episode_memory" in commands.HOSTED_SURFACE_EXCLUSIONS
    assert "episode_memory" not in commands.hosted_complete_surface_names()
    for profile in commands.PRODUCT_SURFACE_PROFILES.values():
        assert "episode_memory" not in profile.command_names


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #


def test_record_writes_one_recap_and_binds_revision_one(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault, client="claude-code")

    assert set(result) == RESULT_KEYS
    assert result["episode"] == KEY
    assert result["revision"] == 1
    assert result["idempotent"] is False
    assert result["ledger"] == "bound"
    assert result["recovery"] == "available"
    assert result["about_skipped"] == 0
    assert result["source"]["title"] == "Harbor Lamp purchase"
    assert result["source"]["path"].startswith("Knowledge Base/Sources/Episodes/")
    [page] = _episodes(vault)
    assert page.relative_to(vault).as_posix() == result["source"]["path"]
    frontmatter = _frontmatter(page)
    assert frontmatter["episode"] == KEY
    assert frontmatter["client"] == "claude-code"
    assert memory_refs.memory_ref(str(frontmatter["exomem_id"])) == result["source"]["ref"]


def test_record_mints_a_key_when_the_client_holds_none(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault, episode=None)

    assert episode_capture.EPISODE_KEY_RE.fullmatch(result["episode"])
    assert result["revision"] == 1


def test_a_retry_yields_one_page_and_one_revision(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        again = _record(vault)

    assert again["idempotent"] is True
    assert again["revision"] == first["revision"] == 1
    assert again["source"] == first["source"]
    assert len(_episodes(vault)) == 1


def test_a_changed_record_yields_a_second_page_and_revision_two(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        second = _record(vault, summary="Chose the brass lamp; delivery booked for Friday.")

    assert second["idempotent"] is False
    assert second["revision"] == 2
    assert second["source"]["path"] != first["source"]["path"]
    assert len(_episodes(vault)) == 2
    old = _frontmatter(vault / first["source"]["path"])
    assert old["status"] == "superseded"
    assert "status" not in _frontmatter(vault / second["source"]["path"])


def test_one_key_used_by_two_audiences_keeps_separate_ledgers(vault: Path) -> None:
    with request_scope(_audience("client-a")):
        first = _record(vault)
    with request_scope(_audience("client-b")):
        second = _record(vault, summary="Audience B's own account of the same thread.")
    with request_scope(_audience("client-a")):
        inspected = commands.op_episode_memory(
            vault, schema_module.load_source_schema(vault), action="inspect", episode=KEY
        )

    assert first["revision"] == 1
    assert second["revision"] == 1
    assert [item["revision"] for item in inspected["revisions"]] == [1]
    assert all((vault / r["source"]["path"]).exists() for r in (first, second))


def test_a_ledger_failure_after_the_write_is_idempotent_on_retry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = episode_recovery.EpisodeInputOwner.bind_committed_input
    calls = {"n": 0}

    def failing_once(self, key, *, path, reference):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EpisodeError("EPISODE_STORE_WRITE_FAILED", "episode history write was refused")
        return real(self, key, path=path, reference=reference)

    monkeypatch.setattr(episode_recovery.EpisodeInputOwner, "bind_committed_input", failing_once)
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        retried = _record(vault)

    assert first["ledger"] == "unbound"
    assert first["revision"] is None
    assert retried["idempotent"] is True
    assert retried["ledger"] == "bound"
    assert retried["revision"] == 1
    assert len(_episodes(vault)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        lambda: curation.CurationError("CURATION_RUN_CORRUPT", "journal digest mismatch"),
        lambda: OSError("the ledger directory is unwritable"),
    ],
    ids=["corrupt-journal", "os-error"],
)
def test_no_ledger_failure_answers_a_committed_recap_with_an_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    """The page is committed before the bind. Whatever the bind raises then,
    the caller gets the receipt with `ledger: unbound`, never an error that
    invites a retry of a write that already happened."""

    def failing(self, key, *, path, reference, **_kwargs):
        raise failure()

    monkeypatch.setattr(episode_recovery.EpisodeInputOwner, "bind_committed_input", failing)
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault)

    assert result["ledger"] == "unbound"
    assert result["recovery"] == "unavailable"
    assert result["revision"] is None
    assert [page.relative_to(vault).as_posix() for page in _episodes(vault)] == [
        result["source"]["path"]
    ]


def test_a_summary_with_unicode_spaces_is_recorded(vault: Path) -> None:
    """Every line the recap accepts is one the Source writer accepts."""
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault, summary="Chose" + chr(0xA0) + "the brass  lamp.")

    [page] = _episodes(vault)
    assert _frontmatter(page)["summary"] == "Chose the brass lamp."
    assert result["ledger"] == "bound"


def test_an_unresolved_principal_fails_before_anything_is_written(vault: Path) -> None:
    with pytest.raises(ValueError, match="EPISODE_OWNER_UNRESOLVED"):
        _record(vault)

    assert _episodes(vault) == []


def test_a_refused_recap_writes_nothing(vault: Path) -> None:
    secret = "sk-proj-" + "Ab3dE" * 10
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(ValueError, match="EPISODE_CREDENTIAL"):
            _record(vault, said=[f"use {secret}"])
        with pytest.raises(ValueError, match="EPISODE_EMPTY"):
            _record(vault, worked_on=[], decided=[], open=[])

    assert _episodes(vault) == []


def test_unknown_and_withheld_about_refs_are_indistinguishable(vault: Path) -> None:
    visible = _write_note(vault, "visible-lamp-note", "11111111-1111-4111-8111-111111111111")
    withheld = _write_note(vault, "withheld-lamp-note", "22222222-2222-4222-8222-222222222222")
    unknown = memory_refs.memory_ref("33333333-3333-4333-8333-333333333333")
    _withhold_notes_from(vault, "client-a")

    with request_scope(_audience("client-a")):
        kept = _record(vault, episode="ep-" + "b2" * 16, about=[visible])
        hidden = _record(vault, episode="ep-" + "c3" * 16, about=[withheld])
        missing = _record(vault, episode="ep-" + "d4" * 16, about=[unknown])

    assert kept["about_skipped"] == 0
    assert _frontmatter(vault / kept["source"]["path"])["about"] == [visible]
    for result in (hidden, missing):
        assert result["about_skipped"] == 1
        assert "about" not in _frontmatter(vault / result["source"]["path"])
    assert {key: hidden[key] for key in ("about_skipped", "ledger", "idempotent")} == {
        key: missing[key] for key in ("about_skipped", "ledger", "idempotent")
    }


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #


def test_inspect_lists_revisions_and_the_latest_source(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _record(vault)
        second = _record(vault, open=["Confirm the delivery date", "Choose a bulb"])
        inspected = commands.op_episode_memory(
            vault, schema_module.load_source_schema(vault), action="inspect", episode=KEY
        )

    assert inspected == {
        "episode": KEY,
        "revisions": [
            {"revision": 1, "recovery": "available"},
            {"revision": 2, "recovery": "available"},
        ],
        "latest_source_ref": second["source"]["ref"],
        "coverage_current": "unchecked",
    }


def test_inspect_answers_unknown_and_other_audience_keys_identically(vault: Path) -> None:
    schema = schema_module.load_source_schema(vault)
    with request_scope(_audience("client-a")):
        _record(vault)
    with request_scope(_audience("client-b")):
        with pytest.raises(ValueError) as other:
            commands.op_episode_memory(vault, schema, action="inspect", episode=KEY)
        with pytest.raises(ValueError) as unknown:
            commands.op_episode_memory(
                vault, schema, action="inspect", episode="ep-" + "e5" * 16
            )

    assert str(other.value) == str(unknown.value)
    assert "EPISODE_NOT_FOUND" in str(other.value)


def test_inspect_takes_nothing_but_the_episode(vault: Path) -> None:
    schema = schema_module.load_source_schema(vault)
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(ValueError, match="EPISODE_INVALID"):
            commands.op_episode_memory(
                vault, schema, action="inspect", episode=KEY, subject="smuggled"
            )
        with pytest.raises(ValueError, match="EPISODE_KEY_INVALID"):
            commands.op_episode_memory(vault, schema, action="inspect", episode=None)


# --------------------------------------------------------------------------- #
# Doors
# --------------------------------------------------------------------------- #


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    return TestClient(server.build_server(require_auth=False).http_app())


def _payload(key: str) -> dict:
    return {
        "action": "record",
        "episode": key,
        "subject": "Harbor Lamp purchase",
        "summary": "Chose the brass lamp.",
        "worked_on": ["Compared two lamps"],
    }


def test_three_doors_record_through_one_leaf(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    client = _rest_client(monkeypatch)
    rest = client.post(
        "/api/episode_memory",
        json=_payload("ep-" + "b2" * 16),
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest.status_code == 200, rest.text
    rest_result = rest.json()["data"]

    mcp = server.build_server(require_auth=False)
    with request_scope(owner_principal(surface="mcp")):
        called = asyncio.run(
            mcp.call_tool("episode_memory", _payload("ep-" + "c3" * 16), run_middleware=False)
        )
    mcp_result = (
        called.structured_content
        if isinstance(called.structured_content, dict)
        else json.loads(called.content[0].text)
    )

    argv = [
        "episode_memory",
        "--action",
        "record",
        "--episode",
        "ep-" + "d4" * 16,
        "--subject",
        "Harbor Lamp purchase",
        "--summary",
        "Chose the brass lamp.",
        "--worked-on",
        "Compared two lamps",
        "--json",
    ]
    try:
        code = cli_main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr().out
    assert code == 0, out
    cli_result = json.loads(out)["data"]

    for result in (rest_result, mcp_result, cli_result):
        assert RESULT_KEYS <= set(result), result
        assert result["revision"] == 1
        assert result["ledger"] == "bound"
    assert len(_episodes(vault)) == 3


def test_unknown_fields_and_leaves_are_refused_at_every_door(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    client = _rest_client(monkeypatch)
    for field in ("leaves", "proposal", "disposition"):
        response = client.post(
            "/api/episode_memory",
            json={**_payload("ep-" + "b2" * 16), field: ["anything"]},
            headers={"Authorization": "Bearer sekret"},
        )
        assert response.status_code >= 400, response.text
        assert "UNKNOWN_PARAM" in response.text

    mcp = server.build_server(require_auth=False)
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(Exception):  # noqa: B017 - the MCP schema refuses the field
            asyncio.run(
                mcp.call_tool(
                    "episode_memory",
                    {**_payload("ep-" + "c3" * 16), "leaves": ["x"]},
                    run_middleware=False,
                )
            )

    assert _episodes(vault) == []


# --------------------------------------------------------------------------- #
# Guidance: an agent can find the tool and knows when to use it
# --------------------------------------------------------------------------- #


def test_the_action_catalog_reaches_the_tool_from_capture() -> None:
    catalog = commands.simple_action_catalog()
    assert "episode_memory" in catalog["capture"]["advanced"]


def test_the_scaffold_skill_routes_conversation_recaps_to_the_tool() -> None:
    scaffold = Path(commands.__file__).parent / "_scaffold" / "_Schema"
    skill = (scaffold / "SKILL.md").read_text(encoding="utf-8")
    routing = (scaffold / "references" / "operation-routing.md").read_text(encoding="utf-8")
    engagement = (scaffold / "references" / "engagement.md").read_text(encoding="utf-8")

    assert "`episode_memory`" in skill
    assert "**episode_memory**" in routing
    assert "episode_memory" in engagement and "episode_due" in engagement
