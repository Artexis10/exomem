"""Batch proofs retain scalar admission and every live container dependency."""

from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_connector_boundary import PRIVATE, PUBLIC, SCOPE, configured_boundary  # noqa: F401
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_records_recall_policy import _manifest
from test_vault_store_migration import (  # noqa: F401
    build_indexes,
    migrate_apply,
    rollback_apply,
    vault,
)
from test_vault_store_read_write import put, stored, writer  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external

from exomem import (
    document_containers,
    find,
    find_corpus,
    find_types,
    lexstore,
    preserve,
    published_admission,
    recall_policy,
    state_paths,
    warmup,
)
from exomem.collection_store import connection, preview
from exomem.collection_store.writer import CollectionWriter
from exomem.governance import connector_boundary, egress, lifecycle, page_admission, policy
from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.vault_store import ownership, read, write


def test_single_page_content_check_does_not_read_unrelated_metadata(stored, writer, monkeypatch):  # noqa: F811
    """Prefetching a hub must not make a one-page content check enumerate the whole vault."""
    plain = "Knowledge Base/Notes/Single.md"
    body = b"---\ntitle: Single\ntype: note\n---\nOrdinary page.\n"
    write.commit(writer, put(plain, body))
    monkeypatch.setattr(read, "KEYS_PER_QUERY", 2)
    # Warm host configuration, while every measured request still starts with no metadata facts.
    with read.request_snapshot(stored):
        assert egress.content_permits(stored, plain, EXTERNAL)
    statements = []
    reader = connection.store_reader

    @contextmanager
    def traced(*args, **kwargs):
        with reader(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(connection, "store_reader", traced)
    counts = []
    for unrelated in (0, 8):
        if unrelated:
            write.commit(writer, write.StoreChange(
                write.Origin.GOVERNED, "unrelated corpus growth",
                puts=tuple(write.PagePut(f"Knowledge Base/Notes/Other-{n}.md", body) for n in range(unrelated)),
            ))
        statements.clear()
        with read.request_snapshot(stored):
            assert egress.content_permits(stored, plain, EXTERNAL)
        # These are closed SQL command tokens; count queries without parsing their implementation.
        counts.append(sum(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in statements))
    assert counts[0] > 0 and counts[1] == counts[0], counts


def exception_corpus(vault):  # noqa: F811
    """A page of every exception kind that a release decision has, under an external policy.

    Each case rewrites ``scope_file`` to one of ``selectors`` and writes or removes ``grant_file``;
    a purpose rule and the grant change what a caller with the audit purpose may see.
    """
    from test_document_containers import container_bytes

    source = preserve.preserve_stream(
        vault, scope="Examples", category="Documents", filename="bundle.asice",
        stream=io.BytesIO(container_bytes([("appendix.pdf", b"neutral document")]))
    )
    child = source.document_container["payloads"][0] + ".md"
    pages = {
        "Knowledge Base/Notes/Open.md": "title: Beacon\ntype: note\n",
        "Knowledge Base/Notes/Hidden.md": "title: Beacon\ntype: insight\nproject: restricted\ntags: [secret]\nclasses: [confidential]\n",
        "Knowledge Base/Notes/Excluded.md": "title: Excluded\ntags: [secret, public]\n",
        "Knowledge Base/Notes/Claim.md": "title: Claim\ncontainer_member: null\n",
        "Knowledge Base/Notes/__exomem_raw_v1__original.md": "title: RAW\n",
        "Knowledge Base/Notes/Identity.md": "title: Identity\nexomem_id: 12345678-1234-4abc-8def-123456789abc\n",
        "Knowledge Base/Notes/Invalid-id.md": "title: Invalid id\nexomem_id: malformed\n",
        "Knowledge Base/Evidence/Video/clip.mp4.md": "title: Video\ntype: source\ntags: [secret]\n",
        "Knowledge Base/Evidence/Video/clip.mp4.frames/scene-001.jpg.md": (
            "title: Frame\ntype: source\nparent_media: Knowledge Base/Evidence/Video/clip.mp4\n"
        ),
        "Knowledge Base/Sources/Report.md": "title: Report\ntype: source\ntags: [secret]\n",
        "Knowledge Base/Sources/.Report.parts/part-02.md": "title: Part\npart_of: ../Report.md\n",
        "Knowledge Base/Sources/orphan.asice.documents/appendix.pdf.md": "title: Removed claim\n",
        "Knowledge Base/Notes/Purpose.md": "title: Purpose\ntags: [purpose-only]\n",
    }
    for rel, frontmatter in pages.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}---\n\nA beacon links to [[Beacon]].\n")
    mixed_fence = "Knowledge Base/Notes/Mixed-fence.md"
    (vault / mixed_fence).write_bytes(
        b"---\n\r---\rcontainer_member: {version: 1}\n---\nA beacon links to [[Beacon]].\n"
    )
    # Only the normalised parse sees this bridge; the page must stay on the per-page rule.
    lone_cr_bridge = "Knowledge Base/Notes/Lone-cr-bridge.md"
    (vault / lone_cr_bridge).write_bytes(
        b"---\rtitle: Cr bridge\rtype: insight\rbridge_of: []\rbridge_scope: navigation\r---\rBody.\r"
    )
    records = vault / "Knowledge Base/Records/Measurements"
    (records / "items").mkdir(parents=True)
    (records / "_collection.md").write_text(_manifest())
    (records / "items/raw.md").write_text("Structured record only.\n")
    restrict_external(vault, 'tags: [secret]\nexclude:\n  tags: [public]')
    governance = vault / "Knowledge Base/_Governance"
    rule = governance / "rules/secret-external.yaml"
    rule.write_text(rule.read_text() + "purpose: audit\npurpose_condition: outside\n")
    (governance / "scopes/purpose.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB2\ntags: [purpose-only]\n"
    )
    (governance / "rules/purpose.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB3\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FB2"]\naudience: external\nceiling: 0\n'
        "purpose: audit\npurpose_condition: matches\n"
    )
    (governance / "grants").mkdir()
    grant_file = governance / "grants/audit.yaml"
    grant = (
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\nkind: standing\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 6\n'
    )
    paths = list(pages) + [mixed_fence, lone_cr_bridge, child, source.sidecar_path,
        "Knowledge Base/Records/Measurements/_collection.md",
        "Knowledge Base/Records/Measurements/items/raw.md",
        "Knowledge Base/Notes/Missing.md", "Knowledge Base/Notes/../Notes/Open.md"]
    selectors = (
        'tags: [secret]\nexclude:\n  tags: [public]',
        'paths: ["Notes/Hidden.md"]', 'projects: [restricted]', 'types: [insight]',
        'classes: [confidential]',
        'refs: ["exomem://memory/12345678-1234-4abc-8def-123456789abc"]',
        f'paths: [{json.dumps(source.path)}]',
    )
    return SimpleNamespace(
        paths=paths, child=child, source=source, mixed_fence=mixed_fence, selectors=selectors,
        scope_file=governance / "scopes/secret.yaml", grant_file=grant_file, grant=grant,
    )


