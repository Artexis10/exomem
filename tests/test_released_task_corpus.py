"""Released task terms retain canonical scoring and complete release dependencies."""

import hashlib
import json
import sqlite3
from pathlib import Path

from exomem import bm25, lexstore
from exomem.governance import artifact_census


def test_source_namespace_proof_binds_the_census_view_consumed_before_publication(vault: Path, monkeypatch) -> None:
    """A newly published withheld name cannot certify text gated against the earlier names."""
    from test_context_catalogue import _page
    from test_governance_egress import _external
    from test_working_set_withheld_twins import _withhold

    from exomem import context_task, context_views
    from exomem.governance import egress, principal

    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    source = _page(vault, "public-source", "prompt: Read future specimen.pdf for the guide.\n")
    target = "Knowledge Base/Notes/Insights/future specimen.pdf"
    _withhold(vault, target, ceiling=0)
    artifact_census.rebuild_published(vault, eager=True)
    request = artifact_census.request_census
    raced = False
    def publish_after_view(root):
        nonlocal raced
        view = request(root)
        if not raced:
            raced = True
            (root / target).write_bytes(b"opaque artifact")
            artifact_census.note_created(root, (target,))
            artifact_census.rebuild_published(root, eager=True)
        return view
    monkeypatch.setattr(artifact_census, "request_census", publish_after_view)
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        caller = context_views.CallerView.current(vault)
        with egress.source_release_proof(vault, basis, source_ref=source) as capture:
            projected = context_task.project_source(vault, source, caller, hydrate=False)
        proof = capture.finish()
        current = context_task.project_source(vault, source, caller, hydrate=False)
    assert projected.public["frontmatter"]["prompt"] != current.public["frontmatter"]["prompt"]
    assert proof is None


def test_source_reference_proof_binds_the_actual_resolution_across_restored_ownership(vault: Path, monkeypatch) -> None:
    """Restoring UUID ownership cannot seal a source resolved while that ownership was absent."""
    from test_context_catalogue import _page
    from test_governance_egress import _external
    from test_working_set_withheld_twins import _withhold

    from exomem import context_task, context_views, freshness, semantic_contract
    from exomem.governance import egress, principal, source_release

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    identity = "12345678-1234-4234-8234-123456789abc"
    target = _page(vault, "identity-target", f"exomem_id: {identity}\n")
    source = _page(vault, "public-reference-source", f"prompt: Read exomem://memory/{identity} for the guide.\n")
    _withhold(vault, target, ceiling=0)
    freshness.rebaseline(vault)
    semantic_contract.build_corpus_context(vault)
    artifact_census.rebuild_published(vault, eager=True)
    original = (vault / target).read_text()
    snapshot = semantic_contract.current_reference_identity_snapshot
    raced = False
    def publish_after_snapshot(root, **kwargs):
        nonlocal raced
        selected = snapshot(root, **kwargs)
        if not raced and selected is not None and source_release._CAPTURE.get() is not None:
            raced = True
            token = source_release._CAPTURE.set(None)
            try:
                (root / target).write_text(original.replace(f"exomem_id: {identity}\n", ""))
                freshness.on_files_changed(root, [root / target])
                semantic_contract.build_corpus_context(root)
            finally:
                source_release._CAPTURE.reset(token)
        return selected
    monkeypatch.setattr(semantic_contract, "current_reference_identity_snapshot", publish_after_snapshot)
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        caller = context_views.CallerView.current(vault)
        with egress.source_release_proof(vault, basis, source_ref=source) as capture:
            projected = context_task.project_source(vault, source, caller, hydrate=False)
        (vault / target).write_text(original)
        freshness.on_files_changed(vault, [vault / target])
        semantic_contract.build_corpus_context(vault)
        proof = capture.finish()
        current = context_task.project_source(vault, source, caller, hydrate=False)
    assert projected.public["frontmatter"]["prompt"] != current.public["frontmatter"]["prompt"]
    assert proof is None


def test_pending_reference_reads_cannot_seal_conflicting_observations(vault: Path, monkeypatch) -> None:
    """Returning to the first pending state cannot certify a different state consumed between reads."""
    from test_governance_egress import _external

    from exomem import memory_refs, pending_recall
    from exomem.governance import egress, principal

    changed = pending_recall.ReferenceProjection({}, frozenset({"withdrawn.md"}), {"withdrawn.md": None})
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        with egress.source_release_proof(vault, basis) as capture:
            for projection in (None, changed, None):
                monkeypatch.setattr(pending_recall, "reference_projection", lambda _root, value=projection: value)
                assert memory_refs._pending_reference_projection(vault) is projection
        assert capture.finish() is None


