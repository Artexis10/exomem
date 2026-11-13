"""Batch proofs retain scalar admission and every live container dependency."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_connector_boundary import PRIVATE, PUBLIC, configured_boundary  # noqa: F401
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_records_recall_policy import _manifest
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
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
)
from exomem.collection_store import connection
from exomem.governance import connector_boundary, egress, policy
from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.vault_store import read, write


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


@pytest.mark.parametrize("store_mode", [False, True])
def test_batch_and_scalar_admission_keep_policy_and_container_answers(vault, monkeypatch, store_mode):  # noqa: F811
    """A shortcut must not widen selectors, purpose, grants, RAW or inherited container restrictions."""
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
    }
    for rel, frontmatter in pages.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}---\n\nA beacon links to [[Beacon]].\n")
    records = vault / "Knowledge Base/Records/Measurements"
    (records / "items").mkdir(parents=True)
    (records / "_collection.md").write_text(_manifest())
    (records / "items/raw.md").write_text("Structured record only.\n")
    restrict_external(vault, 'tags: [secret]\nexclude:\n  tags: [public]')
    governance = vault / "Knowledge Base/_Governance"
    rule = governance / "rules/secret-external.yaml"
    rule.write_text(rule.read_text() + "purpose: audit\npurpose_condition: outside\n")
    (governance / "grants").mkdir()
    grant_file = governance / "grants/audit.yaml"
    grant = (
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\nkind: standing\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 6\n'
    )
    if store_mode:
        build_indexes(vault)
        assert migrate_apply(vault) == 0
    paths = list(pages) + [child, source.sidecar_path,
        "Knowledge Base/Records/Measurements/_collection.md",
        "Knowledge Base/Records/Measurements/items/raw.md",
        "Knowledge Base/Notes/Missing.md", "Knowledge Base/Notes/../Notes/Open.md"]
    scope_file = governance / "scopes/secret.yaml"

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

    selectors = (
        'tags: [secret]\nexclude:\n  tags: [public]',
        'paths: ["Notes/Hidden.md"]', 'projects: [restricted]', 'types: [insight]',
        'classes: [confidential]',
        'refs: ["exomem://memory/12345678-1234-4abc-8def-123456789abc"]',
        f'paths: [{json.dumps(source.path)}]',
    )
    for selector in selectors:
        scope_file.write_text(
            "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n" + selector + "\n"
        )
        for purpose, granted in ((None, False), ("audit", False), ("audit", True)):
            if granted:
                grant_file.write_text(grant)
            else:
                grant_file.unlink(missing_ok=True)
            document_containers.clear_unclaimed_notes()
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
                assert actual["Knowledge Base/Notes/Open.md"]


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
