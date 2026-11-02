"""A restricted caller cannot time a direct read to tell withheld from missing.

Every refusal of a direct read that a restricted caller receives completes at
the fixed disclosure floor. Successes, notices included, and every owner
refusal return without it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import commands, memory_refs, query_log, writer_lease
from exomem.governance import egress, membership, policy, projection_timing, receipts
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

READ_MEMORY = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")


def _restrict_external(vault: Path, *, folder: str, ceiling: int) -> None:
    """Release `Notes/<folder>/**` to `external` at `ceiling`."""
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "example.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC1\n"
        f'name: example\npaths: ["Notes/{folder}/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "example.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC1"]\n'
        f"audience: external\nceiling: {ceiling}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _write_page(vault: Path, relative: str, identity: str) -> None:
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\ntype: insight\nexomem_id: {identity}\n---\n\n# Example\n\nExample body.\n",
        encoding="utf-8",
    )


def _timed_refusal(vault: Path, target: str, **arguments: str) -> tuple[float, str]:
    started = time.perf_counter()
    with pytest.raises(ValueError) as caught:
        writer_lease.invoke_command(READ_MEMORY, vault, path=target, **arguments)
    return time.perf_counter() - started, f"{type(caught.value).__name__}: {caught.value}"


def test_restricted_refusals_complete_at_the_floor_and_read_alike(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    floor_s = projection_timing.RESTRICTED_REFUSAL_FLOOR_MS / 1000.0
    identity = memory_refs.new_id()
    page = "Knowledge Base/Notes/Hidden/withheld-example.md"
    ref = memory_refs.memory_ref(identity)
    unit = f"{ref}#example-unit"
    _write_page(vault, page, identity)
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)

    with request_scope(EXTERNAL):
        withheld_path = _timed_refusal(vault, page)
        hidden_only_ref = _timed_refusal(vault, ref)
        withheld_unit = _timed_refusal(vault, page, unit_ref=unit)
    (vault / page).unlink()
    with request_scope(EXTERNAL):
        missing_path = _timed_refusal(vault, page)
        absent_ref = _timed_refusal(vault, ref)
        missing_unit = _timed_refusal(vault, page, unit_ref=unit)

    for elapsed, _body in (
        withheld_path, hidden_only_ref, withheld_unit, missing_path, absent_ref, missing_unit
    ):
        assert elapsed >= floor_s
    assert withheld_path[1] == missing_path[1]
    assert hidden_only_ref[1] == absent_ref[1]
    assert withheld_unit[1] == missing_unit[1]

    # Nothing can be withheld from the owner, so its refusal is never padded.
    # A floor raised far above a refusal's cost makes any padding unmistakable.
    monkeypatch.setattr(projection_timing, "RESTRICTED_REFUSAL_FLOOR_MS", 2_000)
    with library_scope():
        owner_elapsed, _body = _timed_refusal(vault, page)
    assert owner_elapsed < 1.0


def _door_arguments(name: str, target: str) -> dict:
    """Each floored door's own spelling of "this one path"."""
    return {
        "browse_memory": {"path": target},
        "connect_memory": {"operation": "suggest-links", "path": target},
        "compile_source": {"sources": [target]},
        "query_dataset": {"path": target},
    }[name]