def test_release_proof_binds_the_decision_bytes_across_an_a_b_a_write(vault: Path, monkeypatch) -> None:
    """Restoring A after an owner authorizes B must not seal B's decision with A's earlier guard."""
    import yaml
    from test_context_catalogue import _page
    from test_governance_egress import _external
    from test_working_set_withheld_twins import _withhold

    from exomem import vault as vault_module
    from exomem.governance import egress, principal, source_release

    path = _page(vault, "changing-reference", "tags: [private-reference]\n", body="Current reference.\n")
    _withhold(vault, path, ceiling=0)
    scope = vault / "Knowledge Base/_Governance/scopes/patterns.yaml"
    definition = yaml.safe_load(scope.read_text())
    definition.pop("paths")
    definition["tags"] = ["private-reference"]
    scope.write_text(yaml.safe_dump(definition))
    raw_a = (vault / path).read_bytes()
    raw_b = raw_a.replace(b"tags: [private-reference]", b"tags: []")
    monkeypatch.setattr(vault_module, "STAT_GENERATION_TRUSTED", False)
    with principal.request_scope(_external()):
        policy = egress.policy_module.load(vault)
        basis = egress.current_source_release_basis(vault)
        with egress.source_release_proof(vault, basis, source_ref=path) as capture:
            source_release.path_dependency(vault, path)
            (vault / path).write_bytes(raw_b)
            decision = egress._decide_path(vault, path, policy=policy, principal=_external(),
                                          audience=_external().audience_id, purpose=None,
                                          grants_hash=egress._grants_hash(policy))
            (vault / path).write_bytes(raw_a)
    assert decision.level == egress.LEVEL_FULL
    assert capture.finish() is None

    # Two owners observing different snapshots cannot be sealed by whichever guard was installed last.
    with principal.request_scope(_external()):
        with egress.source_release_proof(vault, basis, source_ref=path) as capture:
            assert egress._release_level_for_markdown_snapshot(
                vault, path, raw_a, principal=_external(), purpose=None, policy=policy,
            ) == egress.DISCLOSURE_MIN
            (vault / path).write_bytes(raw_b)
            egress.connector_boundary._membership_sets(vault, path, policy, content=raw_b, who=_external())
        assert capture.finish() is None


def test_container_classification_refusal_cannot_use_the_restored_source_image(vault: Path, monkeypatch) -> None:
    """A temporary invalid custody claim must not acquire a reusable proof from the restored ordinary page."""
    import inspect

    from test_context_catalogue import _page
    from test_governance_egress import _external

    from exomem import reserved_paths
    from exomem.governance import egress, principal

    path = _page(vault, "ordinary-classification", body="Public comparison specimen.\n")
    raw_a = (vault / path).read_bytes()
    raw_b = raw_a.replace(b"---\n", b"---\ncontainer_member: null\n", 1)
    read = reserved_paths.read_generic_bytes
    raced = False
    def swapped(root, target, *args, **kwargs):
        nonlocal raced
        if not raced and target == path and inspect.currentframe().f_back.f_code.co_name == "parent_binding_for_path":
            raced = True
            (root / path).write_bytes(raw_b)
            try:
                return read(root, target, *args, **kwargs)
            finally:
                (root / path).write_bytes(raw_a)
        return read(root, target, *args, **kwargs)
    with principal.request_scope(_external()):
        who = principal.current_principal()
        policy = egress.policy_module.load(vault)
        basis = egress.current_source_release_basis(vault)
        def decide():
            return egress._decide_path(vault, path, policy=policy, principal=who,
                                      audience=who.audience_id, purpose=None, grants_hash=egress._grants_hash(policy))
        with egress.source_release_proof(vault, basis, source_ref=path) as control:
            assert decide().level == egress.LEVEL_FULL
        assert control.finish() is not None
        with monkeypatch.context() as patch:
            patch.setattr(reserved_paths, "read_generic_bytes", swapped)
            with egress.source_release_proof(vault, basis, source_ref=path) as capture:
                denied = decide()
        assert raced and denied.level == egress.DISCLOSURE_MIN
        assert decide().level == egress.LEVEL_FULL
        assert capture.finish() is None