def apply_case(corpus, selector, granted):
    """Select ``selector`` into the withheld scope, with the audit grant or without it."""
    corpus.scope_file.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n" + selector + "\n"
    )
    if granted:
        corpus.grant_file.write_text(corpus.grant)
    else:
        corpus.grant_file.unlink(missing_ok=True)
    document_containers.clear_unclaimed_notes()


@pytest.mark.parametrize("store_mode", [False, True])
def test_batch_and_scalar_admission_keep_policy_and_container_answers(vault, monkeypatch, store_mode):  # noqa: F811
    """A shortcut must not widen selectors, purpose, grants, RAW or inherited container restrictions."""
    corpus = exception_corpus(vault)
    if store_mode:
        build_indexes(vault)
        assert migrate_apply(vault) == 0
    paths, child, source = corpus.paths, corpus.child, corpus.source
    mixed_fence = corpus.mixed_fence

    def answers(keep):
        result = {}
        for path in paths:
            if ".." in path:
                continue
            try:
                result[path] = keep(path)
            except AttributeError as error:
                # Existing store companion snapshots lack filesystem identities; retain the same failed outcome.
                assert store_mode and path == child and str(error) == "'NoneType' object has no attribute 'device'"
                result[path] = str(error)
        return result

    for selector in corpus.selectors:
        for purpose, granted in ((None, False), ("audit", False), ("audit", True)):
            apply_case(corpus, selector, granted)
            who = RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp", purpose=purpose)
            with request_scope(who), policy.request_policy_snapshot(), state_paths.resolution_scope(), read.request_snapshot(vault):
                compiled = policy.load(vault)
                assert not compiled.blocked and bool(compiled.grants) == granted
                batch = recall_policy.admitted_among(vault, paths)
                assert batch == frozenset(
                    rel for rel in paths if recall_policy.is_recall_candidate(vault, vault / rel)
                )
                keep = egress.edge_view(vault, egress.content_admission(vault, who))
                binding = document_containers.parent_binding_for_path(vault, child)
                assert binding.path == source.path
                actual = answers(keep)
                offered = published_admission.admission_of(keep)
                assert offered is not None
                safe_paths = [path for path in paths if ".." not in path and path != child]
                assert {path: verdict[0] for path, verdict in offered.decide_many(safe_paths).items()} == {
                    path: actual[path] for path in safe_paths
                }
                if isinstance(actual[child], str):
                    with pytest.raises(AttributeError) as refused:
                        offered.decide_many([child])
                    assert str(refused.value) == actual[child]
                else:
                    assert offered.decide_many([child])[child][0] == actual[child]
                # Disable only the new set proof; the existing per-page policy is the oracle.
                with monkeypatch.context() as patch:
                    patch.setattr(document_containers, "_stored_unclaimed", lambda *args: False)
                    document_containers.clear_unclaimed_notes()
                    oracle = egress.edge_view(vault, egress.content_admission(vault, who))
                    assert document_containers.parent_binding_for_path(vault, child) == binding
                    assert actual == answers(oracle), (selector, purpose)
                assert not actual["Knowledge Base/Notes/Claim.md"]
                assert not actual[mixed_fence]
                assert not actual["Knowledge Base/Sources/orphan.asice.documents/appendix.pdf.md"]
                assert actual["Knowledge Base/Notes/Purpose.md"] == (purpose is None)
                if selector.startswith("tags:") and not granted:
                    assert not actual["Knowledge Base/Evidence/Video/clip.mp4.frames/scene-001.jpg.md"]
                    assert not actual["Knowledge Base/Sources/.Report.parts/part-02.md"]
                if selector.startswith("refs:"):
                    assert not actual["Knowledge Base/Notes/Invalid-id.md"]
                assert actual["Knowledge Base/Notes/Open.md"]


