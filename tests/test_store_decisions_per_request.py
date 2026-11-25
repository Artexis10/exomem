"""A command asks the collection store about its pages once per decision shape, never once per page.

The store thread serves one request at a time, so every store request queues behind
its other work. A turn that asked about each of its store-owned pages on its own
paid one queue wait per page, and when the store refused, the pages went on asking
one by one instead of being withheld together. These tests count the requests that
reach the store thread for each command of a caller's workflow. Invented names only.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
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
    due_state,
    lexstore,
    structured_collections,
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
BESIDE = {"beside": True}
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
    """Counts requests at both doors: the store thread's queue (`call`) and the read beside it (`read_beside`).

    A read beside that falls back onto the queue counts once, as the read beside it. A background read
    beside that never queues (``queue=False``) is not a request the command makes, so it is not counted.
    """
    requests = StoreRequests()
    real_call, real_beside = runtime.StoreServer.call, runtime.StoreServer.read_beside
    beside = threading.local()

    def counted(kwargs):
        requests.kwargs.append(kwargs)
        if requests.refuse_nth == len(requests.kwargs):
            raise connection.busy("the collection store did not take this request within the mutation timeout")

    def counting(self, work, **kwargs):
        if not getattr(beside, "active", False):
            counted(kwargs)
        return real_call(self, work, **kwargs)

    def counting_beside(self, work, **kwargs):
        if not kwargs.get("queue", True):
            # A background read that never queues was never a request the store thread served.
            return real_beside(self, work, **kwargs)
        counted(BESIDE)
        beside.active = True
        try:
            return real_beside(self, work, **kwargs)
        finally:
            beside.active = False

    monkeypatch.setattr(runtime.StoreServer, "call", counting)
    monkeypatch.setattr(runtime.StoreServer, "read_beside", counting_beside)
    return requests


def _page_path(index: int) -> str:
    return DAILY_PATH.replace("Daily", f"Survey{index:02d}")


def _store_ref(index: int) -> str:
    return f"exomem://memory/7777{index:04d}-7777-4777-8777-777777777777"


def _store_pages(root: Path, count: int = PAGE_COUNT) -> list[str]:
    """Store collections whose titles carry the turn's invented words; each manifest is a store-owned page."""
    from test_collection_store_service import call

    pages = []
    for index in range(count):
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


def _seed(root: Path, caller: str, count: int = PAGE_COUNT) -> list[str]:
    pages = _store_pages(root, count)
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
        result = writer_lease.invoke_command(command, root, query=QUERY, limit=20, include_timings=True)
    hits = result["hits"] if isinstance(result, dict) else result
    return {hit["path"] for hit in hits}, (result.get("warming") or {}).get("components", []) if isinstance(result, dict) else []


def _mcp(root: Path, who: RequestPrincipal, name: str, **kwargs):
    """A command as an MCP client calls it: the dispatcher's pass, then the binding's own terminal pass."""
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
    with request_scope(who):
        return command_surface.bind_vault(command.leaf, root, command=command)(**kwargs)


def _hits(result) -> tuple[set[str], list[str]]:
    """The paths of an ask_memory result asked with `detail="full"`, and the components its warming marker names."""
    if isinstance(result, dict):
        return {hit["path"] for hit in result["hits"]}, (result.get("warming") or {}).get("components", [])
    return {hit["path"] for hit in result}, []


def _refs(packet: dict) -> list[str]:
    return [item["ref"] for item in packet["turn_slice"]["items"]]


#: Store requests per command, the same at every page count. An activation now offers the store's collections, and
#: each of its components that decides them (body, terminal pass, slice leads, task candidates, skills) asks once.
#: A policy that withholds the store's pages leaves the map's one batched decision. Sharing one decision among the
#: components is the store's own follow-up; until then, more requests here mean a new component or a per-page one.
STORE_REQUESTS = {
    "owner": {"unresolved turn": 2, "phrase turn": 7, "title turn": 7, "follow-up": 6, "ask_memory": 2},
    "owner_with_policy": {"unresolved turn": 2, "phrase turn": 6, "title turn": 6, "follow-up": 5, "ask_memory": 2},
    "external": {"unresolved turn": 1, "phrase turn": 1, "title turn": 1, "follow-up": 1, "ask_memory": 1},
}


