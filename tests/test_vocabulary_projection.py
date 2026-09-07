import uuid

import pytest

from exomem import entity_types, epistemic_graph
from exomem import vocabulary_projection as projection
from exomem.governance.principal import library_scope


def write(vault, path, text):
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def fixture_graph(vault, *, same_url=False, copy_body=False, unknown_origin=False):
    source_path = "Knowledge Base/Entities/Organizations/first.md"
    target_path = "Knowledge Base/Entities/Organizations/second.md"
    for index, path in enumerate((source_path, target_path)):
        sources = (
            '\nsources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]'
            if index == 0
            else ""
        )
        body = (
            "## Relations\n- relates_to [[Entities/Organizations/second]]\n"
            if index == 0
            else "An organization.\n"
        )
        write(
            vault,
            path,
            f"---\ntype: organization\nstatus: active\ntitle: Company {index}\nexomem_id: {uuid.uuid4()}{sources}\n---\n{body}",
        )
    for index, name in enumerate(("one", "two")):
        url = (
            ""
            if unknown_origin
            else f"\nurl: https://example.org/report/{0 if same_url else index}"
        )
        body = (
            "A supplies equipment to B."
            if copy_body or index == 0
            else "An independent report of deliveries between A and B."
        )
        write(
            vault,
            f"Knowledge Base/Sources/Articles/{name}.md",
            f"---\ntype: source\nsource_type: article\nexomem_id: {uuid.uuid4()}{url}\n---\n{body}\n",
        )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return source_path, target_path


def fixture_many_edges(vault):
    path, _ = fixture_graph(vault)
    raw = (vault / path).read_text()
    for index in range(9):
        name = f"target-{index}"
        write(
            vault,
            f"Knowledge Base/Entities/Organizations/{name}.md",
            f"---\ntype: organization\nstatus: active\nexomem_id: {uuid.uuid4()}\n---\nA separate institution.\n",
        )
        raw += f"- relates_to [[Entities/Organizations/{name}]]\n"
    write(vault, path, raw)
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return path


def fixture_no_candidate(vault):
    path, _ = fixture_graph(vault)
    page = vault / path
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "## Relations\n- relates_to [[Entities/Organizations/second]]\n",
            "See [[Entities/Organizations/second]].\n",
        ),
        encoding="utf-8",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return path


def fixture_registry_race(vault):
    path = "Knowledge Base/Notes/Insights/places.md"
    for name in ("a", "b"):
        write(
            vault,
            f"Knowledge Base/Entities/Places/{name}.md",
            f"---\ntype: place\nstatus: active\nexomem_id: {uuid.uuid4()}\n---\nPlace {name}.\n",
        )
    for name in ("one", "two"):
        write(
            vault,
            f"Knowledge Base/Sources/Articles/{name}.md",
            f"---\ntype: source\nurl: https://example.test/{name}\n"
            f"exomem_id: {uuid.uuid4()}\n---\nIndependent source {name}.\n",
        )
    write(
        vault,
        path,
        '---\ntype: insight\nsources: ["[[Sources/Articles/one]]", '
        '"[[Sources/Articles/two]]"]\n---\n'
        "The reports connect [[Entities/Places/a]] and [[Entities/Places/b]].\n",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return path


def activate_place_type(vault):
    registry = entity_types.extension_registry_path(vault)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        """schema_version: 1
entity_types:
  place:
    folder: Places
    label: Place
    aliases: [location]
    capture_guidance: A stable place identity used across notes.
    parent: concept
    status: active
""",
        encoding="utf-8",
    )
    entity_types._CACHE.clear()


def test_edge_cursor_can_resume_every_pair_without_duplicates(tmp_path):
    path = fixture_many_edges(tmp_path)
    with library_scope():
        one = projection.for_write(tmp_path, path=path)
        two = projection.for_write(tmp_path, path=path, continuation=one["continuation"])
        three = projection.for_write(tmp_path, path=path, continuation=two["continuation"])
    assert [len(page["items"]) for page in (one, two, three)] == [4, 4, 2]
    assert len({item.ref for page in (one, two, three) for item in page["items"]}) == 10
    assert three["continuation"] is None


