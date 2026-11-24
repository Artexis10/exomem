"""Release verdicts that a store-mode write's corpus admission keeps from one save to the next.

Each store-mode save admits its whole corpus before it commits. A kept verdict must be the verdict that a
fresh decision gives, for every caller, after every change that can move one; and a save that changes one
page must decide that page again, not every page of the vault.
"""

from __future__ import annotations

import json
from contextlib import closing, contextmanager

import pytest
import test_vault_store_write_cost as write_cost
from test_collection_store_writer import CID
from test_connector_boundary import PRIVATE, PUBLIC, RESIDUAL, configured_boundary  # noqa: F401
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_admission_batches import apply_case, exception_corpus
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external
from test_vault_store_write_path import served  # noqa: F401

from exomem import semantic_contract
from exomem import structured_collections as collections
from exomem.collection_store import connection, preview, runtime
from exomem.collection_store import governance as store_governance
from exomem.collection_store.writer import CollectionWriter
from exomem.governance import egress, lifecycle
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.vault_store import write

OPEN = "Knowledge Base/Notes/Open.md"
VIDEO = "Knowledge Base/Evidence/Video/clip.mp4.md"
FRAME = "Knowledge Base/Evidence/Video/clip.mp4.frames/scene-001.jpg.md"
RAW = "Knowledge Base/Notes/__exomem_raw_v1__original.md"
RAW_SOURCE = "Knowledge Base/Sources/__exomem_raw_v1__ledger.md"
RAW_PART = "Knowledge Base/Sources/__exomem_raw_v1__ledger.parts/part-01.md"
#: Pages whose verdict reads only the caller, the policy, their own bytes and their parent's bytes.
KEPT = (OPEN, VIDEO, FRAME, RAW, RAW_SOURCE, RAW_PART)


@contextmanager
def bound(root):
    """The store thread's writer, bound as a store-mode write binds it."""
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        held = CollectionWriter(root, handle)
        token = preview._BOUND.set((root.resolve(), held, False))
        try:
            yield held
        finally:
            preview._BOUND.reset(token)


def decided_paths(monkeypatch):
    """The paths that each kept admission had to decide, one set per admission."""
    from exomem.governance import admission_memo

    calls = []
    decide = admission_memo.decide

    def recording(root, key, paths, decide_many, who):
        asked = set()
        calls.append(asked)
        return decide(root, key, paths, lambda wanted: asked.update(wanted) or decide_many(wanted), who)

    monkeypatch.setattr(admission_memo, "decide", recording)
    return calls


def admit(root, who, paths, *, kept):
    with request_scope(who):
        return egress.admitted(egress.restricted_release_filter(root), paths, kept=kept)


def page(frontmatter):
    return f"---\n{frontmatter}---\n\nA beacon links to [[Beacon]].\n".encode()