@pytest.mark.timeout(300)
def test_a_bound_writers_sweep_decides_a_set_of_pages_as_it_decides_each_page(vault):  # noqa: F811
    """A store-mode write's corpus and registry sweeps decide their pages as one set, and a read
    decides it by scope class. A set route that answered one exception kind differently from the
    per-page route would change what the owner's or a restricted caller's request may see."""
    corpus = exception_corpus(vault)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    paths = [path for path in corpus.paths if ".." not in path]
    # The companion child raises on either route, which fails a whole sweep; it is its own set here.
    others = [path for path in paths if path != corpus.child]

    def outcome(decide, *arguments):
        try:
            return decide(*arguments)
        except AttributeError as error:  # a stored companion snapshot has no file identity
            return str(error)

    def one_by_one(keep):
        return {path: outcome(keep, path) for path in paths}

    def as_one_set(keep):
        admitted = egress.admitted(keep, others)
        answers = {path: path in admitted for path in others}
        child = corpus.child
        answers[child] = outcome(lambda: child in egress.admitted(keep, [child]))
        return answers

    def compare(sweep, case):
        expected = one_by_one(sweep(vault))
        # Each caller is withheld something here, so both verdicts are compared.
        assert False in expected.values(), case
        assert as_one_set(sweep(vault)) == expected, ("in a write", *case)
        with read.request_snapshot(vault):
            assert as_one_set(sweep(vault)) == expected, ("in a read", *case)

    # The selector, purpose and grant change only the external caller's verdicts. The owner and
    # the link view (the corpus filter wrapped for links that may name no page) take one case each.
    cases = [(selector, None, False) for selector in corpus.selectors]
    cases += [(corpus.selectors[0], "audit", False), (corpus.selectors[0], "audit", True)]
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as handle:
        token = preview._BOUND.set((vault.resolve(), CollectionWriter(vault, handle), False))
        try:
            for index, (selector, purpose, granted) in enumerate(cases):
                apply_case(corpus, selector, granted)
                restricted = RequestPrincipal(
                    audience_id=EXTERNAL.audience_id, surface="mcp", purpose=purpose
                )
                case = (selector, purpose, granted)
                with request_scope(restricted):
                    compare(egress.restricted_release_filter, (*case, "restricted"))
                    if index == 0:
                        compare(egress.visible_page_filter, (*case, "restricted links"))
                if index == 0:
                    compare(egress.restricted_release_filter, (*case, "owner"))
                    compare(egress.visible_page_filter, (*case, "owner links"))
        finally:
            preview._BOUND.reset(token)


