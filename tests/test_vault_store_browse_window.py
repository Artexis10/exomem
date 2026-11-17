"""A store-mode browse reads an ordered window of the store and answers as the exhaustive browse does.

OpenSpec make-vault-store-canonical, task 3.2 (browse latency budget). The window is an
optimisation, so every case compares its answer with the exhaustive route's (the window declined,
the way a store without the browse index declines it) and with file mode on the same pages.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_connector_boundary import configured_boundary  # noqa: F401
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_browse_drill import CELLS as DRILL_CELLS
from test_vault_store_browse_drill import HUB_LINKS, commit_drill_pages, run_probe
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401

from exomem import commands, lexstore, readiness, structured_filters
from exomem import find as find_module
from exomem.cli_ops import OpError
from exomem.governance import egress
from exomem.governance.principal import request_scope

NOTES = "Knowledge Base/Notes/Browse"
OPEN = "Knowledge Base/Notes/Open"
HUB = f"{NOTES}/Hub.md"
SECRET_FRAME_TEXT = "confidential frame excerpt"


def page(title, *, kind="note", updated=None, body=None, **frontmatter):
    lines = ["---", f"title: {title}", f"type: {kind}"]
    if updated:
        lines.append(f"updated: {updated}")
    lines += [f"{key}: {value}" for key, value in frontmatter.items()]
    lines += ["---", f"# {title}", "", body or f"A note about {title}.", ""]
    return "\n".join(lines).encode()


def video(stem, updated):
    return f"{stem}.md", page(f"Clip {stem}", kind="source", updated=updated, evidence_file=stem)


def frame(parent, name, ts, updated, *, parent_media=None, **kwargs):
    """A scene-frame child, reported as its parent video when the parent can be reported."""
    return f"{parent}.frames/{name}.jpg.md", page(
        f"Frame {name}",
        kind=kwargs.pop("kind", "source"),
        updated=updated,
        parent_media=parent_media or parent,
        evidence_file=f"{parent}.frames/{name}.jpg",
        frame_ts=ts,
        **kwargs,
    )


def plain_pages():
    """Ties on `updated`, no `updated`, non-ASCII names, a hub that ten pages link to, and units."""
    pages = {}
    for index in range(24):
        unit = "\n- [config] The lantern oil is trimmed #lamp ^oil\n" if index in (3, 4, 5) else ""
        pages[f"{NOTES}/Plain-{index:02d}.md"] = page(
            f"Plain {index}",
            kind=("note", "insight")[index % 2],
            updated=f"2026-03-0{1 + index % 4}",
            body=("It follows [[Hub]]." if index < 10 else f"The lamp {index} burns.") + unit,
        )
    pages[HUB] = page("Hub", updated="2026-03-02")
    pages[f"{NOTES}/Café.md"] = page("Cafe", updated="2026-03-04")
    pages[f"{NOTES}/日本語.md"] = page("Nihongo", updated="2026-03-04")
    pages[f"{NOTES}/Undated.md"] = page("Undated")
    for index in range(6):  # older than every page above, so a withheld folder is walked past
        pages[f"{OPEN}/Open-{index}.md"] = page(
            f"Open {index}", kind=("note", "insight")[index % 2], updated=f"2026-01-0{1 + index}"
        )
    return pages


def clean_families():
    """Scene-frame parents with children newer and older than the parent, a withheld child and an orphan."""
    one, two, six = (f"{NOTES}/Clip-{name}.mp4" for name in ("One", "Two", "Six"))
    pages = dict(
        [
            video(one, "2026-03-02"),
            frame(one, "scene-000-005.000", 5.0, "2026-03-04"),  # newer than its parent
            frame(one, "scene-001-010.000", 10.0, "2026-02-01"),  # older than its parent
            video(two, "2026-03-03"),
            frame(two, "scene-000-001.000", 1.0, "2026-03-04", body=SECRET_FRAME_TEXT),
            frame(two, "scene-001-002.000", 2.0, "2026-02-15"),
            # The child matches no type that the parent does, so only the parent's columns admit it.
            video(six, "2026-03-01"),
            frame(six, "scene-000-007.000", 7.0, "2026-03-04", kind="insight"),
            # No page exists for this child's parent, so the child is reported as itself.
            frame(f"{NOTES}/Orphan.mp4", "scene-000-001.000", 1.0, "2026-03-03"),
        ]
    )
    return pages


#: The tree that `ACCESS_POLICY` excludes: a page in it is no recall candidate, so a child that names
#: one as its parent is reported as itself although the catalogue groups it under that parent.
EXCLUDED = "Notes/Hidden"
ACCESS_POLICY = {"Knowledge Base/_access.yaml": f"excluded:\n  - {EXCLUDED}/\n".encode()}


def hostile_families():
    """A parent outside the knowledge base and a parent in a tree that the access policy excludes."""
    outside, excluded = "Elsewhere/Clip-Three.mp4", f"Knowledge Base/{EXCLUDED}/Clip-Five.mp4"
    return dict(
        [
            video(outside, "2026-03-03"),
            frame(
                f"{NOTES}/Clip-Three.mp4",
                "scene-000-003.000",
                3.0,
                "2026-03-02",
                parent_media=outside,
            ),
            video(excluded, "2026-03-03"),
            frame(
                f"{NOTES}/Clip-Five.mp4",
                "scene-000-004.000",
                4.0,
                "2026-03-02",
                parent_media=excluded,
            ),
        ]
    )


def write_files(root, pages):
    for rel, body in pages.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)


#: The browse cells. A unit-axis plan has no file-mode counterpart here: an offline caller answers
#: it from a sidecar that this vault has not built for the new pages.
CELLS = {
    "no filter": {},
    "types=note": {"types": ["note"]},
    "types=insight": {"types": ["insight"]},
    "types=source": {"types": ["source"]},
    "relation_of hub": {"relation_of": HUB},
    "relation_of hub, types=note": {"relation_of": HUB, "types": ["note"]},
    "categories=config": {"categories": ["config"]},
}
LIMITS = (3, 7, 100)

SECRET_FRAME = f"{NOTES}/Clip-Two.mp4.frames/scene-000-001.000.jpg.md"
#: The project that `configured_boundary` denies its limited client: a page in it is withheld by a
#: content floor, which is what a browse's admitted set decides.
PRIVATE_PROJECT = "private-project"
#: The connector client that reads each world.
CLIENTS = {"owner": "full", "few withheld": "limited", "most withheld": "limited"}


def withheld_pages(world):
    """The pages that the limited client is denied: a few, or most, with a withheld parent."""
    if world == "owner":
        return set()
    few = {f"{NOTES}/Plain-02.md", f"{NOTES}/Plain-13.md", SECRET_FRAME}
    if world == "few withheld":
        return few
    # A withheld parent hides its children, whatever their own verdict.
    return (
        few
        | {f"{NOTES}/Plain-{index:02d}.md" for index in range(4, 24)}
        | {f"{NOTES}/Clip-Six.mp4.md"}
    )


def in_private_project(body):
    return body.replace(b"\n---\n#", f"\nproject: {PRIVATE_PROJECT}\n---\n#".encode(), 1)


def browse(root, caller, limit, cell):
    """The hits of one browse as plain data, or the refusal it ends in."""
    find_module.clear_cache()
    with caller():
        try:
            hits = find_module.find(
                root, query="", limit=limit, graph=False, rerank=False, mode="keyword", **cell
            )
        except OpError as refused:
            return ("refused", refused.code)
    return [asdict(hit) for hit in hits]


#: Scene-frame parents. File mode takes a parent's excerpt and frame from the first of its pages in
#: directory-listing order (`walk_vault_md` does not sort), the store from the newest, so the two
#: agree on a parent's path, title and date and not on which child it shows.
FAMILY_PARENTS = {rel.split(".frames/")[0] + ".md" for rel in clean_families() if ".frames/" in rel}


def file_view(answer):
    """What file mode and store mode must agree on: the page and its date, and its excerpt and
    scene frame where one page decides them."""
    if isinstance(answer, tuple):
        return answer
    return [
        (hit["path"], hit["title"], hit["updated"])
        + (
            ()
            if hit["path"] in FAMILY_PARENTS
            else (hit["excerpt"], hit["scene_frame"], hit["scene_frame_ts"])
        )
        for hit in answer
    ]


@pytest.fixture
def routes(monkeypatch):
    """Which route answered each browse: the exhaustive route reads the whole scope's rows first."""
    exhaustive = []
    real = find_module._index_resolved_scope_rows

    def counting(*args, **kwargs):
        exhaustive.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(find_module, "_index_resolved_scope_rows", counting)
    return exhaustive