@pytest.mark.timeout(300)
def test_a_kept_verdict_is_the_fresh_verdict_after_each_change(vault, monkeypatch):  # noqa: F811
    """Defect: a verdict kept from an earlier save outlives a change that moves it, so a restricted caller's
    corpus admits a page that a fresh decision withholds, or loses one that it releases.

    The memo is warm before each change. After it, each caller's kept admission is the fresh one, and a
    change that moves a verdict moved one, or this test proves nothing.
    """
    corpus = exception_corpus(vault)
    for rel, frontmatter in ((RAW_SOURCE, "title: Ledger\ntype: source\ntags: [secret]\n"),
                             (RAW_PART, "title: Ledger part\npart_of: ../__exomem_raw_v1__ledger.md\n")):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_bytes(page(frontmatter))
    apply_case(corpus, corpus.selectors[0], False)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    paths = [path for path in [*corpus.paths, RAW_SOURCE, RAW_PART] if ".." not in path]
    callers = {
        "owner": owner_principal(surface="mcp"),
        # A connector client's login names its issuer, which RAW authority reads.
        "restricted": RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp", issuer_family="mcp-local"),
        "restricted audit": RequestPrincipal(
            audience_id=EXTERNAL.audience_id, surface="mcp", issuer_family="mcp-local", purpose="audit"),
    }
    calls = decided_paths(monkeypatch)
    tombstones = set()
    real_tombstones = lifecycle.tombstoned_paths
    monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda root: real_tombstones(root) | frozenset(tombstones))

    with bound(vault) as held:
        def commit(change):
            write.commit(held, change)

        def hold_open():
            with closing(connection.open_reader(connection.store_path(vault))) as reader:
                revision, sha256 = reader.execute(
                    "SELECT revision, sha256 FROM store_pages WHERE path = ?", (OPEN,)).fetchone()
            with held.handle.transaction() as tx:
                tx.execute(
                    "INSERT INTO store_held(path, kind, base_revision, store_revision, sha256, body, detected_at) "
                    "VALUES (?, 'edit', ?, ?, ?, ?, '2026-01-01T00:00:00Z')", (OPEN, revision, revision, sha256, b"held edit"))

        events = (
            # (event, action, whether it moves a verdict, pages it must decide again, pages it must not)
            ("no change", lambda: None, False, (), KEPT),
            ("retag an ordinary page", lambda: commit(put(OPEN, page("title: Open\ntags: [secret]\n"))),
             True, (OPEN,), (FRAME, RAW_PART)),
            ("untag the frame's video", lambda: commit(put(VIDEO, page("title: Video\ntype: source\n"))),
             True, (VIDEO, FRAME), (OPEN, RAW_PART)),
            ("retag the frame's video", lambda: commit(put(VIDEO, page("title: Video\ntype: source\ntags: [secret]\n"))),
             True, (VIDEO, FRAME), (OPEN, RAW_PART)),
            ("edit the RAW part's source", lambda: commit(put(RAW_SOURCE, page("title: Ledger 2\ntype: source\ntags: [secret]\n"))),
             False, (RAW_SOURCE, RAW_PART), (OPEN, FRAME)),
            ("edit a RAW page", lambda: commit(put(RAW, page("title: RAW 2\n"))), False, (RAW,), (OPEN, FRAME)),
            ("delete the frame's video", lambda: commit(write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(VIDEO),))),
             True, (FRAME,), (OPEN, RAW_PART)),
            ("restore the frame's video", lambda: commit(put(VIDEO, page("title: Video\ntype: source\ntags: [secret]\n"))),
             True, (VIDEO, FRAME), (OPEN, RAW_PART)),
            ("narrow the selector", lambda: apply_case(corpus, 'paths: ["Notes/Open.md"]', False), True, (), ()),
            ("restore the selector", lambda: apply_case(corpus, corpus.selectors[0], False), True, (), ()),
            ("grant the restricted audience", lambda: apply_case(corpus, corpus.selectors[0], True), True, (), ()),
            ("revoke the grant", lambda: apply_case(corpus, corpus.selectors[0], False), True, (), ()),
            ("tombstone kept pages", lambda: tombstones.update({OPEN, FRAME, RAW_PART}), True, (), ()),
            ("lift the tombstones", tombstones.clear, True, (), ()),
            ("hold a stored page", hold_open, False, (), KEPT),
            ("exclude a folder from the index", lambda: (vault / "Knowledge Base/_access.yaml").write_text(
                "excluded:\n  - Notes\n"), False, (), KEPT),
        )
        fresh = {name: admit(vault, who, paths, kept=False) for name, who in callers.items()}
        for name, who in callers.items():
            assert admit(vault, who, paths, kept=True) == fresh[name], ("warm-up", name)
        assert fresh["owner"] != fresh["restricted"], "each caller is withheld something different"
        for event, action, moves, again, kept in events:
            action()
            before, calls[:] = fresh, []
            kept_now = {name: admit(vault, who, paths, kept=True) for name, who in callers.items()}
            asked = set().union(*calls)
            fresh = {name: admit(vault, who, paths, kept=False) for name, who in callers.items()}
            assert kept_now == fresh, event
            assert (fresh != before) == moves, (event, "the event must move a verdict, or a stale memo would pass")
            assert set(again) <= asked, (event, "a page that the change touched is decided again", asked)
            assert not asked & set(kept), (event, "an unchanged page keeps its verdict", asked)