def decide_in_a_new_request(root, who, paths, monkeypatch, scalar=False):
    """`who`'s release verdict for each path, from the batch rule or, with `scalar`, from the per-page chain."""
    with request_scope(who), policy.request_policy_snapshot(), state_paths.resolution_scope(), read.request_snapshot(root):
        document_containers.clear_unclaimed_notes()
        keep = egress.edge_view(root, egress.content_admission(root, who))
        if scalar:
            # Disable only the set proof of container claims; the per-page chain is the oracle.
            with monkeypatch.context() as patch:
                patch.setattr(document_containers, "_stored_unclaimed", lambda *args: False)
                return {path: keep(path) for path in paths}
        offered = published_admission.admission_of(keep)
        return {path: verdict[0] for path, verdict in offered.decide_many(paths).items()}


def count_subject_decodes(monkeypatch):
    """The paths whose stored subject text the memo had to decode, in order."""
    decoded = []
    subject = page_admission._subject
    monkeypatch.setattr(page_admission, "_subject", lambda *args: decoded.append(args[1]) or subject(*args))
    return decoded


def commit_here(root, change):
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        write.commit(CollectionWriter(root, handle), change)


def commit_in_a_child(environment, root, change):
    """One commit by another process, which never calls this process's commit hook."""
    os.environ.clear()
    os.environ.update(environment)
    commit_here(Path(root), change)


def note(tags):
    return f"---\ntitle: Ledger\ntags: [{tags}]\n---\nOrdinary page.\n"


RETAGGED, MOVER = "Knowledge Base/Notes/Retagged.md", "Knowledge Base/Notes/Mover.md"
MOVED = "Knowledge Base/Restricted/Mover.md"


@pytest.mark.parametrize("event", [
    "retag", "move into a path-selected folder", "commit from a subprocess",
    "facts rewritten without a byte change", "roll back then migrate again",
])
def test_a_warm_scope_memo_answers_what_the_scalar_chain_answers_after_each_event(vault, tmp_path, monkeypatch, event):  # noqa: F811
    """Defect: a scope set that outlives a change to its page leaves a restricted caller reading a page
    that the per-page chain withholds. Each event changes one page; the memo is warm before it."""
    corpus = exception_corpus(vault)
    apply_case(corpus, 'tags: [secret]\npaths: ["Restricted/**"]\nexclude:\n  tags: [public]', False)
    for rel in (RETAGGED, MOVER):
        (vault / rel).write_text(note("calm"))
    # The container child fails alike in both rules in store mode (its companion snapshot has no filesystem identity), so it stays out.
    asked = [path for path in [*corpus.paths, RETAGGED, MOVER, MOVED] if ".." not in path and path != corpus.child]
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    who = RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp")
    decoded = count_subject_decodes(monkeypatch)

    before = decide_in_a_new_request(vault, who, asked, monkeypatch)
    assert decoded, "the first request decodes the stored facts"
    decoded.clear()
    assert decide_in_a_new_request(vault, who, asked, monkeypatch) == before
    assert not decoded, "the second request is served by the memo, or this test proves nothing"
    if event == "retag":
        commit_here(vault, put(RETAGGED, note("secret").encode()))
    elif event == "move into a path-selected folder":
        commit_here(vault, write.StoreChange(write.Origin.GOVERNED, "move", moves=(write.PageMove(MOVER, MOVED),)))
    elif event == "commit from a subprocess":
        change = put(RETAGGED, note("secret").encode())
        child_process = multiprocessing.get_context("spawn").Process(
            target=commit_in_a_child, args=(dict(os.environ), vault, change),
        )
        child_process.start()
        child_process.join(180)
        assert child_process.exitcode == 0
    elif event == "facts rewritten without a byte change":
        with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as handle:
            with handle.transaction() as tx:
                row = json.loads(tx.execute("SELECT subject FROM page_admission WHERE path = ?", (RETAGGED,)).fetchone()[0])
                row["tags"] = [*row["tags"], "secret"]
                tx.execute("UPDATE page_admission SET subject = ? WHERE path = ?",
                           (json.dumps(row, sort_keys=True, separators=(",", ":")), RETAGGED))
    else:
        assert rollback_apply(vault, tmp_path / "export") == 0
        (vault / RETAGGED).write_text(note("secret"))
        build_indexes(vault)
        assert migrate_apply(vault) == 0
    after = decide_in_a_new_request(vault, who, asked, monkeypatch)
    assert after != before, "the event must change an answer, or a stale memo would pass"
    page_admission.forget_scopes()
    assert after == decide_in_a_new_request(vault, who, asked, monkeypatch)
    if event != "facts rewritten without a byte change":  # there the stored facts and the bytes disagree by construction
        assert after == decide_in_a_new_request(vault, who, asked, monkeypatch, scalar=True)


