"""Released task terms retain canonical scoring and complete release dependencies."""

import hashlib
import json
import sqlite3
from pathlib import Path

from exomem import bm25, lexstore
from exomem.governance import artifact_census


def test_idle_view_eviction_releases_capacity_for_ordinary_canonical_preparation(vault: Path, monkeypatch) -> None:
    """Two views and a failed build must not occupy transient parser reservations while idle."""
    import pytest
    from test_context_catalogue import _built, _page
    from test_governance_egress import _external
    from test_lexstore import _seed_live_scopes

    from exomem import context_views, runtime_resources
    from exomem.governance import principal

    paths = {_page(vault, f"capacity-{number}", body="Visible alignment comparison.\n") for number in range(24)}
    _seed_live_scopes(vault)
    _built(vault).close()
    lexstore.release_released_corpus_memory()
    monkeypatch.setattr(runtime_resources, "SEMANTIC_PREPARATION_BYTES", 192 * 1024)
    monkeypatch.setattr(runtime_resources, "SEMANTIC_SMALL_RESERVE_BYTES", 128 * 1024)
    monkeypatch.setattr(lexstore, "_ADMITTED_STATISTICS_MAX", 1)
    store = lexstore.get_store(vault)
    for purpose in ("comparison", "inspection"):
        with principal.request_scope(_external()):
            caller = context_views.CallerView.current(vault, purpose=purpose)
            with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=192 * 1024) as result:
                assert result[0].pages == len(paths)
        assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0
    assert len(store._released_corpus_builds) == 1
    retained = runtime_resources._released_corpus_bytes
    assert retained > 0
    # Dense small inputs can reserve the entire small-parser allowance through the ordinary owner API.
    with runtime_resources.parser_preparation(0) as reader:
        reader.reserve(reader.limit)
        assert runtime_resources._preparation_bytes + runtime_resources._released_corpus_bytes <= 192 * 1024
    assert runtime_resources._released_corpus_bytes < retained
    def ends_after_one_source():
        if any(build.rows for build in store._released_corpus_builds.values()):
            raise TimeoutError("request ended after a complete source")
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        with pytest.raises(TimeoutError), store.released_task_corpus(
                paths, caller, check=ends_after_one_source, max_bytes=192 * 1024):
            pass
    assert sum(len(build.rows) for build in store._released_corpus_builds.values()) == 1
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0
    with runtime_resources.semantic_preparation(vault, vault / sorted(paths)[0]) as prepared:
        assert "Visible alignment comparison." in prepared.source


def test_a_dangling_link_probe_declines_reuse_when_its_external_target_appears(vault: Path, tmp_path: Path) -> None:
    """An unchanged link name cannot prove a failed final is_file probe stays failed."""
    import pytest
    from test_context_catalogue import _page
    from test_governance_egress import _external
    from test_working_set_withheld_twins import _withhold

    from exomem.governance import egress, principal

    hidden = _page(vault, "hidden-probe", body="Private reference.\n")
    _withhold(vault, hidden, ceiling=0)
    link = vault / "Knowledge Base/Notes/Insights/linked-probe.md"
    target = tmp_path / "outside-target.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    for exists in (False, True):
        if exists:
            target.write_text("External target now exists.")
        with principal.request_scope(_external()):
            basis = egress.current_source_release_basis(vault)
            with egress.source_release_proof(vault, basis) as capture:
                egress.filter_withheld_entries(vault, {"entries": [{"path": link.relative_to(vault).as_posix()}]})
        assert capture.finish() is None


def test_a_racy_namespace_can_become_provable_without_resetting_the_request_memo(vault: Path, monkeypatch) -> None:
    """A previous namespace miss must not force every later canonical source to remain uncacheable."""
    from test_governance_egress import _external

    from exomem.governance import egress, principal, source_release

    artifact_census.rebuild_published(vault, eager=True)
    assert artifact_census.current_namespace_proof(vault) is None
    memo = {"namespace": None}
    now = artifact_census.time.time_ns()
    monkeypatch.setattr(artifact_census.time, "time_ns", lambda: now + 2 * artifact_census._CENSUS_RACY_NS)
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        with egress.source_release_proof(vault, basis, memo=memo) as capture:
            source_release.namespace_dependency(vault)
    proof = capture.finish()
    assert proof is not None and proof.namespace is not None
    assert artifact_census.namespace_proof_is_current(vault, proof.namespace)