@pytest.mark.timeout(300)
def test_a_page_whose_parent_media_names_no_file_is_stored_and_decided_each_save(vault, monkeypatch):  # noqa: F811
    """Defect: a page whose ``parent_media`` has no file name, such as "/", made the migration and every store
    commit raise ValueError, because its facts asked for the frames folder of a path with no name. The page
    migrates and commits, and its admission is decided on every save, never kept: it claims a parent it lacks."""
    corpus = exception_corpus(vault)
    odd = "Knowledge Base/Notes/Odd frame.md"
    (vault / odd).write_bytes(page('title: Odd frame\nparent_media: "/"\n'))
    apply_case(corpus, corpus.selectors[0], False)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    calls = decided_paths(monkeypatch)
    restricted = RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp", issuer_family="mcp-local")
    with bound(vault) as held:
        write.commit(held, put(odd, page('title: Odd frame, edited\nparent_media: "/"\n')))
        for who in (owner_principal(surface="mcp"), restricted, restricted):
            calls[:] = []
            assert admit(vault, who, [odd], kept=True) == admit(vault, who, [odd], kept=False)
            assert odd in set().union(*calls), "a page that claims a parent it lacks is decided again"


@pytest.mark.timeout(300)
def test_a_withhold_from_an_unreadable_input_lasts_one_save(vault, monkeypatch):  # noqa: F811
    """Defect: a decision that cannot read one of its inputs withholds the page, and a kept withhold would hide
    the page from that caller's corpus until the page changed. The next save decides it again and admits it."""
    corpus = exception_corpus(vault)
    apply_case(corpus, corpus.selectors[0], False)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    who = owner_principal(surface="mcp")
    decide = egress._decide_path
    failing = {FRAME}

    def flaky(root, rel_path, **options):
        # A stat or read that fails once, as a page being replaced on disk does.
        if rel_path in failing:
            failing.discard(rel_path)
            return None
        return decide(root, rel_path, **options)

    with bound(vault):
        monkeypatch.setattr(egress, "_decide_path", flaky)
        assert FRAME not in admit(vault, who, [FRAME, VIDEO], kept=True)
        assert FRAME in admit(vault, who, [FRAME, VIDEO], kept=True)


def grow_dependents(held, start, count, *, on_disk):
    """Commit ``count`` scene frames, each with its video, and ``count`` RAW notes: pages whose release reads
    another page or the caller's RAW authority, which the scope class route never decides."""
    puts = []
    for number in range(start, start + count):
        video = f"Knowledge Base/Evidence/Video/clip-{number}.mp4"
        bodies = {
            f"{video}.md": f"---\ntitle: Clip {number}\ntype: source\n---\nA clip of the tide.\n",
            f"{video}.frames/scene-001.jpg.md": (
                f"---\ntitle: Frame {number}\ntype: source\nparent_media: {video}\n---\nA frame of the tide.\n"
            ),
            f"Knowledge Base/Notes/__exomem_raw_v1__tide-{number}.md": (
                f"---\ntitle: Tide reading {number}\n---\nA raw tide reading.\n"
            ),
        }
        for rel, body in bodies.items():
            puts.append(write.PagePut(rel, body.encode()))
            (on_disk / rel).parent.mkdir(parents=True, exist_ok=True)
            (on_disk / rel).write_bytes(body.encode())
    write.commit(held, write.StoreChange(write.Origin.GOVERNED, "growth", puts=tuple(puts)))


@pytest.mark.timeout(300)
def test_a_save_after_a_save_decides_the_pages_it_changed_not_the_vault(served, writer, monkeypatch):  # noqa: F811
    """Defect: each store-mode save decided every scene frame and RAW page of the vault again, one at a time,
    for its corpus admission: 44% of a save at 17,000 pages. A create and an edit by the owner are measured
    at N and 2N such pages, each also a file: the pages decided are the ones the save changes, plus a
    constant, whatever the page count."""
    write_cost.grow(writer, 1, 8, on_disk=served)
    grow_dependents(writer, 1, 12, on_disk=served)
    restrict_external(served, "tags: [secret]")
    runtime._SERVING.pop(served, None)
    write_cost.edit(served, write_cost.create(served, "Warm-up"))
    decided = []
    decide = egress._decide_path
    monkeypatch.setattr(egress, "_decide_path", lambda *args, **kwargs: decided.append(args) or decide(*args, **kwargs))
    measured = []
    for title, more in (("First probe", 0), ("Second probe", 12)):
        if more:
            grow_dependents(writer, 1000, more, on_disk=served)
            # The pages just committed are new to every save's admission; the next save decides them once.
            write_cost.edit(served, write_cost.create(served, "Catch-up"))
        pages = write_cost.page_count(served)
        decided.clear()
        path = write_cost.create(served, title)
        created = len(decided)
        decided.clear()
        write_cost.edit(served, path)
        measured.append((pages, created, len(decided)))
    report = json.dumps(measured)
    (pages_n, create_n, edit_n), (pages_2n, create_2n, edit_2n) = measured
    assert pages_2n - pages_n >= 3 * 12, report
    assert create_2n - create_n <= 2, report
    assert edit_2n - edit_n <= 2, report