OPEN = "Knowledge Base/Notes/Open.md"


@pytest.mark.parametrize("event", ["commit", "policy change", "grant", "tombstone"])
def test_a_warmed_store_releases_what_a_cold_one_releases_after_each_change(vault, monkeypatch, event):  # noqa: F811
    """Defect: a memo or a warm-up that a start fills outlives the change that alters a release, so a
    restricted caller reads a page that the change withheld, or misses one that it released.

    The ownership and scope-set memos are warm from the store-read warm-up and one request before the
    event. After it, the warm answer is the cold one (every memo dropped) and differs from the answer before.
    """
    corpus = exception_corpus(vault)
    apply_case(corpus, 'tags: [secret]\nexclude:\n  tags: [public]', False)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    asked = [path for path in corpus.paths if ".." not in path and path != corpus.child]
    who = RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp", purpose="audit")

    def released():
        # No bound writer: a bound one decides every page by the per-page chain and never reads the memos.
        with request_scope(who), read.request_snapshot(vault):
            keep = egress.restricted_release_filter(vault)
            return egress.admitted(keep, asked)

    decoded = count_subject_decodes(monkeypatch)
    warmup.warm_store_reads(vault)
    assert decoded, "the warm-up decodes the stored facts"
    decoded.clear()
    before = released()
    assert not decoded, "the warm-up filled the scope-set memo, and the request read it"
    assert OPEN in before and released() == before
    page_admission.forget_scopes()
    assert released() == before and decoded, "control: a request that the memo does not serve decodes the facts"
    if event == "commit":
        commit_here(vault, put(OPEN, note("secret").encode()))
    elif event == "policy change":
        apply_case(corpus, 'paths: ["Notes/Open.md"]', False)
    elif event == "grant":
        apply_case(corpus, 'tags: [secret]\nexclude:\n  tags: [public]', True)
    else:
        monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda _root: frozenset({OPEN}))
    warm = released()
    page_admission.forget_scopes()
    ownership._classified.cache_clear()
    assert warm == released()
    assert warm != before, "the event must change a release, or a stale memo would pass"


def test_a_scope_set_is_never_borrowed_from_another_page_with_the_same_subject_text(stored, writer, monkeypatch):  # noqa: F811
    """Defect: a facts row that carries another page's subject text, while that page's entry is warm, takes
    that page's scope set and releases a page that the per-page chain withholds."""
    open_page, shut_page = "Knowledge Base/Notes/Open Alpha.md", "Knowledge Base/Notes/Shut Beta.md"
    restrict_external(stored, 'tags: ["secret"]')
    write.commit(writer, put(open_page, note("calm").encode()))
    write.commit(writer, put(shut_page, note("secret").encode()))
    who = RequestPrincipal(audience_id=EXTERNAL.audience_id, surface="mcp")
    asked = [open_page, shut_page]
    assert decide_in_a_new_request(stored, who, asked, monkeypatch) == {open_page: True, shut_page: False}
    entries = page_admission._SCOPE_MEMOS[str(stored)].entries
    open_text = next(text for text, entry in entries.items() if entry[0] == open_page)
    assert entries[open_text][1] == frozenset(), "the open page's warm entry holds an empty scope set"
    with writer.handle.transaction() as tx:
        tx.execute("UPDATE page_admission SET subject = ? WHERE path = ?", (open_text, shut_page))
    answers = decide_in_a_new_request(stored, who, asked, monkeypatch)
    assert answers == {open_page: True, shut_page: False}
    assert answers == decide_in_a_new_request(stored, who, asked, monkeypatch, scalar=True)