@pytest.mark.parametrize(
    ("name", "withheld", "missing"),
    [
        ("browse_memory", "Knowledge Base/Notes/Hidden", "Knowledge Base/Notes/Absent"),
        (
            "connect_memory",
            "Knowledge Base/Notes/Hidden/withheld-example.md",
            "Knowledge Base/Notes/Hidden/missing-example.md",
        ),
        (
            "compile_source",
            "Knowledge Base/Notes/Hidden/withheld-example.md",
            "Knowledge Base/Notes/Hidden/missing-example.md",
        ),
        (
            "query_dataset",
            "Knowledge Base/Notes/Hidden/withheld-example.csv",
            "Knowledge Base/Notes/Hidden/missing-example.csv",
        ),
    ],
)
def test_the_other_path_doors_refuse_withheld_and_missing_alike_at_the_floor(
    vault: Path, name: str, withheld: str, missing: str
) -> None:
    """Withheld work took 8 to 90 ms longer than a missing path's on each door.

    A restricted caller's refusal now takes the same bytes and at least the
    floor, so neither the text nor the clock tells the two apart.
    """
    floor_s = projection_timing.RESTRICTED_REFUSAL_FLOOR_MS / 1000.0
    _write_page(vault, "Knowledge Base/Notes/Hidden/withheld-example.md", memory_refs.new_id())
    (vault / withheld).parent.mkdir(parents=True, exist_ok=True)
    if withheld.endswith(".csv"):
        (vault / withheld).write_text("name,amount\nexample,1\n", encoding="utf-8")
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == name)

    def refusal(target: str) -> tuple[float, str]:
        started = time.perf_counter()
        with request_scope(EXTERNAL), pytest.raises(Exception) as caught:  # noqa: PT011 - compared as text below
            writer_lease.invoke_command(command, vault, **_door_arguments(name, target))
        text = f"{type(caught.value).__name__}: {caught.value}"
        return time.perf_counter() - started, text.replace(target, "<target>")

    refusal(withheld)  # the first call of a door pays its own caches
    refusal(missing)
    shown, absent = refusal(withheld), refusal(missing)

    assert "NOT_FOUND" in absent[1]
    assert shown[1] == absent[1]
    assert min(shown[0], absent[0]) >= floor_s


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_directory", {"path": "Knowledge Base/Notes/Hidden/absent"}),
        ("suggest_links", {"path": "Knowledge Base/Notes/Hidden/missing-example.md"}),
        ("query_data", {"path": "Knowledge Base/Notes/Hidden/missing-example.csv"}),
    ],
)
def test_a_legacy_alias_refuses_at_its_product_commands_floor(
    vault: Path, name: str, arguments: dict
) -> None:
    """An old tool name reaches the same read as its product command, so it keeps the same clock."""
    floor_s = projection_timing.RESTRICTED_REFUSAL_FLOOR_MS / 1000.0
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    command = next(item for item in commands.COMMANDS if item.name == name)

    def refusal() -> float:
        started = time.perf_counter()
        with request_scope(EXTERNAL), pytest.raises(ValueError, match="NOT_FOUND"):
            writer_lease.invoke_command(command, vault, **arguments)
        return time.perf_counter() - started

    refusal()  # the first call of a door pays its own caches
    assert refusal() >= floor_s