def test_effective_session_basis_rechecks_grants_revocation_close_and_clock(vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Fresh conversations share effective grants; a stale verified context supplies no authority."""
    from test_authorization_session_authority import (
        NOW,
        POLICY_DOCUMENTS,
        _connection,
        _custody,
        _mint_and_redeem,
        _open,
    )
    from test_context_catalogue import _page

    from exomem.governance import (
        authorization_custody,
        authorization_session_authority,
        authorization_session_lifecycle,
        egress,
        principal,
        source_release,
        store,
    )

    for relative, raw in POLICY_DOCUMENTS:
        target = vault / "Knowledge Base/_Governance" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    source = _page(vault, "session-source", body="Current source.\n")
    initial, digest = _connection()
    path = tmp_path / "authority.sqlite"
    connection = sqlite3.connect(path)
    initial.backup(connection)
    initial.close()
    custody = _custody(digest)
    first, second = _open(connection, custody), _open(connection, custody)
    monkeypatch.setattr(store, "open_authorization_session_connection", lambda _root: sqlite3.connect(path))
    monkeypatch.setattr(authorization_custody, "load_authorization_custody", lambda _root, **_kwargs: custody)
    monkeypatch.setattr(source_release.time, "time", lambda: NOW + 3)
    def basis(session):
        who = principal.RequestPrincipal(audience_id="principal:person-1").with_verified_authorization_session(
            session.context, issuer_family="mcp-oauth")
        with principal.request_scope(who):
            return egress.current_source_release_basis(vault, purpose="support")
    plain = basis(first)
    assert plain is not None and plain == basis(second)
    assert first.context.session_id not in repr(plain)
    _mint_and_redeem(authorization_session_authority, connection, first, purpose="support", path=source,
                     fingerprint=hashlib.sha256((vault / source).read_bytes()).hexdigest(),
                     reviewed_scope_ids=("01ARZ3NDEKTSV4RRFFQ69G5FAV",),
                     current_scope_ids=("01ARZ3NDEKTSV4RRFFQ69G5FAV",))
    assert basis(first) != plain and basis(first).grants
    assert basis(second) == plain
    authorization_session_authority.revoke_session_grants(
        connection, context=first.context, audience="principal:person-1", now=NOW + 4)
    assert basis(first) == plain
    authorization_session_lifecycle.close_verified_session(connection, custody=custody, context=first.context, now=NOW + 5)
    assert basis(first) is None
    monkeypatch.setattr(source_release.time, "time", lambda: NOW + 601)
    assert basis(second) is None
    connection.close()


def test_managed_origin_consultation_declines_reuse_when_output_is_identical(vault: Path) -> None:
    """Consultation itself adds unsupported evidence, even when the origin projector changes no bytes."""
    from test_context_catalogue import _page
    from test_governance_egress import _external
    from test_working_set_withheld_twins import _withhold

    from exomem.governance import egress, principal

    hidden = _page(vault, "hidden-origin-input", body="Private evidence.\n")
    _withhold(vault, hidden, ceiling=0)
    text = "<!-- This example mentions exomem-origin. -->\nVisible prose."
    page = {"path": "Knowledge Base/Notes/Insights/origin-example.md", "body": text}
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        with egress.source_release_proof(vault, basis) as capture:
            released, changed = egress._project_page_origin(
                vault, page, policy=egress.policy_module.load(vault), principal=_external(), purpose=None, snapshot_content=None)
    assert released == page and not changed
    assert capture.finish() is None


def test_preparation_and_aggregation_resume_without_reading_completed_sources(vault: Path, monkeypatch) -> None:
    """Repeated short requests must make progress without exposing a source-prefix ranking."""
    import pytest
    from test_context_catalogue import _built, _page
    from test_governance_egress import _external
    from test_lexstore import _seed_live_scopes
    from test_working_set_withheld_twins import _withhold

    from exomem import context_task, context_views
    from exomem.governance import principal

    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    hidden = _page(vault, "hidden", body="Private evidence.\n")
    paths = {_page(vault, f"source-{number}", body=f"Visible alignment sample {number}.\n") for number in range(6)}
    _withhold(vault, hidden, ceiling=0)
    _seed_live_scopes(vault)
    _built(vault).close()
    store = lexstore.get_store(vault)
    original = context_task.project_source
    prepared = []
    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        prepared.append(args[1])
        return result
    monkeypatch.setattr(context_task, "project_source", record)
    def expires():
        if any(len(build.rows) == 2 for build in store._released_corpus_builds.values()):
            raise TimeoutError("request ended")
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        with pytest.raises(TimeoutError):
            with store.released_task_corpus(paths, caller, check=expires, max_bytes=64 * 1024 * 1024):
                pass
        build = next(iter(store._released_corpus_builds.values()))
        assert build.result is None and len(build.rows) == 2
        completed = tuple(prepared)
        advance = lexstore._AdmittedCorpusBuild.advance
        def one_page(self, **kwargs):
            advance(self, steps=1, **kwargs)
            raise TimeoutError("aggregation request ended")
        monkeypatch.setattr(lexstore._AdmittedCorpusBuild, "advance", one_page)
        with pytest.raises(TimeoutError):
            with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=64 * 1024 * 1024):
                pass
        assert build.result is None and build.aggregation.cursor == 1
        assert len(prepared) == len(paths) and prepared[:2] == list(completed)
        monkeypatch.setattr(lexstore._AdmittedCorpusBuild, "advance", advance)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=64 * 1024 * 1024) as result:
            statistics, _postings, released, omitted = result
            assert statistics.pages == len(paths) and released == paths and not omitted
        assert len(prepared) == len(paths)
        assert build.retained_bytes >= lexstore._released_size(build)
        assert build.retained_bytes < 64 * 1024 * 1024


def test_reused_terms_reproject_denied_dependencies_even_when_public_json_stays_equal(vault: Path, monkeypatch) -> None:
    """Changing denied B to denied C must replace the proof before C becomes readable."""
    import yaml
    from test_context_catalogue import _built, _page
    from test_governance_egress import _external
    from test_lexstore import _seed_live_scopes
    from test_working_set_withheld_twins import _withhold

    from exomem import context_task, context_views, freshness, working_set_index
    from exomem.governance import principal

    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    target_b = _page(vault, "target-b", "tags: [private-reference]\n", body="Private reference B.\n")
    target_c = _page(vault, "target-c", "tags: [private-reference]\n", body="Private reference C.\n")
    source = _page(vault, "source", body=f"Visible alignment prose.\n\n- [technique] Inspect quartz [[{target_b}]] ^probe\n")
    independent = _page(vault, "independent", body="Independent seed comparison.\n")
    _withhold(vault, target_b, target_c, ceiling=0)
    scope = vault / "Knowledge Base/_Governance/scopes/patterns.yaml"
    definition = yaml.safe_load(scope.read_text())
    definition.pop("paths")
    definition["tags"] = ["private-reference"]
    scope.write_text(yaml.safe_dump(definition))
    _seed_live_scopes(vault)
    _built(vault).close()
    store = lexstore.get_store(vault)
    def corpus():
        with principal.request_scope(_external()):
            caller = context_views.CallerView.current(vault)
            with store.released_task_corpus({source, independent}, caller, check=lambda: None, max_bytes=64 * 1024 * 1024) as result:
                fresh = {path: bm25.tokenize(json.dumps(context_task.project_source(
                    vault, path, caller, hydrate=False).public, sort_keys=True, ensure_ascii=False)) for path in (source, independent)}
                assert result[0] == bm25.CorpusStatistics.of(fresh)
        return next(reversed(store._released_corpus_builds.values()))
    first = corpus()
    before = first.rows[source]
    held = first.rows[independent]
    assert before.proof is not None
    assert corpus().rows[source] is before
    (vault / source).write_text((vault / source).read_text().replace(target_b, target_c))
    freshness.on_files_changed(vault, [vault / source])
    working_set_index.WorkingSetIndex(vault).rebuild()
    changed = corpus()
    after = changed.rows[source]
    assert after.public_version == before.public_version
    assert after.raw_hash != before.raw_hash
    assert after.terms is before.terms
    assert after.proof is not before.proof
    assert changed.rows[independent] is held
    assert target_c in changed.reverse and source in changed.reverse[target_c]
    assert target_b not in changed.reverse or source not in changed.reverse[target_b]
    (vault / target_c).write_text((vault / target_c).read_text().replace("tags: [private-reference]", "tags: []"))
    freshness.on_files_changed(vault, [vault / target_c])
    working_set_index.WorkingSetIndex(vault).rebuild()
    visible = corpus().rows[source]
    assert visible.public_version != before.public_version
    assert "quartz" in visible.terms.terms


def test_a_previous_name_miss_invalidates_the_source_when_a_hidden_target_appears(vault: Path, monkeypatch) -> None:
    """Creating an omitted name must remove its newly withheld unit from an unchanged source's DF."""
    from test_context_catalogue import _built, _page
    from test_governance_egress import _external
    from test_lexstore import _seed_live_scopes
    from test_working_set_withheld_twins import _withhold

    from exomem import context_views, freshness, working_set_index
    from exomem.governance import principal

    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    target = "Knowledge Base/Notes/Insights/future-reference.md"
    source = _page(vault, "name-dependent-source", body="Visible alignment prose.\n\n- [technique] Compare germination using [[future-reference]] ^probe\n")
    _withhold(vault, target, ceiling=0)
    _seed_live_scopes(vault)
    _built(vault).close()
    store = lexstore.get_store(vault)
    word = bm25.tokenize("germination")[0]
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        with store.released_task_corpus({source}, caller, check=lambda: None, max_bytes=64 * 1024 * 1024) as result:
            assert result[0].document_frequency[word] == 1
        old = next(iter(store._released_corpus_builds.values())).rows[source]
        assert ("future-reference", ()) in old.proof.names
        _page(vault, "future-reference", body="Private reference material.\n")
        freshness.on_files_changed(vault, [vault / target])
        working_set_index.WorkingSetIndex(vault).rebuild()
        with store.released_task_corpus({source}, caller, check=lambda: None, max_bytes=64 * 1024 * 1024) as result:
            assert word not in result[0].document_frequency
        current = next(iter(store._released_corpus_builds.values())).rows[source]
        assert current.raw_hash == old.raw_hash and current.public_version != old.public_version


def test_namespace_proof_tracks_nonmatching_names_and_survives_content_edits(tmp_path: Path, monkeypatch) -> None:
    """A newly named binary can change literal redaction of an unchanged source."""
    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    page = tmp_path / "page.md"
    page.write_text("public body")
    assert artifact_census.current_namespace_proof(tmp_path) is None
    artifact_census.rebuild_published(tmp_path, eager=True)
    proof = artifact_census.current_namespace_proof(tmp_path)
    assert proof is not None
    page.write_text("changed body")
    artifact_census.note_touched(tmp_path, ("page.md",))
    assert not artifact_census.namespace_proof_is_current(tmp_path, proof)
    artifact_census.rebuild_published(tmp_path, eager=True)
    assert artifact_census.namespace_proof_is_current(tmp_path, proof)
    (tmp_path / "new-reference.pdf").write_bytes(b"document")
    artifact_census.note_created(tmp_path, ("new-reference.pdf",))
    assert not artifact_census.namespace_proof_is_current(tmp_path, proof)
    artifact_census.rebuild_published(tmp_path, eager=True)
    assert not artifact_census.namespace_proof_is_current(tmp_path, proof)
    fresh = artifact_census.current_namespace_proof(tmp_path)
    artifact_census.withdraw(tmp_path)
    assert not artifact_census.namespace_proof_is_current(tmp_path, fresh)


def test_cooperative_admitted_builder_matches_fresh_order_floor_scores_and_ties() -> None:
    """Resuming between pages and IDF terms cannot alter BM25's floating point order."""
    documents = {"c": ["shared", "rare", "rare"], "a": ["shared", "first"], "b": ["shared", "first"]}
    rows = {path: " ".join(words) for path, words in documents.items()}
    digests = {path: lexstore._text_digest(text) for path, text in rows.items()}
    _statistics, _postings, base = lexstore._admitted_corpus(rows, digests, None)
    builder = lexstore._AdmittedCorpusBuild(base.pages)
    while not builder.advance(steps=1):
        assert builder.result is None
    statistics, postings, _base = builder.result
    expected = bm25.CorpusStatistics.of(documents)
    assert statistics == expected
    assert list(statistics.document_frequency) == list(expected.document_frequency)
    assert statistics.idf_floor < 0
    words = ["shared", "first", "rare"]
    counts = postings.counted_from_page_terms(words, statistics.document_frequency)
    assert statistics.score_counts(postings.documents(documents, counts), words, 3) == expected.score(documents, words, 3)