def test_a_warm_scope_memo_follows_a_rearmed_registry_binding(vault, configured_boundary, monkeypatch):  # noqa: F811
    """Defect: a scope set that outlives the connector's registry binding leaves a page that the new binding
    assigns to a private scope released to the client that is denied that scope."""
    from exomem import state_migration
    from exomem.vocabulary import registry_specs

    config, authenticate = configured_boundary
    first, second = "Knowledge Base/Notes/Ledger-a.md", "Knowledge Base/Notes/Ledger-b.md"
    for rel in (first, second):
        (vault / rel).write_text("---\nproject: public-project\n---\nA ledger page.\n")
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    who = authenticate("limited")

    def arm(namespace):
        data = json.loads(config.read_text())
        # Arming requires the host to assign every definition and history that already exists.
        overrides = {spec.stem: {"overlay": spec.overlay(vault).relative_to(vault).as_posix(), "history": spec.stem}
                     for spec in registry_specs().values()}
        data["vocabulary"] = {
            "public": {"namespace": "Knowledge Base/_Schema/public", "history": "public", "overrides": overrides},
            "private": {SCOPE: {"namespace": namespace, "history": "private"}},
            "destinations": {}, "selections": {},
        }
        config.write_text(json.dumps(data))
        state_migration.arm_connector_boundary_offline(
            vault, authority=state_migration.assert_offline_migration_authority(source="memo re-arm"),
        )

    asked = [first, second]
    arm(first)
    before = decide_in_a_new_request(vault, who, asked, monkeypatch)
    assert before == {first: False, second: True}
    assert decide_in_a_new_request(vault, who, asked, monkeypatch) == before  # warm
    arm(second)
    after = decide_in_a_new_request(vault, who, asked, monkeypatch)
    assert after == {first: True, second: False}
    assert after == decide_in_a_new_request(vault, who, asked, monkeypatch, scalar=True)
    page_admission.forget_scopes()
    assert after == decide_in_a_new_request(vault, who, asked, monkeypatch)


def test_store_absence_proof_rechecks_claims_supplied_bytes_and_next_commit(stored, writer, monkeypatch):  # noqa: F811
    """Neither NULL metadata, overridden bytes nor a later claim can reuse an ordinary-page proof."""
    plain = "Knowledge Base/Notes/Plain.md"
    bodies = {
        plain: b"---\ntitle: Plain\n---\nOrdinary note.\n",
        "Knowledge Base/Notes/Null.md": b"---\ncontainer_member: null\n---\n",
        "Knowledge Base/Notes/Undecodable.md": b"---\ncontainer_member: null\n---\n\xff",
        "Knowledge Base/Notes/Empty.md": b"Ordinary note without frontmatter.\n",
        "Knowledge Base/Sources/bundle.asice.documents/missing.pdf.md": b"---\ntitle: Missing claim\n---\n",
    }
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "admission parity", puts=tuple(write.PagePut(p, b) for p, b in bodies.items())
    ))
    document_containers.clear_unclaimed_notes()
    monkeypatch.setattr(read, "KEYS_PER_QUERY", 2)
    with read.request_snapshot(stored):
        alias = plain.replace("/Notes/", "/Notes//")
        assert not (stored / plain).exists()
        assert recall_policy.is_recall_candidate(stored, stored / alias)
        assert recall_policy.admitted_among(stored, [alias]) == frozenset({alias})
        assert document_containers.parent_binding_for_path(stored, plain) is None
        for path, body in bodies.items():
            digest = hashlib.sha256(body).hexdigest()
            assert document_containers._stored_unclaimed(stored, path, digest, body) == (path == plain)
        forged = b"---\ncontainer_member: null\n---\n"
        with pytest.raises(ValueError):
            document_containers.parent_binding_for_path(stored, plain, content=forged)
        with document_containers.planned_scope(stored, [SimpleNamespace(path=stored / plain, content=forged.decode())]):
            with pytest.raises(ValueError):
                document_containers.parent_binding_for_path(stored, plain)
    # A row can outlive its ownership; its former projection is then the canonical file again.
    (stored / plain).write_bytes(forged)
    with monkeypatch.context() as patch:
        patch.setenv("EXOMEM_KB_DIRNAME", "Notebook")
        with read.request_snapshot(stored):
            assert read.owned_page(stored, plain) is None
            with pytest.raises(ValueError):
                document_containers.parent_binding_for_path(stored, plain)
    write.commit(writer, put(plain, forged))
    with read.request_snapshot(stored):
        with pytest.raises(ValueError):
            document_containers.parent_binding_for_path(stored, plain)


