"""A command asks the collection store about its pages once per decision shape, never once per page.

The store thread serves one request at a time, so every store request queues behind
its other work. A turn that asked about each of its store-owned pages on its own
paid one queue wait per page, and when the store refused, the pages went on asking
one by one instead of being withheld together. These tests count the requests that
reach the store thread for each command of a caller's workflow. Invented names only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_collection_store_service import DAILY, DAILY_PATH, DAILY_TEXT, OWNER
from test_collection_store_service import service as service  # noqa: F401  (the pytest fixture)
from test_context_catalogue import _built
from test_governance_egress import EXTERNAL, _reset_caches, write_rule, write_scope
from test_working_set_carry import _write, seed_ordinary_notes

from exomem import (
    call_spans,
    command_surface,
    commands,
    lexstore,
    working_set_index,
    working_set_runtime,
    writer_lease,
)
from exomem.collection_store import connection, runtime
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, request_scope

SHARED = "Knowledge Base/Notes/Shared/quorvel-tansid-survey.md"
PAGE_COUNT = 12
LINKED = (3, 7, 11)
READ_ONLY_NO_WAKE = {"read_only": True, "wake_maintenance": False}
UNRESOLVED = "Tell me about zzxq plorv?"
PHRASE = "What did the quorvel tansid survey conclude?"
TITLES = "Open Quorvel tansid ledger 03 and Quorvel tansid ledger 07"
QUERY = "quorvel tansid ledger"


class StoreRequests:
    """The keyword arguments of every request that reaches the collection-store thread."""

    def __init__(self) -> None:
        self.kwargs: list[dict] = []
        #: The 1-based request, counted from the last `kwargs.clear()`, to refuse as busy, the way a store
        #: that misses its queue deadline does.
        self.refuse_nth: int | None = None


@pytest.fixture
def store_requests(monkeypatch: pytest.MonkeyPatch) -> StoreRequests:
    requests = StoreRequests()
    real = runtime.StoreServer.call

    def counting(self, work, **kwargs):
        requests.kwargs.append(kwargs)
        if requests.refuse_nth == len(requests.kwargs):
            raise connection.busy("the collection store did not take this request within the mutation timeout")
        return real(self, work, **kwargs)

    monkeypatch.setattr(runtime.StoreServer, "call", counting)
    return requests


def _page_path(index: int) -> str:
    return DAILY_PATH.replace("Daily", f"Survey{index:02d}")


def _store_pages(root: Path) -> list[str]:
    """Store collections whose titles carry the turn's invented words; each manifest is a store-owned page."""
    from test_collection_store_service import call

    pages = []
    for index in range(PAGE_COUNT):
        text = DAILY_TEXT.replace(DAILY, f"7777{index:04d}-7777-4777-8777-777777777777").replace(
            "title: Daily", f"title: Quorvel tansid ledger {index:02d}"
        )
        call(root, "record_memory", action="create", manifest_path=_page_path(index), manifest_text=text, why="seed")
        pages.append(_page_path(index))
    return pages


def _survey_note() -> str:
    links = " ".join(f"[[Quorvel tansid ledger {index:02d}]]" for index in LINKED)
    return (
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-10\nproject: open-project\n---\n\n"
        f"# Quorvel tansid survey\n\n## Summary\n\n- [decision] The quorvel tansid survey closed early. {links}\n"
    )


def _seed(root: Path, caller: str) -> list[str]:
    pages = _store_pages(root)
    if caller == "owner_with_policy":
        write_scope(root, paths="Unrelated/**")
        write_rule(root, ceiling=egress.LEVEL_FULL)
    elif caller == "external":
        write_scope(root, paths="Records/**", default_deny=True)
        write_rule(root, ceiling=0)
    _write(root / SHARED, _survey_note())
    seed_ordinary_notes(root)
    _built(root).close()
    lexstore.ensure_fresh(root)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    _reset_caches()
    return pages


def _who(caller: str) -> RequestPrincipal:
    return RequestPrincipal(audience_id=EXTERNAL, surface="mcp") if caller == "external" else OWNER


def _ask(root: Path, who: RequestPrincipal) -> tuple[set[str], list[str]]:
    """The paths ask_memory returned, and the components its warming marker names."""
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "ask_memory")
    with request_scope(who):
        result = writer_lease.invoke_command(command, root, query=QUERY, limit=20)
    hits = result["hits"] if isinstance(result, dict) else result
    return {hit["path"] for hit in hits}, (result.get("warming") or {}).get("components", []) if isinstance(result, dict) else []


def _mcp(root: Path, who: RequestPrincipal, name: str, **kwargs):
    """A command as an MCP client calls it: the dispatcher's pass, then the binding's own terminal pass."""
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
    with request_scope(who):
        return command_surface.bind_vault(command.leaf, root, command=command)(**kwargs)


def _hits(result) -> tuple[set[str], list[str]]:
    """The paths of an ask_memory result, and the components its warming marker names."""
    if isinstance(result, dict):
        return {hit["path"] for hit in result["hits"]}, (result.get("warming") or {}).get("components", [])
    return {hit["path"] for hit in result}, []


def _refs(packet: dict) -> list[str]:
    return [item["ref"] for item in packet["turn_slice"]["items"]]