def declined_window(monkeypatch):
    """The browse index reads as missing, which is how the window declines for an older store."""
    monkeypatch.setattr(lexstore, "BROWSE_INDEX", "pages_browse_declined")


def answers(root, caller):
    found = {}
    for limit in LIMITS:
        for name, cell in CELLS.items():
            found[(limit, name)] = browse(root, caller, limit, cell)
    return found


def build_world(vault, authenticate, name, families):  # noqa: F811
    withheld = withheld_pages(name)
    pages = {**plain_pages(), **families}
    write_files(
        vault,
        {rel: in_private_project(body) if rel in withheld else body for rel, body in pages.items()},
    )
    owner, limited = authenticate("full"), authenticate("limited")
    who = owner if CLIENTS[name] == "full" else limited
    caller = lambda: request_scope(who)  # noqa: E731
    build_indexes(vault)
    file_mode = {
        key: file_view(answer)
        for key, answer in answers(vault, caller).items()
        if key[1] != "categories=config"
    }
    assert migrate_apply(vault) == 0
    return SimpleNamespace(
        name=name,
        root=vault,
        caller=caller,
        owner=lambda: request_scope(owner),
        withheld=withheld,
        file_mode=file_mode,
    )


@pytest.fixture(params=CLIENTS)
def world(request, vault, configured_boundary):  # noqa: F811
    return build_world(vault, configured_boundary[1], request.param, clean_families())