def grow_rows(store_writer, start, count):
    """Append ``count`` rows to the Work collection and write each row's view."""
    for number in range(start, start + count):
        store_writer.append_record(CID, item={"title": f"Row {number}"}, item_key=row_key(number), why="growth")
    store_writer.reconcile_views(limit=1024)


def row_key(number):
    return f"{number:08d}-1111-4111-8111-111111111111"


def collection_views(root):
    """Every page that a collection of the store writes, as its files on disk hold it."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        paths = [path for (path,) in reader.execute("SELECT path FROM projection_state ORDER BY path")]
    return [path for path in paths if (root / path).is_file()]


def projection_decisions(monkeypatch):
    """The paths whose release the store decided afresh, rather than reading a kept decision."""
    made = []
    subjects = store_governance.OperationAuthorization.projection_subjects
    monkeypatch.setattr(store_governance.OperationAuthorization, "projection_subjects",
                        lambda operation, path: made.append(path) or subjects(operation, path))
    return made


@pytest.mark.timeout(300)
def test_a_save_after_a_save_decides_no_collection_view_again(served, writer, monkeypatch):  # noqa: F811
    """Defect: each store-mode save proved every collection view of the vault again for its corpus admission,
    twice, because a vault commit cleared the store's release cache: 65% of a create with 334 views. A
    create and an edit by the owner are measured at N and 2N views, each a file: the views decided afresh
    are the ones that the save changes, plus a constant, whatever the view count."""
    write_cost.grow(writer, 1, 8, on_disk=served)
    runtime._SERVING.pop(served, None)
    made = projection_decisions(monkeypatch)
    measured = []
    for title, start in (("First probe", 100), ("Second probe", 200)):
        grow_rows(writer, start, 16)
        # The views just written are new to every save's admission; the next save decides them once.
        write_cost.edit(served, write_cost.create(served, f"Catch-up {start}"))
        views = len(collection_views(served))
        made.clear()
        path = write_cost.create(served, title)
        created = len(made)
        made.clear()
        write_cost.edit(served, path)
        measured.append((views, created, len(made)))
    report = json.dumps(measured)
    (views_n, create_n, edit_n), (views_2n, create_2n, edit_2n) = measured
    assert views_2n - views_n >= 16, report
    assert create_2n - create_n <= 2, report
    assert edit_2n - edit_n <= 2, report


@pytest.mark.timeout(300)
def test_a_kept_view_decision_is_the_fresh_decision_after_each_change(served, writer, monkeypatch):  # noqa: F811
    """Defect: a view's release decision, kept from one save to the next, outlives a change that moves it, so
    a caller's corpus admits a view that a fresh decision withholds, or loses one that it releases.

    The kept decisions are warm before each change. After it, each caller's admission of every view is the
    one that an empty release cache gives. A change that moves a verdict moved one, or this test proves
    nothing, and a save that changes no collection decides no view again.
    """
    grow_rows(writer, 100, 3)
    restrict_external(served, "tags: [secret]")
    scope = served / "Knowledge Base/_Governance/scopes/secret.yaml"
    policy_text = scope.read_text()
    paths = collection_views(served)
    edited = "Knowledge Base/Records/Work/Items/" + row_key(100) + ".md"
    view_bytes = (served / edited).read_bytes()
    changed = row_key(101)
    row_body = writer.connection.execute("SELECT body FROM item_rows WHERE item_key = ?", (changed,)).fetchone()[0]
    callers = {
        "owner": owner_principal(surface="mcp"),
        "restricted": RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp"),
        "restricted audit": RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp", purpose="audit"),
    }
    tombstones = set()
    real_tombstones = lifecycle.tombstoned_paths
    monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda root: real_tombstones(root) | frozenset(tombstones))
    made = projection_decisions(monkeypatch)

    def fresh(who):
        writer.handle.release_cache.clear()
        return admit(served, who, paths, kept=True)

    def set_row_body(body):
        # A store write outside the writer's own statements, as a repair or a migration makes one.
        writer.connection.execute("UPDATE item_rows SET body = ? WHERE item_key = ?", (body, changed))

    events = (
        # (event, action, whether it moves a verdict, whether the kept admission decides no view again)
        ("no change", lambda: None, False, True),
        ("save a note", lambda: write.commit(writer, put("Knowledge Base/Notes/Tide.md", page("title: Tide\n"))),
         False, True),
        ("edit a view", lambda: (served / edited).write_bytes(view_bytes + b"\nAn edit by hand.\n"), True, False),
        ("restore the view", lambda: (served / edited).write_bytes(view_bytes), True, False),
        ("change a row outside the writer", lambda: set_row_body("Changed in the store."), True, False),
        ("restore the row", lambda: set_row_body(row_body), True, False),
        ("withhold the collection from the restricted audience",
         lambda: scope.write_text(policy_text.replace("tags: [secret]", 'paths: ["Records/Work/**"]')), True, False),
        ("restore the policy", lambda: scope.write_text(policy_text), True, False),
        ("tombstone a row", lambda: tombstones.add(collections.record_ref(CID, changed)), True, False),
        ("lift the tombstone", tombstones.clear, True, False),
    )
    before = {name: fresh(who) for name, who in callers.items()}
    assert before["owner"] != before["restricted"], "each caller is withheld something different"
    for name, who in callers.items():
        assert admit(served, who, paths, kept=True) == before[name], ("warm-up", name)
    for event, action, moves, reused in events:
        action()
        made.clear()
        kept = {name: admit(served, who, paths, kept=True) for name, who in callers.items()}
        decided_again = list(made)
        now = {name: fresh(who) for name, who in callers.items()}
        assert kept == now, event
        assert (now != before) == moves, (event, "the event must move a verdict, or a stale decision would pass")
        assert not reused or not decided_again, (event, "an unchanged view keeps its decision", decided_again)
        before = now
        for who in callers.values():
            admit(served, who, paths, kept=True)


@pytest.mark.timeout(300)
def test_a_restricted_client_never_reads_what_the_owners_save_admitted(vault, configured_boundary, monkeypatch):  # noqa: F811
    """Drill: the owner's save keeps a verdict for every page; a connector client denied a scope then builds
    its corpus under the same bound writer, and the host changes the client's ceiling between its builds.
    The client's corpus admits only what a fresh decision admits for it, after each change."""
    config, authenticate = configured_boundary
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    limited = authenticate("limited")
    admitted = []
    real_admitted = egress.admitted

    def watched(keep, paths, **options):
        found = real_admitted(keep, paths, **options)
        if options.get("kept"):
            admitted.append((frozenset(paths), found))
        return found

    monkeypatch.setattr(egress, "admitted", watched)

    def corpus(who):
        admitted.clear()
        with request_scope(who):
            semantic_contract.build_corpus_context_with_census(vault)
        assert len(admitted) == 1, "the build admits its corpus once, from kept verdicts"
        pages, found = admitted[0]
        with request_scope(who):
            assert found == real_admitted(egress.restricted_release_filter(vault), pages), "kept is fresh"
        return pages, found

    def deny(scopes):
        data = json.loads(config.read_text())
        data["clients"][0]["denied_scope_ids"] = scopes
        config.write_text(json.dumps(data))

    with bound(vault):
        pages, owners = corpus(owner_principal(surface="mcp"))
        assert {PUBLIC, PRIVATE} <= pages and {PUBLIC, PRIVATE} <= owners
        _, first = corpus(limited)
        assert PUBLIC in first and PRIVATE not in first
        calls = decided_paths(monkeypatch)
        assert corpus(limited)[1] == first
        assert calls and not {PUBLIC, PRIVATE} & set().union(*calls), "the client's second build reads kept verdicts"
        denied = json.loads(config.read_text())["clients"][0]["denied_scope_ids"]
        # A client that writes its inbox stays fenced: it is denied the residual scope alone.
        deny([RESIDUAL])
        _, widened = corpus(limited)
        assert PRIVATE in widened, "the ceiling change must move a verdict, or a stale memo would pass"
        deny(denied)
        _, narrowed = corpus(limited)
        assert PRIVATE not in narrowed and narrowed == first