def test_sourced_ordinary_note_exposes_its_resolved_pair_without_inventing_a_meaning(tmp_path):
    fixture_graph(tmp_path)
    path = "Knowledge Base/Notes/Insights/deliveries.md"
    write(
        tmp_path,
        path,
        '---\ntype: insight\nsources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]\n---\n'
        "The reports describe [[Entities/Organizations/first]] and [[Entities/Organizations/second]].\n",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        result = projection.for_write(tmp_path, path=path)
    assert len(result["items"]) == 1
    assert result["signals"][0]["selected_relation"] is None
    assert result["signals"][0]["independent_origins"] == 2
    assert path in {
        dict(result["items"][0].paths).get(entry.ref, entry.ref)
        for entry in result["items"][0].evidence
    }
    assert set(dict(result["items"][0].paths).values()) >= {
        "Knowledge Base/Entities/Organizations/first.md",
        "Knowledge Base/Entities/Organizations/second.md",
    }


def test_note_pair_pages_cover_all_pairs_without_a_quadratic_intermediate(tmp_path):
    fixture_many_edges(tmp_path)
    path = "Knowledge Base/Notes/Insights/cohort.md"
    links = [f"[[Entities/Organizations/target-{index}]]" for index in range(6)]
    write(
        tmp_path,
        path,
        '---\ntype: insight\nsources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]\n---\n'
        + "The recorded organizations: "
        + ", ".join(links)
        + ".\n",
    )
    items = []
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        continuation = None
        for _ in range(12):
            result = projection.for_write(tmp_path, path=path, continuation=continuation)
            assert len(result["items"]) <= 4
            assert result["query_budget"]["neighbour_rows"] <= 7
            items.extend(result["items"])
            continuation = result["continuation"]
            if continuation is None:
                break
    assert continuation is None
    assert len(items) == len({item.ref for item in items}) == 15


def test_real_generic_edge_and_independent_sources_produce_bounded_work(tmp_path):
    source_path, _ = fixture_graph(tmp_path)
    with library_scope():
        result = projection.for_write(tmp_path, path=source_path)
    assert result["status"] == "current"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item.family == "relation-type/v1"
    assert len(item.target_versions) == 2
    assert len(item.evidence) == 2
    assert all(ref.startswith("exomem://memory/") for ref, _ in item.target_versions)
    assert result["signals"][0]["independent_origins"] == 2
    assert result["signals"][0]["selected_relation"] is None


def test_empty_probe_requires_a_live_snapshot(tmp_path, monkeypatch):
    path = fixture_no_candidate(tmp_path)

    assert projection.proven_empty_for_write(tmp_path, path=path)
    monkeypatch.setattr(projection, "_live_snapshot", lambda *_args: False)
    assert not projection.proven_empty_for_write(tmp_path, path=path)


def test_empty_projection_refuses_an_unindexed_anchor_change(tmp_path):
    path = fixture_no_candidate(tmp_path)
    page = tmp_path / path
    page.write_text(page.read_text(encoding="utf-8") + "Unindexed edit.\n", encoding="utf-8")

    with library_scope():
        result = projection.for_write(tmp_path, path=path)

    assert result["status"] == "unavailable"
    assert result["reason"] == "target_projection_changed"


def test_empty_probe_refuses_a_changed_entity_registry(tmp_path, monkeypatch):
    path = fixture_registry_race(tmp_path)
    real_load = entity_types.load_entity_types
    changed = False

    def load_then_activate(vault):
        nonlocal changed
        registry = real_load(vault)
        if not changed:
            changed = True
            activate_place_type(vault)
        return registry

    monkeypatch.setattr(entity_types, "load_entity_types", load_then_activate)

    assert not projection.proven_empty_for_write(tmp_path, path=path)


@pytest.mark.parametrize("duplicate_endpoint", [False, True])
def test_ambiguous_entity_ids_are_not_treated_as_resolved_pairs(tmp_path, duplicate_endpoint):
    source_path, target_path = fixture_graph(tmp_path)
    source_text = (tmp_path / source_path).read_text()
    source_id = source_text.split("exomem_id: ", 1)[1].splitlines()[0]
    duplicate_path = (
        target_path if duplicate_endpoint else "Knowledge Base/Entities/Organizations/copy.md"
    )
    write(
        tmp_path,
        duplicate_path,
        f"---\ntype: organization\nstatus: active\nexomem_id: {source_id}\n---\nAn unresolved duplicate identity.\n",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        result = projection.for_write(tmp_path, path=source_path)
    assert result["status"] == "unavailable"
    assert result["reason"] == "ambiguous_entity_identity"
    assert result["items"] == []


@pytest.mark.parametrize("options", [{"same_url": True}, {"copy_body": True}])
def test_copies_by_origin_or_raw_content_cannot_manufacture_recurrence(tmp_path, options):
    path, _ = fixture_graph(tmp_path, **options)
    with library_scope():
        result = projection.for_write(tmp_path, path=path)
    assert not result["items"]
    assert result["signals"][0]["independent_origins"] == 1


def test_duplicate_source_identity_cannot_manufacture_independence(tmp_path):
    path, _ = fixture_graph(tmp_path)
    one = tmp_path / "Knowledge Base/Sources/Articles/one.md"
    two = tmp_path / "Knowledge Base/Sources/Articles/two.md"
    identity = one.read_text().split("exomem_id: ", 1)[1].splitlines()[0]
    previous = two.read_text().split("exomem_id: ", 1)[1].splitlines()[0]
    two.write_text(two.read_text().replace(previous, identity))
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        with pytest.raises(ValueError, match="ambiguous source identity"):
            projection.for_write(tmp_path, path=path)


def test_ambiguous_ordinary_note_anchor_keeps_path_identity(tmp_path):
    fixture_graph(tmp_path)
    identity = str(uuid.uuid4())
    path = "Knowledge Base/Notes/context.md"
    write(tmp_path, path,
          f'---\ntype: note\nexomem_id: {identity}\nsources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]\n---\n'
          '[[Entities/Organizations/first]] and [[Entities/Organizations/second]].\n')
    write(tmp_path, "Knowledge Base/Notes/duplicate.md",
          f"---\ntype: note\nexomem_id: {identity}\n---\nAn unrelated context.\n")
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        result = projection.for_write(tmp_path, path=path)
    assert len(result["items"]) == 1
    refs = {entry.ref for entry in result["items"][0].evidence}
    assert path in refs
    assert f"exomem://memory/{identity}" not in refs


@pytest.mark.parametrize("ordinary_note", [False, True])
def test_archived_entity_is_not_a_resolved_pair_endpoint(tmp_path, ordinary_note):
    path, other = fixture_graph(tmp_path)
    target = tmp_path / other
    target.write_text(target.read_text().replace("status: active", "status: archived"))
    if ordinary_note:
        path = "Knowledge Base/Notes/archived-context.md"
        write(
            tmp_path,
            path,
            '---\ntype: note\nsources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]\n---\n'
            "[[Entities/Organizations/first]] and [[Entities/Organizations/second]].\n",
        )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        result = projection.for_write(tmp_path, path=path)
    assert result["items"] == []


def test_distinct_paths_without_origin_evidence_are_unavailable(tmp_path):
    path, _ = fixture_graph(tmp_path, unknown_origin=True)
    with library_scope():
        result = projection.for_write(tmp_path, path=path)
    assert not result["items"]
    assert result["signals"][0]["eligibility"] == "unavailable"
    assert result["signals"][0]["independent_origins"] is None


def test_missing_graph_is_typed_unavailable_not_clean(tmp_path):
    result = projection.for_write(tmp_path, path="Knowledge Base/Entities/Organizations/missing.md")
    assert result["status"] == "unavailable"
    assert result["items"] == []
    assert result["reason"] == "graph_projection_unavailable"


def test_missing_anchor_in_a_ready_graph_is_typed_unavailable(tmp_path):
    fixture_graph(tmp_path)

    with library_scope():
        result = projection.for_write(
            tmp_path, path="Knowledge Base/Entities/Organizations/missing.md"
        )

    assert result["status"] == "unavailable"
    assert result["reason"] == "target_projection_unavailable"


def test_projection_does_not_walk_corpus_or_resolve_ids_by_scan(tmp_path, monkeypatch):
    path, _ = fixture_graph(tmp_path)
    from exomem import find_corpus, memory_refs

    def forbidden(*args, **kwargs):
        pytest.fail("synchronous vocabulary projection attempted a corpus scan")

    monkeypatch.setattr(memory_refs, "_scan_pages", forbidden)
    monkeypatch.setattr(find_corpus, "walk_md", forbidden)
    with library_scope():
        result = projection.for_write(tmp_path, path=path)
    assert result["items"]
    assert result["query_budget"]["edge_rows"] <= 5
    assert result["query_budget"]["source_rows"] <= 9


def test_origin_continuation_returns_the_next_real_source_page(tmp_path):
    path, _ = fixture_graph(tmp_path)
    raw = (tmp_path / path).read_text()
    names = [f"extra-{i}" for i in range(9)]
    refs = ["Sources/Articles/one", "Sources/Articles/two"] + [
        f"Sources/Articles/{name}" for name in names
    ]
    raw = raw.replace(
        'sources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]',
        "sources: [" + ", ".join(f'"[[{ref}]]"' for ref in refs) + "]",
    )
    write(tmp_path, path, raw)
    for name in names:
        write(
            tmp_path,
            f"Knowledge Base/Sources/Articles/{name}.md",
            f"---\ntype: source\nsource_type: article\nurl: https://example.org/{name}\nexomem_id: {uuid.uuid4()}\n---\nAn independent account {name}.\n",
        )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        first = projection.for_write(tmp_path, path=path)
        assert first["status"] == "warming"
        assert not first["items"]
        first = projection.for_write(tmp_path, path=path, continuation=first["continuation"])
        continuation = first["items"][0].continuation
        assert continuation
        next_page = projection.more_evidence(tmp_path, continuation=continuation)
        final_page = projection.more_evidence(tmp_path, continuation=next_page["continuation"])
    assert [len(next_page["evidence"]), len(final_page["evidence"])] == [8, 1]
    assert final_page["continuation"] is None
    assert len(first["items"][0].evidence) == 2
    assert not {entry.ref for entry in first["items"][0].evidence} & {
        entry.ref for entry in (*next_page["evidence"], *final_page["evidence"])
    }
    from exomem import review_state, vocabulary_review
    from exomem.vocabulary_state import VocabularyState

    original_item = first["items"][0]
    VocabularyState(tmp_path).observe(original_item)
    before_context = review_state.state_path(tmp_path).read_bytes()
    with library_scope():
        expanded = vocabulary_review.context(
            tmp_path, ref=original_item.ref, continuation=continuation
        )
        rest = vocabulary_review.context(
            tmp_path, ref=original_item.ref, continuation=expanded["evidence_continuation"]
        )
    assert expanded["item"]["fingerprint"] == original_item.fingerprint
    assert len(expanded["item"]["evidence"]) == 2
    assert len(expanded["evidence"]) == 8
    assert len(rest["evidence"]) == 1
    assert rest["evidence_continuation"] is None
    assert review_state.state_path(tmp_path).read_bytes() == before_context
    source = tmp_path / next_page["paths"][next_page["evidence"][0].ref]
    source.write_text(source.read_text() + "changed\n")
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_EVIDENCE_UNAVAILABLE"):
        projection.more_evidence(tmp_path, continuation=continuation)
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
            projection.more_evidence(tmp_path, continuation=continuation)


def fixture_paged_origins(vault, *, independent_at=None):
    path, _ = fixture_graph(vault)
    names = [f"paged-{i:02}" for i in range(12)]
    raw = (
        (vault / path)
        .read_text()
        .replace(
            'sources: ["[[Sources/Articles/one]]", "[[Sources/Articles/two]]"]',
            "sources: [" + ", ".join(f'"[[Sources/Articles/{name}]]"' for name in names) + "]",
        )
    )
    write(vault, path, raw)
    for index, name in enumerate(names):
        url = (
            "https://example.net/independent"
            if index == independent_at
            else "https://example.org/shared"
        )
        write(
            vault,
            f"Knowledge Base/Sources/Articles/{name}.md",
            f"---\ntype: source\nsource_type: article\nurl: {url}\nexomem_id: {uuid.uuid4()}\n---\nReport revision {index}.\n",
        )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return path


@pytest.mark.parametrize("independent_at", [None, 1, 11])
def test_origin_discovery_pages_before_eligibility_without_counting_cross_page_copies(
    tmp_path, independent_at
):
    path = fixture_paged_origins(tmp_path, independent_at=independent_at)
    with library_scope():
        first = projection.for_write(tmp_path, path=path)
        assert first["status"] == "warming"
        assert first["continuation"]
        assert not first["items"]
        second = projection.for_write(tmp_path, path=path, continuation=first["continuation"])
        assert second["status"] == "current"
        assert second["continuation"] is None
        assert bool(second["items"]) == (independent_at is not None)
        for result in (first, second):
            assert result["query_budget"]["source_rows"] <= projection.SOURCE_PAGE + 1
        if second["items"]:
            item = second["items"][0]
            more = projection.more_evidence(tmp_path, continuation=item.continuation)
            origins = {entry.origin for entry in (*item.evidence, *more["evidence"])}
            assert len(origins) == 2


def test_recovery_advances_source_discovery_before_an_item_exists(tmp_path):
    from test_vocabulary_delivery import terminal

    from exomem import vocabulary_delivery
    from exomem.vocabulary_state import VocabularyState

    path = fixture_paged_origins(tmp_path, independent_at=11)
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal(path))
        assert result["vocabulary_sync"]["state"] == "warming"
        recovered = vocabulary_delivery.recover(tmp_path)
    assert recovered["state"] == "current"
    assert len(VocabularyState(tmp_path).page()["items"]) == 1