def test_the_window_answers_as_the_exhaustive_browse_and_file_mode_do(world, routes, monkeypatch):
    """A window that orders, groups or admits a page differently from the exhaustive browse: ties on
    `updated`, a page with no `updated`, non-ASCII names, children newer and older than their
    parent, an orphan child, a withheld page, and a limit that ends inside a tie or a family."""
    windowed = answers(world.root, world.caller)
    assert routes == [], "the window must answer every cell, or this comparison proves nothing"
    declined_window(monkeypatch)
    exhaustive = answers(world.root, world.caller)
    assert routes, "the declined window must take the exhaustive route"

    for key, answer in windowed.items():
        assert answer == exhaustive[key], key
        if key[1] != "categories=config":
            assert file_view(answer) == world.file_mode[key], key
    # A comparison of two empty answers proves nothing, nor does one in which no page is withheld.
    assert all(windowed[(100, name)] for name in CELLS), {
        name: windowed[(100, name)] for name in CELLS
    }
    seen = {hit["path"] for hit in windowed[(100, "no filter")]}
    for index in (2, 5, 13):
        rel = f"{NOTES}/Plain-{index:02d}.md"
        assert (rel in seen) == (rel not in world.withheld), rel


def test_a_hostile_parent_answers_as_exhaustive(vault, configured_boundary, routes, monkeypatch):  # noqa: F811
    """A parent outside the knowledge base, or in a tree that the access policy excludes, is
    reported where the exhaustive browse reports it, whichever route answers. The owner reads it,
    so that no withheld parent hides the case."""
    families = {**clean_families(), **hostile_families(), **ACCESS_POLICY}
    world = build_world(vault, configured_boundary[1], "owner", families)
    windowed = answers(world.root, world.caller)
    declined_window(monkeypatch)
    exhaustive = answers(world.root, world.caller)
    assert windowed == exhaustive
    assert all(windowed[(100, name)] for name in CELLS)
    everything = json.dumps(windowed[(100, "no filter")])
    assert "Clip-Three.mp4" in everything and "Clip-Five.mp4" in everything


