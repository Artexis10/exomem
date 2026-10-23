"""The map and the domain maps, read through `read_memory` by an owner and by a restricted caller.

The workflow under test is an agent reading `exomem://map` or a domain map and
paging through it. The checks below name what only a real vault catches: a
withheld page that moves a byte a restricted caller receives (its order, its
count, its generation, a token), a page boundary that drops or repeats an
entry, a token that works for the wrong view or caller, and a forged item that
names a page the caller may not read.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURE_VAULT, initialize_vault_state_offline
from test_activate_context_surface import _rest_client, _run_cli
from test_context_catalogue import INSIGHTS, KB, _built, _page, _seed, _write
from test_governance_egress import _external, _reset_caches, write_rule
from test_working_set_withheld_twins import _withhold

from exomem import commands
from exomem import context_catalogue as cc
from exomem import context_views as cv
from exomem.activation_result import INDEX_WARMING
from exomem.governance import egress, principal
from exomem.writer_lease import invoke_command

THREADS = f"{KB}/Sources/Episodes"
THREAD_R1 = f"{THREADS}/berth-planning-ep0123456789ab-20260910t090000000000-aaaaaaaa.md"
THREAD_W = f"{THREADS}/berth-planning-ep0123456789ab-20261001t090000000000-bbbbbbbb.md"
SECRET = f"{KB}/Notes/Restricted/secret-cluster.md"
MOORING = f"{INSIGHTS}/mooring-lines.md"
TIDE = f"{INSIGHTS}/tide-work.md"
CREW = f"{INSIGHTS}/crew-log.md"
LOOSE = f"{INSIGHTS}/loose-end.md"
OLD = f"{INSIGHTS}/old-berths.md"
OLD_MOORING = f"{INSIGHTS}/old-moorings.md"
ROUTE_HUB = f"{INSIGHTS}/route-hub.md"

OWNER = principal.owner_principal(surface="library")
REFS = (cv.MAP_REF, cv.topic_ref("harbour"), cv.UNFILED_REF)


def _frozen(monkeypatch: pytest.MonkeyPatch, *, page_size: int | None = None) -> None:
    """The clock a token is minted against, and the key it is signed with, are the same in every vault."""
    monkeypatch.setattr(cv, "_clock", lambda: 1_800_000_000.0)
    monkeypatch.setattr(cc.ContextCatalogue, "secret", lambda self: b"shared-test-key")
    if page_size is not None:
        monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", page_size)


def _world(vault: Path, *, with_w: bool) -> None:
    """A small harbour: four visible pages, a thread, and (when `with_w`) the thread's newest revision."""
    _write(
        vault,
        f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\n"
        "domains:\n"
        "  harbour: {label: Harbour, path_label: Harbour, description: Quays and moorings.}\n"
        "  fleet: {label: Fleet, path_label: Fleet}\n"
        "  ledger: {label: Ledger, path_label: Ledger}\n",
    )
    _write(vault, f"{KB}/_Schema/project-keys.yaml", "projects:\n  harbour-ops: {folder: Harbour Ops, category: domain, domain: harbour}\n")
    _page(vault, "mooring-lines", "topics: [harbour]\nupdated: 2026-08-01\nsummary: How the quay lines are rated.\n")
    _page(vault, "tide-work", "project: harbour-ops\nupdated: 2026-09-01\n")
    link = THREAD_W.removesuffix(".md")
    _page(vault, "crew-log", "updated: 2026-09-05\nsummary: Who crews which berth.\n", body=f"## Relations\n\n- member_of [[{link}]]\n")
    _page(vault, "loose-end", "updated: 2026-07-01\n")
    _write(vault, OLD, "---\ntype: insight\nstatus: archived\nupdated: 2026-06-01\n---\n\n# old-berths\n\nBerths no longer in use.\n")
    _write(vault, THREAD_R1, "---\ntype: source\nstatus: active\nupdated: 2026-09-10\ntopics: [harbour]\n---\n\n# Berth recap one\n\nFirst recap.\n")
    if with_w:
        _write(
            vault,
            THREAD_W,
            "---\ntype: source\nstatus: active\nupdated: 2026-10-01\ntopics: [harbour, fleet]\n---\n\n# Berth recap two\n\nSecond recap.\n",
        )