def test_direct_store_find_pins_connector_constants_and_rechecks_the_next_request(
    vault, configured_boundary, monkeypatch,  # noqa: F811
):
    """A direct read must apply its host ceiling once, then observe a changed ceiling next time."""
    config, authenticate = configured_boundary
    who = authenticate("limited")
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    snapshots = []
    current = connector_boundary._current_snapshot

    def counted(*args, **kwargs):
        snapshots.append(1)
        return current(*args, **kwargs)

    monkeypatch.setattr(connector_boundary, "_current_snapshot", counted)

    def batch_answers():
        with read.request_snapshot(vault):
            keep = egress.edge_view(vault, egress.content_admission(vault, who))
            expected = {path: keep(path) for path in (PUBLIC, PRIVATE)}
            offered = published_admission.admission_of(keep)
            assert offered.identity() is not None
            actual = offered.decide_many([PUBLIC, PRIVATE])
            assert {path: answer[0] for path, answer in actual.items()} == expected
            return expected

    with request_scope(who):
        assert batch_answers() == {PUBLIC: True, PRIVATE: False}
        snapshots.clear()
        find.clear_cache()
        hits = find.find(vault, query="", mode="keyword", graph=False, limit=50)
        assert PUBLIC in {hit.path for hit in hits} and PRIVATE not in {hit.path for hit in hits}
        assert len(snapshots) == 1
        data = json.loads(config.read_text())
        data["clients"][0].update(denied_scope_ids=[], writable_scope_ids=[])
        config.write_text(json.dumps(data))
        assert batch_answers() == {PUBLIC: True, PRIVATE: True}
        snapshots.clear()
        find.clear_cache()
        assert PRIVATE in {hit.path for hit in find.find(vault, query="", mode="keyword", graph=False, limit=50)}
        assert len(snapshots) == 1


def test_equal_ranking_metadata_decodes_once_without_sharing_mutable_frontmatter(stored, writer, monkeypatch):  # noqa: F811
    """Repeated authored metadata must preserve YAML types and nested values independently for each page."""
    bodies = {
        f"Knowledge Base/Notes/Rank-{index}.md": (
            f"---\ntitle: Rank {index}\ntype: note\n"
            "tags: &labels [survey, 2026-01-01]\nprojects: *labels\n"
            f"status: {'[active]' if index == 2 else 'active'}\n---\n\nLantern {index}.\n"
        ).encode()
        for index in range(3)
    }
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "ranking parity", puts=tuple(write.PagePut(p, b) for p, b in bodies.items())
    ))
    expected = {
        path: find_types.RankingPage.from_page(find_corpus.parse_page(stored / path, 0, stored, content=body))
        for path, body in bodies.items()
    }
    decoded = []
    from exomem import vault as vault_module

    load = vault_module.yaml_safe_load

    def counted(raw):
        decoded.append(raw)
        return load(raw)

    monkeypatch.setattr(vault_module, "yaml_safe_load", counted)
    with read.request_snapshot(stored), read.snapshot(stored) as view:
        rows = lexstore._eligibility_metadata_query(
            stored, view.connection, set(bodies), include_units=False, include_ranking=True,
        )
    assert {path: row.ranking for path, row in rows.items()} == expected
    assert len(decoded) == 2
    with read.request_snapshot(stored), read.snapshot(stored) as view:
        repeated = lexstore._eligibility_metadata_query(
            stored, view.connection, set(bodies), include_units=False, include_ranking=True,
        )
        serialized = json.loads(view.connection.execute(
            "SELECT ranking_json FROM pages WHERE path = ?", (next(iter(bodies)),),
        ).fetchone()[0])
    assert {path: row.ranking for path, row in repeated.items()} == expected
    assert len(decoded) == 4
    for invalid in ("[not a mapping]", "status: [unterminated"):
        with pytest.raises(ValueError, match="invalid ranking frontmatter"):
            lexstore._ranking_metadata(json.dumps({**serialized, "frontmatter_yaml": invalid}), {})
    first, second = [rows[path].ranking.frontmatter for path in list(bodies)[:2]]
    first["tags"].append("changed")
    assert second == expected[list(bodies)[1]].frontmatter