def test_an_empty_inbound_answer_to_a_restricted_caller_completes_at_the_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A withheld target answers `[]` like a missing one, and its decision must not show on the clock.

    The answer is a success, so the dispatcher holds it only when the door asks.
    A visible target and the owner get their links without the wait.
    """
    floor_s = projection_timing.RESTRICTED_REFUSAL_FLOOR_MS / 1000.0
    withheld = "Knowledge Base/Notes/Hidden/withheld-example.md"
    missing = "Knowledge Base/Notes/Hidden/missing-example.md"
    visible = "Knowledge Base/Notes/Open/released-example.md"
    _write_page(vault, withheld, memory_refs.new_id())
    _write_page(vault, visible, memory_refs.new_id())
    source = vault / "Knowledge Base/Notes/Open/links-out.md"
    source.write_text(
        "---\ntype: insight\n---\n\nSee [[withheld-example]] and [[released-example]].\n",
        encoding="utf-8",
    )
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    connect = next(item for item in commands.PRODUCT_COMMANDS if item.name == "connect_memory")

    def inbound(target: str) -> tuple[float, dict]:
        started = time.perf_counter()
        answer = writer_lease.invoke_command(
            connect, vault, operation="inbound-links", target=target
        )
        return time.perf_counter() - started, answer

    with request_scope(EXTERNAL):
        inbound(withheld)  # the first call of a door pays its own caches
        inbound(missing)
        shown, absent = inbound(withheld), inbound(missing)
    assert shown[1] == absent[1]
    assert shown[1]["inbound"] == []
    assert min(shown[0], absent[0]) >= floor_s

    # A floor raised far above the work makes any padding unmistakable.
    monkeypatch.setattr(projection_timing, "RESTRICTED_REFUSAL_FLOOR_MS", 2_000)
    with request_scope(EXTERNAL):
        elapsed, answer = inbound(visible)
    assert [row["path"] for row in answer["inbound"]] == ["Knowledge Base/Notes/Open/links-out.md"]
    assert elapsed < 1.5
    with library_scope():
        elapsed, answer = inbound(withheld)
    assert answer["count"] == 1
    assert elapsed < 1.5


def test_a_withheld_and_a_missing_target_that_a_visible_page_links_answer_alike(
    vault: Path,
) -> None:
    """The caller reads both links verbatim in the visible page, so only this answer could tell them apart.

    The dangling link to a missing target was listed while the link to a
    withheld target was dropped, so the count said which of the two exists.
    """
    withheld = "Knowledge Base/Notes/Hidden/withheld-example.md"
    missing = "Knowledge Base/Notes/Hidden/ghost-example.md"
    _write_page(vault, withheld, memory_refs.new_id())
    source = vault / "Knowledge Base/Notes/Open/links-out.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntype: insight\n---\n\nSee [[Knowledge Base/Notes/Hidden/withheld-example]]"
        " and [[Knowledge Base/Notes/Hidden/ghost-example]].\n",
        encoding="utf-8",
    )
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    connect = next(item for item in commands.PRODUCT_COMMANDS if item.name == "connect_memory")

    with request_scope(EXTERNAL):
        shown, absent = (
            writer_lease.invoke_command(connect, vault, operation="inbound-links", target=target)
            for target in (withheld, missing)
        )
    assert json.dumps(shown, sort_keys=True) == json.dumps(absent, sort_keys=True)
    assert absent["count"] == 0


def test_a_write_through_a_floored_command_decides_on_the_policy_it_finds(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a read answers from one policy load; a write must see a change made before it decides."""
    connect = next(item for item in commands.PRODUCT_COMMANDS if item.name == "connect_memory")
    seen: list[tuple[bool, bool]] = []

    def leaf(vault_root: Path, **_kwargs: object) -> dict:
        before = policy.load(vault_root)
        _restrict_external(vault_root, folder="Hidden", ceiling=egress.LEVEL_NONE)
        seen.append((before.empty, policy.load(vault_root).empty))
        return {}

    class Manager:
        def invoke(self, current, injected, kwargs, **_dispatch_kwargs):
            return current.leaf(*injected, **kwargs)

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    with library_scope():
        writer_lease.invoke_command(
            replace(connect, leaf=leaf), vault, operation="create-entity", name="Example"
        )

    assert seen == [(True, False)]


def test_an_early_refusal_leaves_the_full_withheld_receipt(vault: Path) -> None:
    """The owner's audit record of a refused read keeps its hash, size and ref."""
    identity = memory_refs.new_id()
    page = "Knowledge Base/Notes/Hidden/withheld-example.md"
    _write_page(vault, page, identity)
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    held = (vault / page).read_bytes()
    recorded = len(receipts.event_records(vault))

    with request_scope(EXTERNAL):
        _timed_refusal(vault, page)

    (record,) = receipts.event_records(vault)[recorded:]
    (outcome,) = record["outcomes"]
    assert outcome["decision"] == "withheld"
    assert outcome["level"] == egress.LEVEL_NONE
    assert outcome["content_hash"] == hashlib.sha256(held).hexdigest()
    assert outcome["size"] == len(held)
    assert outcome["ref"] == memory_refs.memory_ref(identity)