def _cold() -> None:
    """The caches that a second, differently-scoped caller must not inherit."""
    _reset_caches()


def _ask(vault: Path, ref: str, caller: Any = None, **options: Any) -> dict[str, Any]:
    """One read through the command path and the terminal egress filter, as `caller` (default: the owner)."""
    with principal.request_scope(caller or OWNER):
        result = commands.op_read_memory(vault, path=ref, **options)
        return egress.postfilter("read_memory", result, vault, request_kwargs={"path": ref, **options})


def _pages(vault: Path, ref: str, caller: Any = None) -> list[dict[str, Any]]:
    """Every page of a view, following continuations to the end."""
    pages: list[dict[str, Any]] = []
    token = None
    for _ in range(300):  # a view that never ends is a failure, not a hang
        pages.append(_ask(vault, ref, caller, continuation=token))
        token = pages[-1]["continuation"]
        if token is None:
            return pages
    raise AssertionError(f"{ref} did not end after {len(pages)} pages")


def _refs(pages: list[dict[str, Any]]) -> list[str]:
    return [item["ref"] for page in pages for item in page["items"]]


def _outcome(vault: Path, ref: str, caller: Any = None) -> object:
    """The pages of a view, or the refusal text when the caller has none."""
    try:
        return _pages(vault, ref, caller)
    except ValueError as error:
        return str(error)


def _second_vault(tmp_path: Path) -> Path:
    twin = tmp_path / "twin"
    shutil.copytree(FIXTURE_VAULT, twin)
    initialize_vault_state_offline(twin, source="twin of the fixture vault")
    return twin