def test_idle_view_eviction_releases_capacity_for_ordinary_canonical_preparation(vault: Path, monkeypatch) -> None:
    """Two views and a failed build must not occupy transient parser reservations while idle."""
    import pytest
    from test_context_catalogue import _built, _page
    from test_governance_egress import _external
    from test_lexstore import _seed_live_scopes

    from exomem import context_views, runtime_resources
    from exomem.governance import principal

    paths = {_page(vault, f"capacity-{number}", body="Visible alignment comparison.\n") for number in range(12)}
    _seed_live_scopes(vault)
    _built(vault).close()
    lexstore.release_released_corpus_memory()
    monkeypatch.setattr(runtime_resources, "SEMANTIC_PREPARATION_BYTES", 512 * 1024)
    monkeypatch.setattr(runtime_resources, "SEMANTIC_SMALL_RESERVE_BYTES", 256 * 1024)
    monkeypatch.setattr(lexstore, "_ADMITTED_STATISTICS_MAX", 1)
    store = lexstore.get_store(vault)
    for purpose in ("comparison", "inspection"):
        with principal.request_scope(_external()):
            caller = context_views.CallerView.current(vault, purpose=purpose)
            with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=512 * 1024) as result:
                assert result[0].pages == len(paths)
                with (pytest.raises(runtime_resources.PreparationCapacityBusy),
                      runtime_resources.parser_preparation(0) as first,
                      runtime_resources.parser_preparation(0) as second):
                    first.reserve(first.limit)
                    second.reserve(second.limit)
        assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0
    assert len(store._released_corpus_builds) == 1
    retained = runtime_resources._released_corpus_bytes
    assert retained > 0
    # Concurrent ordinary readers can claim the shared envelope after an idle view yields its rows.
    with runtime_resources.parser_preparation(0) as first, runtime_resources.parser_preparation(0) as second:
        first.reserve(first.limit)
        second.reserve(second.limit)
        assert runtime_resources._preparation_bytes + runtime_resources._released_corpus_bytes <= 512 * 1024
    assert runtime_resources._released_corpus_bytes < retained
    def ends_after_one_source():
        if any(build.preparation_cursor for build in store._released_corpus_builds.values()):
            raise TimeoutError("request ended after a complete source")
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        with pytest.raises(TimeoutError), store.released_task_corpus(
                paths, caller, check=ends_after_one_source, max_bytes=512 * 1024):
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


def test_a_racy_namespace_requires_owner_publication_before_reuse(vault: Path, monkeypatch) -> None:
    """Proof lookup must leave a racy view unchanged until its owner republishes it."""
    from test_governance_egress import _external

    from exomem.governance import egress, principal, source_release

    artifact_census.rebuild_published(vault, eager=True)
    assert artifact_census.current_namespace_proof(vault) is None
    memo = {"namespace": None}
    now = artifact_census.time.time_ns()
    monkeypatch.setattr(artifact_census.time, "time_ns", lambda: now + 2 * artifact_census._CENSUS_RACY_NS)
    assert artifact_census.current_namespace_proof(vault) is None
    artifact_census.rebuild_published(vault, eager=True)
    view = artifact_census.request_census(vault)
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        with egress.source_release_proof(vault, basis, memo=memo) as capture:
            source_release.namespace_dependency(vault, artifact_census.namespace_proof_for_view(vault, view))
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
    artifact_census.rebuild_published(vault, eager=True)
    store = lexstore.get_store(vault)
    original = context_task.project_source
    prepared = []
    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        prepared.append(args[1])
        return result
    monkeypatch.setattr(context_task, "project_source", record)
    def expires():
        if any(build.preparation_cursor == 2 for build in store._released_corpus_builds.values()):
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
    from test_released_corpus_memory import _independent_bytes
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
    artifact_census.rebuild_published(vault, eager=True)
    store = lexstore.get_store(vault)
    def corpus():
        with principal.request_scope(_external()):
            caller = context_views.CallerView.current(vault)
            with store.released_task_corpus({source, independent}, caller, check=lambda: None, max_bytes=64 * 1024 * 1024) as result:
                fresh = {path: bm25.tokenize(json.dumps(context_task.project_source(
                    vault, path, caller, hydrate=False).public, sort_keys=True, ensure_ascii=False)) for path in (source, independent)}
                assert result[0] == bm25.CorpusStatistics.of(fresh)
        build = next(reversed(store._released_corpus_builds.values()))
        identities = set()
        actual = _independent_bytes([build.basis, build.candidates, *build.rows.keys(), *build.rows.values()], identities)
        assert build.payload_bytes == actual
        assert build.epoch.identities.count == len(identities)
        return build
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
    artifact_census.rebuild_published(vault, eager=True)
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
        artifact_census.rebuild_published(vault, eager=True)
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