def test_a_restricted_notice_returns_without_the_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = "Knowledge Base/Notes/Noticed/noticed-example.md"
    _write_page(vault, page, memory_refs.new_id())
    _restrict_external(vault, folder="Noticed", ceiling=egress.LEVEL_NOTICE)
    monkeypatch.setattr(projection_timing, "RESTRICTED_REFUSAL_FLOOR_MS", 2_000)

    started = time.perf_counter()
    with request_scope(EXTERNAL):
        result = writer_lease.invoke_command(READ_MEMORY, vault, path=page)
    elapsed = time.perf_counter() - started

    assert result["withheld"] is True
    assert result["level"] == egress.LEVEL_NOTICE
    assert "path" not in result
    # Well inside the patched 2 s floor, which a padded notice would wait out.
    assert elapsed < 1.5


def test_a_page_served_with_a_notice_still_seeds_a_link_ranking(vault: Path) -> None:
    """A seed page is refused as absent only when the release gate withholds it entirely."""
    page = "Knowledge Base/Notes/Noticed/noticed-example.md"
    _write_page(vault, page, memory_refs.new_id())
    _restrict_external(vault, folder="Noticed", ceiling=egress.LEVEL_NOTICE)
    connect = next(item for item in commands.PRODUCT_COMMANDS if item.name == "connect_memory")

    with request_scope(EXTERNAL):
        answer = writer_lease.invoke_command(
            connect, vault, operation="suggest-links", path=page
        )

    assert "NOT_FOUND" not in json.dumps(answer)


def test_a_restricted_withheld_refusal_writes_no_read_log_row(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The read log feeds the owner's recency signals; a refused read is no read."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    reads = tmp_path / "reads.jsonl"
    monkeypatch.setattr(query_log, "READS_PATH", reads)
    withheld = "Knowledge Base/Notes/Hidden/withheld-example.md"
    released = "Knowledge Base/Notes/Open/released-example.md"
    _write_page(vault, withheld, memory_refs.new_id())
    _write_page(vault, released, memory_refs.new_id())
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)

    with request_scope(EXTERNAL):
        _timed_refusal(vault, withheld)
        writer_lease.invoke_command(READ_MEMORY, vault, path=released)

    rows = [json.loads(line) for line in reads.read_text(encoding="utf-8").splitlines()]
    assert [row["read_path"] for row in rows] == [released]


def test_mcp_refusals_of_withheld_and_missing_pages_take_the_same_time(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the served MCP stack, logging and error handling included."""
    from fastmcp import Client

    from exomem import server
    from exomem.governance import principal

    withheld = "Knowledge Base/Notes/Hidden/withheld-example.md"
    missing = "Knowledge Base/Notes/Hidden/missing-example.md"
    _write_page(vault, withheld, memory_refs.new_id())
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    caller = RequestPrincipal(audience_id="external", surface="mcp", issuer_family="example-issuer")
    monkeypatch.setattr(principal, "resolve_mcp_principal", lambda: caller)
    elapsed: dict[str, list[float]] = {withheld: [], missing: []}
    answers: set[str] = set()

    async def refuse(client: Client, target: str) -> None:
        started = time.perf_counter()
        result = await client.call_tool("read_memory", {"path": target}, raise_on_error=False)
        elapsed[target].append(time.perf_counter() - started)
        answer = json.loads(result.content[0].text)
        assert answer["success"] is False
        assert answer["error"]["code"] == "NOT_FOUND"
        answers.add(result.content[0].text.replace(target, "<target>"))

    async def refuse_in_turn() -> None:
        async with Client(server.build_server(require_auth=False)) as client:
            await refuse(client, withheld)
            await refuse(client, missing)
            for target in elapsed:
                elapsed[target].clear()
            for turn in range(8):
                first, second = (withheld, missing) if turn % 2 else (missing, withheld)
                await refuse(client, first)
                await refuse(client, second)

    asyncio.run(refuse_in_turn())

    assert len(answers) == 1
    floor_s = projection_timing.RESTRICTED_REFUSAL_FLOOR_MS / 1000.0
    assert min(elapsed[withheld] + elapsed[missing]) >= floor_s
    # Before the fix the two medians stood more than 100 ms apart. A gap that
    # small loaded runners stay well under is still far below that.
    gap = statistics.median(elapsed[withheld]) - statistics.median(elapsed[missing])
    assert abs(gap) < 0.02