def _leave_pending(vault: Path, rel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Change a page, then change it again while the catalogue folds it, so its path stays pending."""
    absolute = vault / rel
    absolute.write_text(absolute.read_text(encoding="utf-8") + "\nAdded before the fold.\n", encoding="utf-8")
    read = cc._read_page

    def read_then_rewrite(path: Path, root: Path):
        page = read(path, root)
        path.write_text(path.read_text(encoding="utf-8") + "\nAdded after the parse.\n", encoding="utf-8")
        return page

    with monkeypatch.context() as patch:
        patch.setattr(cc, "_read_page", read_then_rewrite)
        cc.ContextCatalogue(vault).update([rel])
    assert rel in cc.ContextCatalogue(vault).pending()


def _warm_catalogue(monkeypatch: pytest.MonkeyPatch, *, unlisted: bool = False) -> None:
    """The catalogue answers as a warm managed runtime does, without folding what is pending."""
    monkeypatch.setattr(cc, "ensure_unlisted", lambda root, freshness_stamp="": (cc.READY, cc.ContextCatalogue(root), unlisted))


# --------------------------------------------------------------------------- #
# Counterfactual twins
# --------------------------------------------------------------------------- #


def test_a_withheld_page_changes_no_byte_a_restricted_caller_receives(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W is the newest member of `fleet` and `harbour`, the target of a visible note's `member_of` edge, and the
    newest revision of a visible thread. One vault holds W, withheld; its twin never had it."""
    _frozen(monkeypatch, page_size=2)
    twin = _second_vault(tmp_path)
    _world(vault, with_w=True)
    _world(twin, with_w=False)
    for root in (vault, twin):
        _withhold(root, THREAD_W, ceiling=0)
        _built(root)

    def read_everything(root: Path) -> tuple[dict[str, object], dict[str, object], tuple[tuple[int, ...], str], int]:
        owner = {ref: _outcome(root, ref) for ref in (*REFS, cv.topic_ref("fleet"))}  # the owner first, to warm
        _cold()
        guest = {ref: _outcome(root, ref, _external()) for ref in (*REFS, cv.topic_ref("fleet"))}
        with principal.request_scope(_external()):
            view = cv.compile_view(root, cv.MAP_REF)
        return owner, guest, cv.map_within(view, 700), len(view.rows)

    owner_with, guest_with, lines_with, rows_with = read_everything(vault)
    owner_without, guest_without, lines_without, rows_without = read_everything(twin)

    assert json.dumps(guest_with) == json.dumps(guest_without)
    assert (lines_with, rows_with) == (lines_without, rows_without)
    # The pages are real: the map spans several tokens, and `fleet` has no map for a caller who sees no member.
    assert len(guest_with[cv.MAP_REF]) > 1 and guest_with[cv.MAP_REF][0]["continuation"]
    assert guest_with[cv.topic_ref("fleet")] == "NOT_FOUND: memory identifier is unavailable"
    # The twin is not vacuous: the owner, who may read W, receives a different map.
    assert json.dumps(owner_with) != json.dumps(owner_without)
    assert isinstance(owner_with[cv.topic_ref("fleet")], list)


def test_pending_work_marks_a_view_stale_only_for_a_caller_who_may_see_the_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(vault, with_w=True)
    _withhold(vault, THREAD_W, ceiling=0)
    _built(vault)
    _warm_catalogue(monkeypatch)
    baseline = _ask(vault, cv.MAP_REF, _external())
    assert baseline["stale"] is False

    _leave_pending(vault, THREAD_W, monkeypatch)
    assert _ask(vault, cv.MAP_REF)["stale"] is True  # the owner may see W
    assert _ask(vault, cv.MAP_REF, _external()) == baseline  # a caller who may not sees no change at all

    cc.ContextCatalogue(vault).update()
    _leave_pending(vault, MOORING, monkeypatch)
    assert _ask(vault, cv.MAP_REF, _external())["stale"] is True

    # Work the catalogue cannot list says nothing about which pages it touches: a restricted caller waits.
    cc.ContextCatalogue(vault).update()
    _warm_catalogue(monkeypatch, unlisted=True)
    waiting = _ask(vault, cv.MAP_REF, _external())
    assert (waiting["state"], waiting["items"], waiting["stale"]) == (INDEX_WARMING, [], False)
    assert _ask(vault, cv.MAP_REF)["stale"] is True


# --------------------------------------------------------------------------- #
# Fit, paging and reach
# --------------------------------------------------------------------------- #


def test_map_within_returns_whole_lines_that_fit_and_name_the_domain_maps(vault: Path) -> None:
    _world(vault, with_w=True)
    _built(vault)
    with principal.request_scope(OWNER):
        view = cv.compile_view(vault, cv.MAP_REF)
    order = [row.item().get("profile", row.ref) if not row.path else row.ref for row in view.rows]

    for budget in (400, 700, 1500, 100_000):
        indices, overflow = cv.map_within(view, budget)
        lines = [*(cv.map_item_line(cv._map_item(view, view.rows[index])) for index in indices), overflow]
        assert overflow.startswith("Not shown: ") and cv.UNFILED_REF in overflow
        assert len("\n".join(lines)) <= budget
        shown = [line.rpartition("[")[2].removesuffix("]") for line in lines[:-1]]
        assert all(ref in order for ref in shown)  # each line is a whole row, not a cut one
        assert shown == [ref for ref in order if ref in shown]  # in map order
        assert lines[-1].startswith(f"Not shown: {len(order) - len(shown)}.")
    assert cv.map_within(view, 100_000)[-1].startswith("Not shown: 0.")
    assert cv.map_within(view, 20) == ((), "")  # not even the closing line fits


def test_paging_the_map_returns_every_entry_exactly_once(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(vault, with_w=True)
    _built(vault)
    for ref in (cv.MAP_REF, cv.UNFILED_REF):
        monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 1000)
        whole = _refs(_pages(vault, ref))
        for size in (1, 3, 5):  # one that does not divide the view, one that may, and the smallest
            monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", size)
            assert _refs(_pages(vault, ref)) == whole
        assert len(set(whole)) == len(whole) > 5


def test_a_retired_note_with_no_topic_is_reachable_only_through_the_unfiled_map(vault: Path) -> None:
    _world(vault, with_w=True)
    _write(vault, OLD_MOORING, "---\ntype: insight\nstatus: archived\ntopics: [harbour]\nupdated: 2026-05-01\n---\n\n# old-moorings\n\nGone.\n")
    _built(vault)

    on_map = {item["path"] for page in _pages(vault, cv.MAP_REF) for item in page["items"] if "path" in item}
    unfiled = {item["path"]: item for page in _pages(vault, cv.UNFILED_REF) for item in page["items"] if "path" in item}
    harbour = {item["path"]: item for page in _pages(vault, cv.topic_ref("harbour")) for item in page["items"] if "path" in item}

    assert OLD not in on_map and OLD_MOORING not in on_map and LOOSE not in on_map
    assert unfiled[OLD]["lifecycle_class"] == "retired"
    assert harbour[OLD_MOORING]["lifecycle_class"] == "retired"  # a domain map carries every lifecycle class


# --------------------------------------------------------------------------- #
# The read contract
# --------------------------------------------------------------------------- #


def test_every_continuation_outcome_follows_the_contract(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    _built(vault)
    monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 2)
    now = [1_800_000_000.0]
    monkeypatch.setattr(cv, "_clock", lambda: now[0])
    token = _ask(vault, cv.MAP_REF)["continuation"]
    other_view = _ask(vault, cv.UNFILED_REF)["continuation"]
    assert token and other_view
    mac_flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    plain_miss = _outcome(vault, "exomem://profile/topic/no-such-domain")

    def read(ref: str, **options: Any) -> Any:
        """The response, or the refusal's code."""
        try:
            return _ask(vault, ref, **options)
        except ValueError as error:
            return str(error).partition(":")[0]

    assert read(cv.MAP_REF, continuation=token)["items"]  # the control: an honest token pages on
    assert read(cv.MAP_REF, continuation=mac_flipped) == "INVALID_CONTINUATION"
    assert read(cv.MAP_REF, continuation="not a token") == "INVALID_CONTINUATION"
    assert read(cv.MAP_REF, continuation=other_view) == "INVALID_CONTINUATION"
    assert read(cv.UNFILED_REF, continuation=token) == "INVALID_CONTINUATION"
    assert read(cv.MAP_REF, continuation=token, caller=_external()) == "INVALID_CONTINUATION"
    assert read(f"{INSIGHTS}/mooring-lines.md", continuation=token) == "INVALID_CONTINUATION"
    for option in ({"unit_ref": "u"}, {"frontmatter_only": True}, {"links": True}, {"include_raw": True}, {"include_history": True}):
        assert read(cv.MAP_REF, **option) == "INVALID_VIEW_READ_OPTIONS", option
    # A ref that names no view reads exactly as an identifier that names nothing does.
    with pytest.raises(ValueError) as miss:
        _ask(vault, "exomem://map/topic/no-such-domain")
    assert str(miss.value) == plain_miss
    assert read("exomem://map/other") == "NOT_FOUND"
    # A deprecated domain key reads the map of the key that replaced it.
    assert read(cv.topic_ref("old-harbour"))["ref"] == cv.topic_ref("harbour")

    now[0] += cv.CONTINUATION_TTL_SECONDS + 1
    assert read(cv.MAP_REF, continuation=token) == "INVALID_CONTINUATION"
    now[0] -= cv.CONTINUATION_TTL_SECONDS + 1

    _page(vault, "mooring-lines", "topics: [harbour]\nsummary: Rewritten after the token was issued.\nupdated: 2026-12-01\n")
    assert read(cv.MAP_REF, continuation=token) == "CONTINUATION_STALE"
    assert _ask(vault, cv.MAP_REF)["continuation"]  # reading again from the first page works


def test_the_cli_and_rest_return_the_bytes_the_command_returns(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(vault, with_w=True)
    _built(vault)
    _frozen(monkeypatch, page_size=3)
    client = _rest_client(monkeypatch)
    first = _ask(vault, cv.MAP_REF)
    second = _ask(vault, cv.MAP_REF, continuation=first["continuation"])

    code, out = _run_cli(["read_memory", cv.MAP_REF, "--json"], capsys)
    assert code == 0, out
    cli_first = json.loads(out)["data"]
    response = client.post(
        "/api/read_memory",
        json={"path": cv.MAP_REF, "continuation": first["continuation"]},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 200, response.text

    assert json.dumps(cli_first, sort_keys=True) == json.dumps(first, sort_keys=True)
    assert json.dumps(response.json()["data"], sort_keys=True) == json.dumps(second, sort_keys=True)


# --------------------------------------------------------------------------- #
# Egress
# --------------------------------------------------------------------------- #


def test_the_guard_drops_a_forged_view_item_that_names_a_withheld_page(vault: Path) -> None:
    """A real page of a view, with a forged item beside its real ones. Only the forged one goes."""
    _world(vault, with_w=True)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\nupdated: 2026-10-05\n---\n\n# secret-cluster\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    _built(vault)
    harbour = cv.topic_ref("harbour")
    _cold()
    with principal.request_scope(_external()):
        page = commands.op_read_memory(vault, path=harbour)
        forged = {**page, "items": [*page["items"], {"path": SECRET, "ref": SECRET, "title": "Forged"}]}
        guarded = egress.postfilter("read_memory", forged, vault, request_kwargs={"path": harbour})

    assert guarded["items"] == page["items"] and TIDE in [item["ref"] for item in guarded["items"]]
    assert "secret-cluster" not in json.dumps(guarded)


def test_a_forged_description_leaves_the_guard_as_the_entrys_own(vault: Path) -> None:
    """A real item carries a description its entry never had, naming a withheld page by path and by title.

    The guard serves the entry's own description instead, and keeps the item: scanning the text
    for withheld names could only drop it when the named page exists.
    """
    _world(vault, with_w=True)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\nupdated: 2026-10-05\n---\n\n# Secret cluster\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    _built(vault)
    harbour = cv.topic_ref("harbour")
    _cold()
    with principal.request_scope(_external()):
        page = commands.op_read_memory(vault, path=harbour)
        forged = {
            **page,
            "items": [
                {**item, "title": "Secret cluster", "description": f"Read {SECRET}, the Secret cluster page.",
                 "profile": cv.anchor_profile_ref(SECRET)}
                if item.get("path") == MOORING
                else item
                for item in page["items"]
            ],
        }
        guarded = egress.postfilter("read_memory", forged, vault, request_kwargs={"path": harbour})

    assert guarded["items"] == page["items"]
    assert next(item for item in guarded["items"] if item.get("path") == MOORING)["description"] == "How the quay lines are rated."


@pytest.mark.parametrize(
    "linked",
    [
        f"summary: Read [[{THREAD_W.removesuffix('.md')}]] first.\n",
        f"title: Route via [[{THREAD_W.removesuffix('.md')}]]\n",
    ],
    ids=["in its description", "in its title"],
)
def test_a_link_to_a_withheld_page_reads_the_same_whether_or_not_the_page_exists(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    """A visible hub links to W. One vault holds W, withheld; its twin never had it."""
    _frozen(monkeypatch)
    twin = _second_vault(tmp_path)
    for root, with_w in ((vault, True), (twin, False)):
        _world(root, with_w=with_w)
        _page(root, "route-hub", f"tags: [hub]\ntopics: [harbour]\nupdated: 2026-09-20\n{linked}")
        _withhold(root, THREAD_W, ceiling=0)
        _built(root)

    guests = []
    for root in (vault, twin):
        _cold()
        guests.append({ref: _outcome(root, ref, _external()) for ref in REFS})

    assert json.dumps(guests[0]) == json.dumps(guests[1])
    assert any(item.get("path") == ROUTE_HUB for page in guests[0][cv.MAP_REF] for item in page["items"])


def test_a_state_that_links_to_a_withheld_page_reads_the_same_whether_or_not_the_page_exists(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visible anchor's state links to W. One vault holds W, withheld; its twin never had it."""
    _frozen(monkeypatch)
    twin = _second_vault(tmp_path)
    link = THREAD_W.removesuffix(".md")
    for root, with_w in ((vault, True), (twin, False)):
        _world(root, with_w=with_w)
        quay = _page(
            root, "quay-system", f'topics: [harbour]\nupdated: 2026-09-20\nsummary: The quay system.\nstate: "berthed near [[{link}]]"\n',
            folder=f"{KB}/Products",
        )
        _withhold(root, THREAD_W, ceiling=0)
        _built(root)

    guests = []
    for root in (vault, twin):
        _cold()
        guests.append(_pages(root, cv.MAP_REF, _external()))

    assert json.dumps(guests[0]) == json.dumps(guests[1])
    state = next(item["state"] for page in guests[0] for item in page["items"] if item.get("path") == quay)
    assert state["statement"] == "state: berthed near"


def test_the_terminal_guard_decides_under_the_purpose_the_request_declared(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W is withheld only for `marketing`. The compile is made to ignore the caller's view, so the terminal
    guard alone stands between W and the wire, and it must decide under the purpose the request declared."""
    _world(vault, with_w=True)
    _withhold(vault, THREAD_W, ceiling=0)
    write_rule(vault, ceiling=0, extra="purpose: marketing\npurpose_condition: matches\n")
    _cold()
    _built(vault)
    compile_world = cv._world
    monkeypatch.setattr(
        cv, "_world", lambda catalogue, caller: compile_world(catalogue, dataclasses.replace(caller, visible=None))
    )
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")

    with principal.request_scope(_external()):
        undeclared = invoke_command(command, vault, path=cv.topic_ref("harbour"))
        declared = invoke_command(command, vault, path=cv.topic_ref("harbour"), purpose="marketing")

    assert THREAD_W in json.dumps(undeclared)  # the control: with no purpose declared, W is this caller's to read
    for needle in (THREAD_W, "bbbbbbbb", "Berth recap two"):
        assert needle not in json.dumps(declared), needle


def test_a_caller_who_may_not_read_the_domain_registry_learns_nothing_from_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry's overlay is withheld from the caller, so `schema_memory` refuses it. So does every view."""
    _world(vault, with_w=True)
    _withhold(vault, THREAD_W, f"{KB}/_Schema/source-taxonomy.yaml", ceiling=0)
    _cold()
    _built(vault)
    with principal.request_scope(_external()):
        assert commands.op_schema_memory(vault, operation="inspect", subject="domains")["available"] is False
    registered, unknown = cv.topic_ref("harbour"), cv.topic_ref("no-such-domain")

    with monkeypatch.context() as patch:
        patch.setattr(cc, "ensure_unlisted", lambda root, freshness_stamp="": (cc.WARMING, None, False))
        assert _outcome(vault, registered, _external()) == _outcome(vault, unknown, _external())  # while warming
    assert _outcome(vault, registered, _external()) == _outcome(vault, unknown, _external())  # and when ready
    pages = _pages(vault, cv.MAP_REF, _external())
    items = [item for page in pages for item in page["items"]]
    assert any(item.get("path") == THREAD_R1 for item in items)  # the map itself is still served
    assert all(item["kind"] != "topic" and not item["topics"] for item in items)
    assert "Quays and moorings." not in json.dumps(pages)

    # A topic item forged into a real page leaves the guard with nothing of the registry.
    with principal.request_scope(_external()):
        page = commands.op_read_memory(vault, path=cv.MAP_REF)
        forged = {**page, "items": [*page["items"], {"kind": "topic", "ref": registered, "title": "F", "description": "F"}]}
        guarded = egress.postfilter("read_memory", forged, vault, request_kwargs={"path": cv.MAP_REF})
    assert guarded["items"] == pages[0]["items"]


def test_both_guard_passes_serve_the_text_their_generation_was_compiled_from(vault: Path) -> None:
    """The catalogue publishes an edit after the compile, and another before the second pass."""
    _world(vault, with_w=True)
    _built(vault)
    harbour = cv.topic_ref("harbour")

    def publish(summary: str) -> None:
        _page(vault, "mooring-lines", f"topics: [harbour]\nupdated: 2026-08-01\nsummary: {summary}\n")
        cc.ContextCatalogue(vault).update()

    def mooring(response: dict[str, Any]) -> dict[str, Any]:
        return next(item for item in response["items"] if item.get("path") == MOORING)

    with principal.request_scope(OWNER):
        compiled = commands.op_read_memory(vault, path=harbour)
        publish("Changed after the compile.")
        first = egress.postfilter("read_memory", compiled, vault, request_kwargs={"path": harbour})
        publish("Changed before the second pass.")
        second = egress.postfilter("read_memory", first, vault, request_kwargs={"path": harbour})

    assert mooring(first)["description"] == mooring(second)["description"] == "How the quay lines are rated."
    assert compiled["generation"] == first["generation"] == second["generation"]
    assert mooring(_ask(vault, harbour))["description"] == "Changed before the second pass."  # the edits did publish


def test_a_request_keeps_its_items_while_other_requests_record_thousands_between_its_passes(vault: Path) -> None:
    """Between this request's compile and each of its guard passes, a thousand other requests record their pages."""
    _world(vault, with_w=True)
    _built(vault)
    with principal.request_scope(OWNER):
        view = cv.compile_view(vault, cv.MAP_REF)
        owner = cv.CallerView.current(vault)

    def one_request(n: int) -> None:
        with principal.request_scope(OWNER):
            cv.served_items(vault, view, view.rows, dataclasses.replace(owner, binding=f"other-request-{n}"))

    def other_requests() -> None:
        for n in range(1000):
            contextvars.Context().run(one_request, n)  # each in a context of its own, as a server runs a request

    with principal.request_scope(_external()):
        compiled = commands.op_read_memory(vault, path=cv.MAP_REF)
        other_requests()
        first = egress.postfilter("read_memory", compiled, vault, request_kwargs={"path": cv.MAP_REF})
        other_requests()
        second = egress.postfilter("read_memory", first, vault, request_kwargs={"path": cv.MAP_REF})

    assert len(view.rows) * 1000 > 4096  # more items than any bounded shared store kept
    assert compiled["items"] and first["items"] == second["items"] == compiled["items"]


def test_a_release_strip_in_the_first_pass_does_not_change_what_the_second_serves(vault: Path) -> None:
    """A released bridge loses provenance fields in the first pass. The second pass still serves it."""
    from test_governance_bridges import _write_bridge_fixture

    bridge = f"{KB}/Products/workload-constraint.md"
    _write_bridge_fixture(vault, approval=True, source_title="resource", bridge_path=bridge)
    _built(vault)
    with principal.request_scope(_external()):
        compiled = commands.op_read_memory(vault, path=cv.MAP_REF)
        first = egress.postfilter("read_memory", compiled, vault, request_kwargs={"path": cv.MAP_REF})
        second = egress.postfilter("read_memory", first, vault, request_kwargs={"path": cv.MAP_REF})

    served = [item for item in compiled["items"] if item.get("path") == bridge]
    stripped = [item for item in first["items"] if item.get("path") == bridge]
    assert served and stripped and stripped != served  # the control: the first pass strips the bridge
    assert second["items"] == first["items"]


def test_a_withheld_project_registry_edit_moves_no_byte_a_restricted_caller_receives(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller may not read the project registry. Its project's domain is edited between two pages."""
    _frozen(monkeypatch, page_size=1)
    _world(vault, with_w=False)
    registry = f"{KB}/_Schema/project-keys.yaml"
    _withhold(vault, registry, ceiling=0)
    _cold()
    _built(vault)
    harbour = cv.topic_ref("harbour")

    def read_all(caller: Any) -> dict[str, list[dict[str, Any]]]:
        return {ref: _pages(vault, ref, caller) for ref in REFS}

    owner_before, guest_before = read_all(OWNER), read_all(_external())
    first_page = guest_before[harbour][0]
    assert first_page["continuation"]
    (vault / registry).write_text(
        (vault / registry).read_text(encoding="utf-8").replace("domain: harbour", "domain: fleet"), encoding="utf-8"
    )
    _cold()
    owner_after, guest_after = read_all(OWNER), read_all(_external())
    following = _ask(vault, harbour, _external(), continuation=first_page["continuation"])

    # The control: the owner files tide-work through the project's domain, and sees the project anchor.
    assert TIDE in _refs(owner_before[harbour]) and TIDE not in _refs(owner_after[harbour])
    assert "Harbour Ops" in json.dumps(owner_before)
    # The guest gets nothing from the registry, so the edit moves nothing it receives.
    assert TIDE not in _refs(guest_before[harbour]) and TIDE in _refs(guest_before[cv.UNFILED_REF])
    assert "Harbour Ops" not in json.dumps(guest_before)
    assert json.dumps(guest_before) == json.dumps(guest_after)
    assert following == guest_before[harbour][1]


def test_a_state_change_moves_the_generation_and_stales_an_old_continuation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the anchor's `state` changes: its summary and its `updated` date stay as they were."""
    _world(vault, with_w=True)

    def anchor(state: str) -> str:
        return _page(
            vault, "quay-system", f"topics: [harbour]\nupdated: 2026-09-20\nsummary: The quay system.\nstate: {state}\n",
            folder=f"{KB}/Products",
        )

    path = anchor("ready")
    _built(vault)
    monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 1)

    def state_now() -> tuple[str, str]:
        pages = _pages(vault, cv.MAP_REF)
        served = next(item["state"] for page in pages for item in page["items"] if item.get("path") == path)
        return served["statement"], pages[0]["generation"]

    first = _ask(vault, cv.MAP_REF)
    before, generation_before = state_now()
    anchor("paused")
    after, generation_after = state_now()

    assert (before, after) == ("state: ready", "state: paused")
    assert generation_after != generation_before
    with pytest.raises(ValueError, match="^CONTINUATION_STALE"):
        _ask(vault, cv.MAP_REF, continuation=first["continuation"])


def test_an_empty_unfiled_map_is_ready_with_no_items(vault: Path) -> None:
    """Every entry has a topic, so the unfiled map names a projection with nothing in it."""
    for page in list(vault.rglob("*.md")):
        page.unlink()
    _write(vault, f"{KB}/_Schema/source-taxonomy.yaml", "schema_version: 1\ndomains:\n  harbour: {label: Harbour, path_label: Harbour}\n")
    _write(vault, f"{KB}/_Schema/project-keys.yaml", "projects:\n  harbour-ops: {folder: Harbour Ops, category: domain, domain: harbour}\n")
    _page(vault, "mooring-lines", "topics: [harbour]\nupdated: 2026-08-01\n")
    _built(vault)

    for caller in (OWNER, _external()):
        _cold()
        unfiled = _ask(vault, cv.UNFILED_REF, caller)
        assert (unfiled["ref"], unfiled["state"], unfiled["items"], unfiled["continuation"]) == (cv.UNFILED_REF, "ready", [], None)
        assert unfiled["generation"]


# --------------------------------------------------------------------------- #
# The whole workflow
# --------------------------------------------------------------------------- #


def test_each_view_pages_to_exhaustion_for_the_owner_and_for_a_restricted_caller(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(vault, with_w=True)
    _write(
        vault,
        SECRET,
        "---\ntype: insight\nstatus: active\ntopics: [harbour, ledger]\nupdated: 2026-10-05\n"
        "summary: Zebra codeword for the hidden ledger.\n---\n\n# secret-cluster\n\nHidden.\n",
    )
    _withhold(vault, THREAD_W, SECRET, ceiling=0)
    _built(vault)
    monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 2)
    refs = (cv.MAP_REF, cv.topic_ref("harbour"), cv.UNFILED_REF)
    mine = {THREAD_R1, THREAD_W, MOORING, TIDE, CREW, LOOSE, OLD, SECRET}

    def read(caller: Any) -> dict[str, list[dict[str, Any]]]:
        _cold()
        return {ref: _pages(vault, ref, caller) for ref in refs}

    owner, guest = read(OWNER), read(_external())

    for caller, views in ((OWNER, owner), (_external(), guest)):
        for ref, pages in views.items():
            seen = _refs(pages)
            assert len(seen) == len(set(seen)), ref  # no entry twice
            assert len(pages) > 1 and all(len(page["items"]) <= 2 for page in pages), ref
            assert pages[-1]["continuation"] is None and len({page["generation"] for page in pages}) == 1, ref
            monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 1000)
            assert seen == _refs(_pages(vault, ref, caller)), ref  # paged, it is the whole view in the same order
            monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 2)

    def mine_in_order(pages: list[dict[str, Any]]) -> list[str]:
        return [item["ref"] for page in pages for item in page["items"] if item["ref"] in mine]

    # The restricted caller's order, derived from the spec: newest first, W and the secret page absent.
    assert mine_in_order(guest[cv.topic_ref("harbour")]) == [THREAD_R1, TIDE, MOORING]
    assert mine_in_order(guest[cv.UNFILED_REF]) == [CREW, LOOSE, OLD]
    assert [item["ref"] for page in guest[cv.MAP_REF] for item in page["items"] if item["kind"] == "topic"] == [cv.topic_ref("harbour")]
    # The owner may read both: the secret page leads `harbour`, W leads the thread, and `crew-log` joins through W.
    assert mine_in_order(owner[cv.topic_ref("harbour")])[:3] == [SECRET, THREAD_W, CREW]
    assert [item["ref"] for page in owner[cv.MAP_REF] for item in page["items"] if item["kind"] == "topic"][:3] == [
        cv.topic_ref("harbour"),
        cv.topic_ref("ledger"),
        cv.topic_ref("fleet"),
    ]

    everything = json.dumps(guest)
    for needle in (THREAD_W, "bbbbbbbb", "Berth recap two", SECRET, "secret-cluster", "Zebra codeword", cv.topic_ref("ledger"), cv.topic_ref("fleet")):
        assert needle not in everything, needle
    assert THREAD_W in json.dumps(owner)  # the control: the owner does receive it
