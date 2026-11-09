"""The `compile` block a recorded recap returns: the ref to cite, and what is left uncited.

The close step is record, then cite: the recap is a Source, and a live page that
names it in `sources:` covers it. The block reports the recap's ref, its Decided
item count and how many of them no recap cited by a live page holds. What these
tests pin is what an agent depends on: the open count falls once a live page cites
the recap, an unchecked count is never 0, a recap request orders no page write
that live policy forbids, and the record never walks the vault to count.

The count borrows a current resident resolver without rebuilding it, so a test
that expects a citation to be found warms the shared resolver just before its
record (`find_module.shared_resolver`), as a running service keeps it warm. It
first joins the background graph rebuild an earlier write started: a rebuild
that retargets drops the shared resolver, and the count then honestly reports
unchecked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from _public_call import public_call
from conftest import _drain_background_threads
from test_episode_coverage_citation import _cite
from test_episode_memory_command import _record, _withhold_notes_from

from exomem import capture_sweep, episode_compile, semantic_contract
from exomem import find as find_module
from exomem import server as server_module
from exomem import vault as vault_module
from exomem.governance import principal as principal_module
from exomem.governance.principal import request_scope

OWNER = principal_module.owner_principal(surface="mcp")
DECIDED = ["Buy the brass lamp", "Ship by rail"]


@pytest.fixture(autouse=True)
def _owner():
    with request_scope(OWNER):
        yield


@pytest.fixture
def record_episode(vault: Path, monkeypatch: pytest.MonkeyPatch):
    """The real MCP `record_episode` tool, as a client calls it; one server serves the whole test."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server_module.build_server(require_auth=False)

    def call(**fields: object) -> dict:
        operation = {"kind": "record", "subject": "Harbor Lamp purchase", "summary": "Chose the brass lamp.", **fields}
        called = asyncio.run(mcp.call_tool(*public_call("record_episode", {"operation": operation}), run_middleware=True))
        structured = called.structured_content
        return structured if isinstance(structured, dict) else json.loads(called.content[0].text)

    return call


def test_the_block_names_the_recap_to_cite_and_the_open_count_falls_once_a_live_page_cites_it(
    vault: Path, record_episode
) -> None:
    # The first write into a vault leaves the writers' snapshot stale; a running service has it warm.
    record_episode(episode="ep-" + "c3" * 16, decided=["Prime the snapshot"])
    _drain_background_threads()
    find_module.shared_resolver(vault)
    first = record_episode(decided=DECIDED)

    assert next(iter(first)) == "user_summary"
    assert first["compile"]["cite"] == first["source"]["ref"]
    assert (first["compile"]["decided"], first["compile"]["open"]) == (2, 2)
    assert first["compile"]["outcome"] == "This recap has 2 decided items; none is already cited by a live page."

    _cite(vault, first["source"]["path"])
    _drain_background_threads()
    find_module.shared_resolver(vault)
    second = record_episode(episode=first["episode"], decided=DECIDED, open=["Pick the paint"])

    assert second["source"]["path"] == first["source"]["path"]
    assert second["compile"]["open"] == 0
    assert second["compile"]["outcome"] == "This recap has 2 decided items; 2 are already cited by a live page."
    assert "next_step" not in second["compile"], "nothing is left to cite"


def test_a_cold_resolver_reports_the_open_count_unchecked_and_builds_no_resolver(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting must not walk the vault on every record, and unknown must not read as 0 open."""
    first = _record(vault, decided=DECIDED)
    _cite(vault, first["source"]["path"])
    monkeypatch.setattr(find_module, "_RESOLVER_CACHE", {})
    work: list[str] = []
    walk = vault_module.walk_vault_md

    def counting_walk(*args: object, **kwargs: object):
        work.append("walk")
        return walk(*args, **kwargs)

    monkeypatch.setattr(vault_module, "walk_vault_md", counting_walk)
    monkeypatch.setattr(find_module, "_schedule_query_resolver_rebuild", lambda *_: work.append("rebuild"))
    block = episode_compile.block(
        vault, ref=first["source"]["ref"], revisions=[first["source"]["path"]], decided=len(DECIDED),
    )

    assert work == []
    assert block["open"] == "unchecked"
    assert block["outcome"] == "This recap has 2 decided items; whether a live page cites it was not checked."


def test_with_a_cold_resolver_a_withheld_citing_page_and_none_give_the_same_block(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block must not tell a caller that a page it cannot see cites the recap."""
    from granted_writer import grant_writer

    _withhold_notes_from(vault, "client-a")
    writer = grant_writer(
        vault, tmp_path, monkeypatch,
        writable_paths=["Sources/Episodes/**"],
        capture_paths=["Knowledge Base/Sources/Episodes"],
        protected=["01ARZ3NDEKTSV4RRFFQ69G5FAC"],
        audience="client-a",
    )("client-a")
    with request_scope(writer):
        cited = _record(vault, episode="ep-" + "a1" * 16, decided=DECIDED)
        uncited = _record(vault, episode="ep-" + "b2" * 16, decided=DECIDED)
    _cite(vault, cited["source"]["path"], title="Withheld lamp pick")
    monkeypatch.setattr(find_module, "_RESOLVER_CACHE", {})
    monkeypatch.setattr(semantic_contract, "current_writer_resolver_entries", lambda *a, **k: None)

    with request_scope(writer):
        withheld_record = _record(vault, episode=cited["episode"], decided=DECIDED, open=["Pick the paint"])
        uncited_record = _record(vault, episode=uncited["episode"], decided=DECIDED, open=["Pick the paint"])

    assert withheld_record["revision"] == uncited_record["revision"] == 2
    withheld, none = withheld_record["compile"], uncited_record["compile"]

    assert {**withheld, "cite": None} == {**none, "cite": None}
    assert withheld["open"] == "unchecked"


def test_the_next_step_rides_only_when_live_policy_permits_capture(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recap request must not order a page write at `off`: the outcome never does, the next step is gated."""
    monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: True)
    permitted = _record(vault, episode="ep-" + "d4" * 16, decided=DECIDED)["compile"]
    monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: False)
    refused = _record(vault, episode="ep-" + "e5" * 16, decided=DECIDED)["compile"]

    assert permitted["next_step"]
    assert "next_step" not in refused
    assert permitted["outcome"] == refused["outcome"]
    assert "again" not in json.dumps([permitted, refused])


def test_a_recap_that_decides_nothing_has_no_compile_block(vault: Path) -> None:
    assert "compile" not in _record(vault, decided=[])