def test_a_page_that_fails_to_hydrate_is_replaced_by_the_next_one(world, monkeypatch):
    """A browse that stops short of its limit, or lists a page that failed to hydrate, when the
    window has pages left to read: the window refills from the next pages as the exhaustive route."""
    everyone = [hit["path"] for hit in browse(world.root, world.caller, 100, {})]
    lost = everyone[1]
    admitted = SimpleNamespace(incomplete=False)
    views = []
    real_resolve = find_module._resolve_page

    def resolve(root, rel, *args, **kwargs):
        if rel != lost:
            return real_resolve(root, rel, *args, **kwargs)
        # The admitted view that a managed request holds, handed to the browse that asks for it
        # just after this page failed.
        views.append(admitted)
        return None

    monkeypatch.setattr(find_module, "_resolve_page", resolve)
    monkeypatch.setattr(
        lexstore, "_admitted_catalog_view", lambda root: views.pop() if views else None
    )

    windowed = [hit["path"] for hit in browse(world.root, world.caller, 3, {})]
    assert admitted.incomplete, "the failed page must mark the answer incomplete"
    admitted.incomplete = False
    declined_window(monkeypatch)
    exhaustive = [hit["path"] for hit in browse(world.root, world.caller, 3, {})]

    assert windowed == exhaustive == [path for path in everyone if path != lost][:3]


def test_a_withheld_frame_never_reaches_a_visible_parents_hit(world):
    """A withheld child's excerpt or frame time on its visible parent's hit, or a withheld page that
    changes which pages the caller sees or in which order."""
    if world.name == "owner":
        pytest.skip("the owner is withheld nothing")
    owner = browse(world.root, world.owner, 100, {})
    clip = next(hit for hit in owner if hit["path"] == f"{NOTES}/Clip-Two.mp4.md")
    # The control: the newer, secret child is what the parent shows to a caller who may see it.
    assert clip["scene_frame_ts"] == 1.0
    assert SECRET_FRAME_TEXT in clip["excerpt"]

    everyone = [hit["path"] for hit in browse(world.root, world.caller, 100, {})]
    assert everyone == [hit["path"] for hit in owner if hit["path"] in set(everyone)]
    for limit in LIMITS:
        seen = browse(world.root, world.caller, limit, {})
        assert SECRET_FRAME_TEXT not in json.dumps(seen), limit
        assert not any(hit["scene_frame_ts"] == 1.0 for hit in seen), limit
        # A withheld page leaves no gap and no reorder: the first `limit` of the visible pages.
        assert [hit["path"] for hit in seen] == everyone[:limit], limit


def test_the_user_sees_no_withheld_frame_in_the_final_answer(vault, configured_boundary):  # noqa: F811
    """The same, through the tool that a caller uses, which applies egress after the browse."""
    world = build_world(vault, configured_boundary[1], "few withheld", clean_families())
    with world.caller():
        answer = commands.op_find(world.root, query="", limit=100, graph=False)
    text = json.dumps(answer, default=str)
    assert f"{NOTES}/Clip-Two.mp4.md" in text
    assert SECRET_FRAME_TEXT not in text
    assert SECRET_FRAME.removesuffix(".md") not in text