@pytest.mark.timeout(180)
@pytest.mark.parametrize("caller", ["owner", "owner_with_policy", "external"])
def test_a_command_asks_the_store_once_for_all_its_pages(
    service, store_requests: StoreRequests, caller: str  # noqa: F811
) -> None:
    """Each command of the workflow makes at most one store request, read-only and without waking maintenance."""
    root, who = service.root, _who(caller)
    pages = _seed(root, caller)
    counts: dict[str, int] = {}
    unexpected: list[dict] = []

    def run(label: str, command):
        store_requests.kwargs.clear()
        result = command()
        counts[label] = len(store_requests.kwargs)
        unexpected.extend(kwargs for kwargs in store_requests.kwargs if kwargs != READ_ONLY_NO_WAKE)
        return result

    unresolved = run("unresolved turn", lambda: _activate(root, who, turn=UNRESOLVED))
    phrase = run("phrase turn", lambda: _activate(root, who, turn=PHRASE))
    titles = run("title turn", lambda: _activate(root, who, turn=TITLES))
    follow_up = run(
        "follow-up", lambda: _activate(root, who, turn="And Quorvel tansid ledger 11?", continuity=phrase["continuity"])
    )
    handle = call_spans.MCP_CALL_TOKEN.set("store-decisions")
    try:
        found, warming = run("ask_memory", lambda: _ask(root, who))
    finally:
        spans = call_spans.pop_call_spans("store-decisions")
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert {label: count for label, count in counts.items() if count > 1} == {}, "a command asked per page"
    assert unexpected == [], "a projection read must not write or wake maintenance"
    assert _refs(unresolved) == []
    assert _refs(phrase) == [SHARED]
    store_spans = [span for span in spans if span["name"].startswith("store.call@")]
    assert [(span["count"], span["fields"]["pages"], span["fields"]["refused"], span["fields"]["wake"])
            for span in store_spans] == [(1, PAGE_COUNT, 0, 0)]
    # The site is the code that asked, not the plumbing between it and the store.
    assert not store_spans[0]["name"].removeprefix("store.call@").startswith(
        ("collection_store.", "published_admission", "functools", "contextlib", "unknown")
    )
    assert SHARED in found and "collection_store" not in warming
    if caller == "external":
        assert not found & set(pages), "the policy withholds the Records pages from this caller"
    else:
        assert set(pages) <= found
    assert all("collection_store" not in packet["generation"] for packet in (unresolved, phrase, titles, follow_up))


@pytest.mark.timeout(180)
def test_a_refused_store_request_withholds_the_store_pages_once_and_says_so(
    service, store_requests: StoreRequests  # noqa: F811
) -> None:
    """The refusal ends the command's questions to the store; the next command asks again."""
    root = service.root
    pages = _seed(root, "owner")

    store_requests.kwargs.clear()
    store_requests.refuse_nth = 1
    refused = _activate(root, OWNER, turn=PHRASE + " " + TITLES)
    assert len(store_requests.kwargs) == 1, "pages kept asking the refusing store one by one"
    assert _refs(refused) == [SHARED], "the activation still answers from the pages the store does not own"
    assert refused["generation"]["collection_store"] == "unavailable"

    store_requests.kwargs.clear()
    store_requests.refuse_nth = 1
    found, warming = _ask(root, OWNER)
    assert len(store_requests.kwargs) == 1
    assert not found & set(pages)
    assert "collection_store" in warming

    store_requests.refuse_nth = None
    found, warming = _ask(root, OWNER)
    assert set(pages) <= found, "a refusal outlived the command it refused"
    assert "collection_store" not in warming


@pytest.mark.timeout(180)
@pytest.mark.parametrize("caller", ["owner", "owner_with_policy", "external"])
def test_an_mcp_call_asks_the_store_once_per_pass(
    service, store_requests: StoreRequests, caller: str  # noqa: F811
) -> None:
    """The MCP binding adds its own independent terminal pass: one request more, never one per page."""
    root, who = service.root, _who(caller)
    pages = _seed(root, caller)

    store_requests.kwargs.clear()
    found, warming = _hits(_mcp(root, who, "ask_memory", query=QUERY, limit=20))
    asked = list(store_requests.kwargs)
    store_requests.kwargs.clear()
    activation = _mcp(root, who, "activate_context", turn=PHRASE)
    activated = list(store_requests.kwargs)

    assert len(asked) <= 2, "the dispatcher pass and the MCP pass each ask once"
    assert len(activated) <= 2
    assert all(kwargs == READ_ONLY_NO_WAKE for kwargs in asked + activated)
    assert SHARED in found and "collection_store" not in warming
    assert _refs(activation) == [SHARED] and "collection_store" not in activation["generation"]
    if caller == "external":
        assert not found & set(pages), "the policy withholds the Records pages from this caller"
    else:
        assert set(pages) <= found


@pytest.mark.timeout(180)
def test_a_store_that_refuses_the_mcp_pass_withholds_the_store_pages_once_and_says_so(
    service, store_requests: StoreRequests  # noqa: F811
) -> None:
    """The pass that decides last refuses once; its store pages are withheld, and the owner is told."""
    root = service.root
    pages = _seed(root, "owner")

    store_requests.kwargs.clear()
    store_requests.refuse_nth = 2
    found, warming = _hits(_mcp(root, OWNER, "ask_memory", query=QUERY, limit=20))
    assert len(store_requests.kwargs) == 2, "pages kept asking the refusing store one by one"
    assert SHARED in found and not found & set(pages)
    assert "collection_store" in warming

    store_requests.refuse_nth = None
    store_requests.kwargs.clear()
    found, warming = _hits(_mcp(root, OWNER, "ask_memory", query=QUERY, limit=20))
    assert set(pages) <= found and "collection_store" not in warming, "a refusal outlived the call it refused"