@pytest.mark.timeout(240)
@pytest.mark.parametrize("count", [PAGE_COUNT, PAGE_COUNT + 4])
@pytest.mark.parametrize("caller", ["owner", "owner_with_policy", "external"])
def test_a_command_asks_the_store_once_for_all_its_pages(
    service, store_requests: StoreRequests, caller: str, count: int  # noqa: F811
) -> None:
    """A command asks the store a fixed number of times, whatever the number of store pages, never once per page."""
    root, who = service.root, _who(caller)
    pages = _seed(root, caller, count)
    counts: dict[str, int] = {}
    unexpected: list[dict] = []

    def run(label: str, command):
        store_requests.kwargs.clear()
        result = command()
        counts[label] = len(store_requests.kwargs)
        unexpected.extend(kwargs for kwargs in store_requests.kwargs if kwargs not in (READ_ONLY_NO_WAKE, BESIDE))
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

    assert counts == STORE_REQUESTS[caller], "a command asked per page, or a new component asked"
    assert unexpected == [], "a projection read must not write or wake maintenance"
    assert _refs(unresolved) == []
    if caller == "external":
        assert _refs(phrase) == [SHARED]
    else:
        # The turn's words title the store's collections, which recall now offers.
        assert _refs(phrase) and set(_refs(phrase)) <= {_store_ref(index) for index in range(count)}
    store_spans = [span for span in spans if span["name"].startswith(("store.call@", "store.read_beside@"))]
    # The body and the terminal pass each decide every page; policy withholds an external caller's
    # store pages before the terminal pass, so only its body asks.
    passes = 1 if caller == "external" else 2
    assert [(span["count"], span["fields"]["pages"], span["fields"]["refused"], span["fields"]["wake"])
            for span in store_spans] == [(passes, passes * count, 0, 0)]
    # The site is the code that asked, not the plumbing between it and the store.
    assert not store_spans[0]["name"].split("@", 1)[1].startswith(
        ("collection_store.", "published_admission", "functools", "contextlib", "unknown")
    )
    assert SHARED in found and "collection_store" not in warming
    if caller == "external":
        assert not found & set(pages), "the policy withholds the Records pages from this caller"
    else:
        assert set(pages) <= found
    assert all("collection_store" not in packet["generation"] for packet in (unresolved, phrase, titles, follow_up))


@pytest.mark.timeout(180)
@pytest.mark.parametrize("caller", ["owner", "owner_with_policy", "external"])
def test_a_read_that_reuses_the_due_state_memo_asks_the_store_once_for_its_pages(
    service, store_requests: StoreRequests, monkeypatch: pytest.MonkeyPatch, caller: str  # noqa: F811
) -> None:
    """A read that serves a due-state build asks the release plane again for every page the build asked about.

    Bootstrap and activation serve the build that way. The verdicts stay the same either way, so only the
    request count shows a recheck that went back to one store request per page.
    """
    root, who = service.root, _who(caller)
    pages = _seed(root, caller)
    # Fixed, so a run that crosses midnight UTC still reuses the build: the memo is kept per day.
    now = dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)
    due_state.save(root, {
        "version": due_state.SCHEMA_VERSION,
        "categories": {"predictions": {SHARED: {"open": [{"path": SHARED}]}}},
        "marker": "store-pages",
    })

    def build(vault_root, *, today, now, principal=None, purpose=None, **_inputs):
        # As the real build does: record the verdict the release plane gave each page it asked about.
        keep = egress.release_walk_filter(vault_root, principal=principal, purpose=purpose)
        asked = {page: True if keep is None else bool(keep(page)) for page in [SHARED, *pages]}
        return [{"ref": "exomem://memory/1", "kind": "open", "path": SHARED}], None, asked, None

    monkeypatch.setattr(due_state, "_served_entries_uncached", build)
    due_state.reset_serve_cache()
    try:
        with request_scope(who):
            due_state.served_entries(root, now=now, principal=who)
        store_requests.kwargs.clear()
        with request_scope(who):
            due_state.served_entries(root, now=now, principal=who)
    finally:
        due_state.reset_serve_cache()

    assert len(store_requests.kwargs) == 1, "the memo's store pages were rechecked one store request each"


@pytest.mark.timeout(180)
@pytest.mark.parametrize("caller", ["owner", "owner_with_policy", "external"])
def test_routing_a_write_to_a_records_collection_asks_the_store_once_for_the_collections(
    service, store_requests: StoreRequests, caller: str  # noqa: F811
) -> None:
    """A semantic write routes its terms through every records collection the writer may see.

    Each collection's manifest, its storage source and its items pass the release plane. Asked one at a
    time, every store-owned page queued its own store request after every write.
    """
    root, who = service.root, _who(caller)
    pages = _seed(root, caller)
    # Current rows, as the claims projection holds them, so each one reaches its storage source check.
    claims = {
        page: {"manifest_hash": structured_collections.load_manifest(root, root / page).manifest_version.hash,
               "items": []}
        for page in pages
    }

    store_requests.kwargs.clear()
    with request_scope(who):
        due_state.routing_targets(root, payload={"claims": claims}, principal=who)

    assert len(store_requests.kwargs) == 1, "routing asked the store about each collection on its own"