def test_the_window_runs_only_where_the_store_can_answer_it_whole(stored, monkeypatch):  # noqa: F811
    """A window that answers a browse that it cannot answer whole: with a query, a pending overlay
    to merge, a dimension predicate, a plan that only the offline oracle answers, a file-mode vault
    or a store without the index."""
    none = SimpleNamespace(empty=True)

    def window(**changes):
        arguments = dict(
            plan=None,
            query_norm="",
            pending=none,
            dimension_lookups=None,
            timings=None,
            resolve_plan=None,
        )
        return find_module._browse_window(stored, **{**arguments, **changes})

    assert window() is not None, "the control: an empty query with nothing pending is windowed"
    plan = structured_filters.compile_filter({"page.type": {"$eq": "note"}})
    assert window(plan=plan).keyed is False
    assert window(query_norm="lighthouse") is None
    assert window(pending=SimpleNamespace(empty=False)) is None
    assert window(dimension_lookups=object()) is None
    unnarrowed = structured_filters.compile_filter(None)
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    with pytest.raises(find_module.RetrievalIndexWarming):
        window(plan=unnarrowed)  # a refusal stays a refusal; the window does not answer it
    monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
    assert window(plan=plan) is not None, "an offline caller whose plan the columns answer"
    assert window(plan=unnarrowed) is None, "an offline caller keeps its scan fallback"
    monkeypatch.setattr(lexstore, "store_rows_serve", lambda _root: False)
    assert window() is None


def test_a_file_mode_vault_takes_no_window(vault, routes):  # noqa: F811
    """A window over a vault whose rows are a sidecar's: file mode keeps its own browse."""
    window = find_module._browse_window(
        vault,
        plan=None,
        query_norm="",
        pending=SimpleNamespace(empty=True),
        dimension_lookups=None,
        timings=None,
        resolve_plan=None,
    )
    assert window is None


def window_plan(root, monkeypatch, **cell):
    """`EXPLAIN QUERY PLAN` of the window's own statement, as the store runs it."""
    store = lexstore.get_store(root)
    real = store._serve_from_ready_catalog_result
    plans = []

    def serve(scope, freshness, query, message, **options):
        def traced(conn):
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                value = query(conn)
            finally:
                conn.set_trace_callback(None)
            for sql in statements:
                if sql.lstrip().startswith("SELECT r.path, r.updated") and "ORDER BY" in sql:
                    plans.append([row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)])
            return value

        return real(scope, freshness, traced, message, **options)

    monkeypatch.setattr(store, "_serve_from_ready_catalog_result", serve)
    browse(root, nullcontext, 15, cell)
    return plans


@pytest.mark.parametrize("cell", ["no filter", "types=note"])
def test_the_window_walks_the_browse_index_without_sorting(stored, writer, monkeypatch, cell):  # noqa: F811
    """A window statement that scans every page and sorts them: its cost then grows with the vault
    in SQLite, where the count of rows and decodes cannot see it."""
    commit_drill_pages(writer, 400)
    plans = window_plan(stored, monkeypatch, **DRILL_CELLS[cell])
    assert plans, "the browse must run the window statement"
    text = " | ".join(plans[0])
    assert "pages_browse" in text, text
    assert "TEMP B-TREE" not in text, text


@pytest.mark.parametrize("pages", [400, 1600])
def test_a_browse_reads_what_its_limit_needs_whatever_the_vault_holds(stored, writer, pages):  # noqa: F811
    """A browse whose catalogue rows plus view decodes grow with the pages in the vault: it must
    stay within four times its own limit (the drill's pages have no children), at both sizes."""
    commit_drill_pages(writer, pages)
    limit = 15
    # `op_find` asks a governed vault's browse for a pool of pages beyond the limit, then releases.
    _policy, over_fetch = egress.gate_state(stored)
    browse_limit = egress.pool_limit(limit) if over_fetch else limit
    source = str(Path(__file__).resolve().parents[1] / "src")
    figures = run_probe(stored, source)
    for name in ("types=note", "types=insight", "no filter"):
        for state in ("cold", "warm"):
            read = figures[name][state]
            assert read["hits"], name
            assert read["rows"] + read["decodes"] <= 4 * browse_limit, (pages, name, state, read)
    # A relation browse reads its relation set (fixed here), and decodes what its limit needs.
    for state in ("cold", "warm"):
        read = figures["relation_of hub"][state]
        assert read["hits"]
        assert read["decodes"] <= 4 * browse_limit, (pages, state, read)
        assert read["rows"] <= HUB_LINKS + 4 * browse_limit, (pages, state, read)
