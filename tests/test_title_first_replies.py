"""Search hits and entity refusals name a page by its title, not its storage path or stored link.

Each case drives the real MCP server over a temporary vault, as an agent does. `detail="full"` on a
search keeps the path and the stored link, for a harness that reads vault files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _public_call import public_call
from fastmcp import Client

from exomem import call_ledger, find, freshness, writer_lease
from exomem import server as server_module
from exomem.vault import walk_vault_md

ENTITY_NAME = "Jäne Exämple"
ENTITY_PATH_FORM = "Knowledge Base/Entities/People/Jäne Exämple"


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


async def _call(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(*public_call(name, arguments), raise_on_error=False)
    assert result.structured_content is not None
    body = result.structured_content
    return body["result"] if set(body) == {"result"} else body


def _resident_resolver(vault: Path) -> None:
    """Hold the shared resolver in memory and current, as the running server's watcher keeps it."""
    freshness.seed(vault, "vault", [(str(p), freshness.stat_signature(p)) for p in walk_vault_md(vault)])
    find.shared_resolver(vault)


def _write_page(vault: Path, rel: str, title: str, page_type: str, body: str) -> None:
    """A page written straight to disk: a tool write starts a graph rebuild that moves the freshness
    checkpoint under a resolver this test holds."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{title}"\ntype: {page_type}\n---\n\n# {title}\n\n{body}\n', encoding="utf-8")


def _create_entity(client: Client, name: str = ENTITY_NAME) -> Any:
    operation = {
        "kind": "create-entity", "entity_type": "person", "name": name,
        "summary": "An invented person for naming checks.",
    }
    return _call(client, "change_connections", {"operation": operation})


def test_search_hits_show_links_in_their_excerpt_by_title(build, vault: Path) -> None:
    _write_page(vault, f"{ENTITY_PATH_FORM}.md", ENTITY_NAME, "entity", "An invented person for naming checks.")
    _write_page(
        vault, "Knowledge Base/Notes/Insights/retry-policy.md", "Retry policy", "insight",
        f"- [operating constraint] Keep retries bounded for [[{ENTITY_PATH_FORM}]] #reliability",
    )
    # An excerpt runs 100 characters past its match: this link starts just before that cut.
    _write_page(
        vault, "Knowledge Base/Notes/Insights/backoff-ceiling.md", "Backoff ceiling", "insight",
        f"zephyrtoken {'plain words ' * 7}[[{ENTITY_PATH_FORM}]] and a long tail. {'tail ' * 30}",
    )

    async def run() -> None:
        async with Client(build()) as client:
            _resident_resolver(vault)

            whole = await _call(client, "search_memory", {"query": "retries bounded"})
            full = await _call(client, "search_memory", {"query": "retries bounded", "detail": "full"})

            excerpts = [hit["excerpt"] for hit in whole["hits"] if hit["title"] == "Retry policy"]
            assert excerpts and f"[[{ENTITY_NAME}]]" in excerpts[0], whole
            assert all("Knowledge Base/" not in hit["excerpt"] for hit in whole["hits"]), whole
            cut = await _call(client, "search_memory", {"query": "zephyrtoken"})
            hit = next(hit for hit in cut["hits"] if hit["title"] == "Backoff ceiling")
            assert hit["excerpt"].endswith("…") and "[[" not in hit["excerpt"], cut
            # A harness that reads vault files asks for `detail="full"`: stored link and path stay.
            noted = next(hit for hit in full["hits"] if hit["title"] == "Retry policy")
            assert f"[[{ENTITY_PATH_FORM}]]" in noted["excerpt"]
            assert noted["path"].endswith("retry-policy.md")

    asyncio.run(run())


def test_entity_exists_refusal_names_the_page_by_title_not_by_path(build) -> None:
    async def run() -> None:
        async with Client(build()) as client:
            await _create_entity(client)

            refused = (await _create_entity(client))["error"]

            assert refused["code"] == "ENTITY_EXISTS"
            assert ENTITY_NAME in refused["message"]
            assert "Knowledge Base/" not in refused["message"]
            assert "candidate_fingerprint" in refused["message"]

    asyncio.run(run())


def test_entity_filename_clash_refusal_names_the_holder_by_title(build) -> None:
    async def run() -> None:
        async with Client(build()) as client:
            await _create_entity(client, "Orbit Lab")

            refused = (await _create_entity(client, "orbit lab?"))["error"]

            assert refused["code"] == "ENTITY_EXISTS"
            assert "Orbit Lab" in refused["message"]
            assert "Knowledge Base/" not in refused["message"]

    asyncio.run(run())