@pytest.mark.timeout(180)
def test_a_refused_store_request_withholds_the_store_pages_once_and_says_so(
    service, store_requests: StoreRequests  # noqa: F811
) -> None:
    """The refusal ends the command's questions to the store; the next command asks again.

    The turn names store collections, so a refused store withholds every page it would have offered: the
    slice does not fill the gap from lower-ranked pages (a known limit, not this test's subject).
    """
    root = service.root
    pages = _seed(root, "owner")

    store_requests.kwargs.clear()
    store_requests.refuse_nth = 1
    refused = _activate(root, OWNER, turn=PHRASE + " " + TITLES)
    assert len(store_requests.kwargs) == 1, "pages kept asking the refusing store one by one"
    assert not set(_refs(refused)) & {_store_ref(index) for index in range(PAGE_COUNT)}, "a refused store's page was served"
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
def test_an_mcp_call_asks_the_store_once_for_the_body_and_once_for_the_terminal_pass(
    service, store_requests: StoreRequests, caller: str  # noqa: F811
) -> None:
    """An MCP call runs the one dispatcher pass: the body asks once and the terminal pass asks once, never per page."""
    root, who = service.root, _who(caller)
    pages = _seed(root, caller)

    store_requests.kwargs.clear()
    found, warming = _hits(_mcp(root, who, "ask_memory", query=QUERY, limit=20, detail="full"))
    asked = list(store_requests.kwargs)
    store_requests.kwargs.clear()
    activation = _mcp(root, who, "activate_context", turn=PHRASE)
    activated = list(store_requests.kwargs)

    # The policy withholds every store page from the external caller before the terminal pass sees one.
    assert len(asked) == (1 if caller == "external" else 2), "the body and the terminal pass each ask once"
    assert len(activated) == (1 if caller == "external" else 2)
    assert all(kwargs in (READ_ONLY_NO_WAKE, BESIDE) for kwargs in asked + activated)
    assert SHARED in found and "collection_store" not in warming
    assert "collection_store" not in activation["generation"]
    if caller == "external":
        assert _refs(activation) == [SHARED]
    else:
        assert _refs(activation) and set(_refs(activation)) <= {_store_ref(index) for index in range(PAGE_COUNT)}
    if caller == "external":
        assert not found & set(pages), "the policy withholds the Records pages from this caller"
    else:
        assert set(pages) <= found


@pytest.mark.timeout(180)
def test_a_store_that_refuses_the_terminal_pass_withholds_the_store_pages_once_and_says_so(
    service, store_requests: StoreRequests  # noqa: F811
) -> None:
    """The store refuses the terminal pass, which decides live after the body: the pages are withheld, the owner is told."""
    root = service.root
    pages = _seed(root, "owner")

    store_requests.kwargs.clear()
    store_requests.refuse_nth = 2
    found, warming = _hits(_mcp(root, OWNER, "ask_memory", query=QUERY, limit=20, detail="full"))
    assert len(store_requests.kwargs) == 2, "pages kept asking the refusing store one by one"
    assert SHARED in found and not found & set(pages)
    assert "collection_store" in warming

    store_requests.refuse_nth = None
    store_requests.kwargs.clear()
    found, warming = _hits(_mcp(root, OWNER, "ask_memory", query=QUERY, limit=20, detail="full"))
    assert set(pages) <= found and "collection_store" not in warming, "a refusal outlived the call it refused"


@pytest.mark.timeout(180)
def test_a_write_that_holds_the_store_thread_does_not_withhold_the_owners_store_pages(
    service,  # noqa: F811
) -> None:
    """Work that holds the store thread past the mutation timeout leaves the owner's recall whole.

    The release decisions read the store's last commit beside the thread. When they queued behind
    the work instead, the queue refused them after the mutation timeout, and recall withheld every
    store page and said the store was unavailable.
    """
    root = service.root
    pages = _seed(root, "owner")
    server = runtime._server(root.resolve())
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        entered.set()
        release.wait(60)

    holder = threading.Thread(target=lambda: server.call(hold, wake_maintenance=False))
    holder.start()
    try:
        assert entered.wait(30), "the store thread never took the holding work"
        served = _activate(root, OWNER, turn=PHRASE + " " + TITLES)
        found, warming = _ask(root, OWNER)
        assert holder.is_alive() and not release.is_set(), "the store thread was free while recall ran"
    finally:
        release.set()
        holder.join(60)

    assert "collection_store" not in served["generation"], "recall reported the store unavailable"
    assert set(_refs(served)) & {_store_ref(index) for index in range(PAGE_COUNT)}, "recall withheld the store pages"
    assert set(pages) <= found and "collection_store" not in warming


@pytest.mark.timeout(180)
def test_a_store_handoff_delays_the_owners_recall_instead_of_withholding_it(
    service, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """A portability-export flush hands the store off for a moment; recall waits it out and stays whole.

    A handoff refuses every store checkout until it ends. A release decision that met that refusal
    withheld every store page and reported the store unavailable, although the store was back a
    second later.
    """
    root, manager = service.root, service.manager
    pages = _seed(root, "owner")
    flushing = threading.Event()
    real_flush = runtime.CollectionStoreRuntime.flush

    def slow_flush(self, *args, **kwargs):
        flushing.set()
        time.sleep(1.5)
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(runtime.CollectionStoreRuntime, "flush", slow_flush)
    exporter = threading.Thread(target=lambda: manager._release_collection_store(
        manager._fencing_token, deadline=time.monotonic() + 30, release=False))
    exporter.start()
    try:
        assert flushing.wait(30), "the export flush never started"
        found, warming = _ask(root, OWNER)
    finally:
        exporter.join(60)

    assert set(pages) <= found, "recall withheld the store pages during the handoff"
    assert "collection_store" not in warming
