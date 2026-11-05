"""Released corpus accounting must leave room for canonical preparation that actually fits."""

import gc
import json
import random
import sys
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from types import MappingProxyType

import pytest
from test_connector_boundary import (
    configured_boundary,  # noqa: F401 - use the maintained binding fixture
)
from test_latency_gate import model_free  # noqa: F401 - reuse the maintained model-free fixture
from test_private_vocabulary_instances import (
    private_instances,  # noqa: F401 - canonical assigned registries
)

from exomem import bm25, context_views, lexstore, runtime_resources
from exomem.governance import principal


def _independent_bytes(roots, seen=None):
    """Use an ordinary Python identity oracle outside the product's accounting epoch."""
    pending = list(roots)
    seen = set() if seen is None else seen
    total = 0
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        total += sys.getsizeof(value)
        if isinstance(value, MappingProxyType):
            pending.extend(gc.get_referents(value))
        elif isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)
        elif is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, field.name) for field in fields(value)
                           if field.name not in {"reservation", "lock", "epoch", "preparation_owner", "memory"})
        elif isinstance(value, (lexstore._AdmittedCorpusBuild, bm25._CorpusStatisticsBuild, bm25.CorpusPostings)):
            pending.append(vars(value))
    return total


def test_compact_identity_splits_preserve_exact_membership_with_admitted_metadata() -> None:
    """Scattered integer identities must not collide or acquire Python-int storage per row."""
    rng = random.Random(7)
    values = [rng.getrandbits(64) for _ in range(5066)]
    identities = lexstore._CompactIdentities()
    admitted = []
    for value in values:
        assert identities.add(value, reserve=admitted.append)
    assert identities.count == len(values)
    assert all(value in identities for value in values)
    assert all(not identities.add(value, reserve=admitted.append) for value in values[-100:])
    actual = (sys.getsizeof(identities) + sys.getsizeof(identities.leaves) + sys.getsizeof(identities.maxima)
              + sys.getsizeof(identities.used) + sys.getsizeof(identities.count) + sys.getsizeof(identities.metadata_bytes)
              + sum(sys.getsizeof(leaf) for leaf in identities.leaves))
    assert identities.metadata_bytes == actual
    assert actual <= 2304 * ((len(values) + 127) // 128) + 4096
    assert max(admitted) >= actual


def test_identity_split_refusal_preserves_existing_membership() -> None:
    """A refused leaf allocation must not partially replace its sorted directory."""
    identities = lexstore._CompactIdentities()
    for value in range(256):
        identities.add(value)
    before = identities.metadata_bytes
    def refuse(_amount):
        raise runtime_resources.PreparationBudgetExceeded("identity metadata does not fit")
    with pytest.raises(runtime_resources.PreparationBudgetExceeded):
        identities.add(256, reserve=refuse)
    assert identities.count == 256 and identities.metadata_bytes == before
    assert all(value in identities for value in range(256)) and 256 not in identities


def test_canonical_child_stays_owned_through_return_and_preserves_its_parent(vault: Path) -> None:
    """Closing the internal source context must not orphan its returned data or free the row's claim."""
    path = vault / "Knowledge Base/Notes/Insights/owned-source.md"
    path.write_text("---\ntype: insight\ntitle: Owned source\n---\nVisible source.\n")
    before = runtime_resources._preparation_bytes
    with runtime_resources.parser_preparation(0, bulk=True) as row:
        row.reserve(65536)
        with runtime_resources.semantic_preparation(vault, path, owner=row) as child:
            assert runtime_resources._preparation_bytes == before + row.reserved + child.reserved
            canonical_bytes = child.reserved
        assert child.reserved == canonical_bytes and child.source
        assert runtime_resources._preparation_bytes == before + row.reserved + canonical_bytes
        with runtime_resources.parser_preparation(0, owner=row) as proof:
            proof.reserve(4096)
            assert runtime_resources._preparation_bytes == before + row.reserved + canonical_bytes + 4096
        assert row.reserved == 65536 and child.reserved == canonical_bytes
        row.replace_phase(1024, workspace=65536)
        assert child.reserved == 0 and not child.source and child.guard is None
        assert row.floor_bytes == 1024
        assert runtime_resources._preparation_bytes == before + row.reserved
    assert runtime_resources._preparation_bytes == before


def test_atomic_resident_transfer_keeps_live_row_floor() -> None:
    """Resident growth can use surplus credit while a public representation and capture stay live."""
    with runtime_resources.parser_preparation(0, bulk=True) as row:
        row.reserve(100000)
        row.floor_bytes = 70000
        resident = runtime_resources.ReleasedCorpusMemory(1024 * 1024)
        try:
            resident.reserve(50000, transient=row)
            assert resident.reserved == 50000 and row.reserved == 70000
        finally:
            resident.release()


@pytest.mark.usefixtures("model_free")
def test_failed_reverse_allocation_rolls_back_the_unregistered_row(tmp_path: Path, monkeypatch) -> None:
    """Failure after row insertion must not leave its payload outside the accounting epoch."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    install = lexstore.ReleasedCorpusBuild.install
    failed = False
    def fail_reverse_set(self, path, row):
        nonlocal failed
        if not failed:
            failed = True
            def refused(*args, **kwargs):
                raise MemoryError("reverse-set allocation failed after row insertion")
            with monkeypatch.context() as patch:
                patch.setattr(lexstore, "set", refused, raising=False)
                return install(self, path, row)
        return install(self, path, row)
    monkeypatch.setattr(lexstore.ReleasedCorpusBuild, "install", fail_reverse_set)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with pytest.raises(MemoryError), store.released_task_corpus(
                paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
        build = next(iter(store._released_corpus_builds.values()))
        assert failed and not build.rows and not build.reverse and not build.retired
        assert runtime_resources._preparation_bytes == 0
        assert build.retained_bytes >= _independent_bytes([build]) + build.accounting_bytes
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)


@pytest.mark.usefixtures("model_free")
def test_warm_validation_reads_each_directory_once_per_fresh_phase(tmp_path: Path, monkeypatch) -> None:
    """Borrowed published rows share one physical phase without rebuilding unchanged accounting state."""
    import inspect
    import os
    from collections import Counter

    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem.governance import source_release
    from exomem.vocabulary import registry

    root = _build_dense_vault(tmp_path, 4)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)
        build = next(iter(store._released_corpus_builds.values()))
        before = tuple(getattr(build, name) for name in (
            "checkpoint", "validation", "validation_cursor", "preparation", "preparation_cursor",
            "aggregation", "result", "fingerprint", "epoch",
        ))
        def refreshed(*args, **kwargs):
            pytest.fail("an unchanged published borrow must not walk mutable build accounting")
        monkeypatch.setattr(lexstore, "_refresh_released_memory", refreshed)
        request_floor = lexstore._request_floor
        request_charge = []
        def charged(build, refs, workspace, **kwargs):
            floor = request_floor(build, refs, workspace, **kwargs)
            request_charge.append((workspace, floor))
            return floor
        monkeypatch.setattr(lexstore, "_request_floor", charged)
        reads = []
        registries = []
        scandir = os.scandir
        load = registry.load
        def observed(path):
            if inspect.currentframe().f_back.f_code.co_filename.endswith("/governance/source_release.py"):
                reads.append(Path(path).relative_to(root).as_posix())
            return scandir(path)
        def loaded(spec, *args, **kwargs):
            if inspect.currentframe().f_back.f_code.co_filename.endswith("/governance/source_release.py"):
                registries.append((spec.name, spec.instance_id))
            return load(spec, *args, **kwargs)
        monkeypatch.setattr(os, "scandir", observed)
        monkeypatch.setattr(registry, "load", loaded)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)
            workspace, incoming_floor = request_charge[-1]
            assert workspace.floor_bytes >= incoming_floor + sys.getsizeof(result[2]) + sys.getsizeof(result)
    assert reads and set(Counter(reads).values()) == {1}
    assert registries and set(Counter(registries).values()) == {2}
    assert all(current is previous for current, previous in zip((getattr(build, name) for name in (
        "checkpoint", "validation", "validation_cursor", "preparation", "preparation_cursor",
        "aggregation", "result", "fingerprint", "epoch",
    )), before, strict=True))
    build = next(iter(store._released_corpus_builds.values()))
    proof = build.rows[paths[0]].proof
    changed_expectation = replace(proof, registries=tuple(
        (*fact[:3], "0" * 64, fact[4]) for fact in proof.registries
    ))
    facts = source_release._ValidationFacts()
    assert source_release.validate_source_release_proofs(
        root, build.basis, (proof, changed_expectation), check_paths=False,
        directory_facts=facts, memory=lambda extra=0: None,
    ) == (True, False)
    facts.clear()


def test_phase_directory_facts_preserve_canonical_matching_and_charge_shared_names(tmp_path: Path) -> None:
    """Exact names win; folded collisions retain the resolver's first match without charging values twice."""
    from exomem.governance import egress, source_release

    for name in ("Case", "case", "Straße", "A", "a"):
        (tmp_path / name).touch()
    facts = source_release._ValidationFacts()
    admitted = []
    def reserve(extra=0):
        assert extra >= facts.retained_bytes
        admitted.append(extra)
    for name in ("Case", "case", "CASE", "STRASSE", "absent"):
        names = dict.fromkeys(path.name for path in tmp_path.iterdir())
        assert facts.match(tmp_path, ".", name, memory=reserve) == egress._physical_directory_match(names, name)
    from exomem.vocabulary import instances, registry, registry_spec

    spec = instances.select(tmp_path, registry_spec("relations"))
    current = registry.load(spec, tmp_path)
    expected = spec.binding_revision, current.content_hash, current.effective_digest
    assert facts.registry_tuple(tmp_path, "relations", None, memory=reserve) == expected
    assert facts.registries_current(tmp_path, memory=reserve)
    actual = (_independent_bytes([facts.directories, facts.registries])
              + sys.getsizeof(facts) + sys.getsizeof(facts.retained_bytes))
    assert actual <= facts.retained_bytes
    assert max(admitted) >= actual
    facts.clear()
    assert not facts.directories and not facts.registries


def test_registry_phase_refuses_growth_before_reading_or_retaining_the_tuple(tmp_path: Path, monkeypatch) -> None:
    """Admission must precede the canonical miss read and leave no partially retained observation."""
    from exomem.governance import source_release

    facts = source_release._ValidationFacts()
    before = facts.retained_bytes
    def refused(extra=0):
        assert extra >= before
        raise runtime_resources.PreparationBudgetExceeded("registry facts do not fit")
    def read(*args, **kwargs):
        pytest.fail("the refused owner tuple must not be read")
    monkeypatch.setattr(source_release, "_current_registry_tuple", read)
    with pytest.raises(runtime_resources.PreparationBudgetExceeded):
        facts.registry_tuple(tmp_path, "relations", None, memory=refused)
    assert not facts.registries and facts.retained_bytes == before


@pytest.mark.usefixtures("model_free")
def test_borrow_cancellation_while_charging_new_request_refs_keeps_the_published_build(tmp_path: Path, monkeypatch) -> None:
    """A new request must own its references before reading proofs or changing a published generation."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem.governance import egress

    root = _build_dense_vault(tmp_path, 3)
    paths = {path.relative_to(root).as_posix() for path in root.rglob("*.md")}
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
        build = next(iter(store._released_corpus_builds.values()))
        result, epoch, charge = build.result, build.epoch, build.reservation.reserved
        fresh_paths = {path.encode().decode() for path in paths}
        def expired(_build, refs, _workspace, **kwargs):
            assert refs[0] is fresh_paths
            raise TimeoutError("request ended while charging its references")
        def unexpected(*args, **kwargs):
            pytest.fail("no proof or build accounting may precede the new request charge")
        monkeypatch.setattr(lexstore, "_request_floor", expired)
        monkeypatch.setattr(lexstore, "_refresh_released_memory", unexpected)
        monkeypatch.setattr(egress, "validate_source_release_proofs", unexpected)
        with pytest.raises(TimeoutError), store.released_task_corpus(
                fresh_paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pytest.fail("an expired request must not publish")
        assert build.result is result and build.epoch is epoch and build.reservation.reserved == charge
    assert runtime_resources._preparation_bytes == 0


@pytest.mark.usefixtures("model_free")
def test_unobserved_source_edit_on_borrow_reprojects_only_the_affected_source(tmp_path: Path, monkeypatch) -> None:
    """Complete empty history cannot hide a source edit discovered by the borrow's physical guards."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem import context_task, freshness

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
        build = next(iter(store._released_corpus_builds.values()))
        previous = dict(build.rows)
        checkpoint = freshness.consumer_checkpoint(root, "vault")
        edited = paths[0], paths[-1]
        for path in edited:
            (root / path).write_text((root / path).read_text() + "\nUnobserved additional specimen.\n")
        assert freshness.consumer_checkpoint(root, "vault") == checkpoint
        prepared = []
        project = context_task.project_source
        def projected(root, path, *args, **kwargs):
            prepared.append(path)
            return project(root, path, *args, **kwargs)
        monkeypatch.setattr(context_task, "project_source", projected)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            fresh = {path: bm25.tokenize(json.dumps(project(root, path, caller, hydrate=False).public,
                                                  sort_keys=True, ensure_ascii=False)) for path in paths}
            assert result[0] == bm25.CorpusStatistics.of(fresh)
        assert prepared == list(edited)
        assert all(build.rows[path] is not previous[path] for path in edited)
        assert build.rows[paths[1]] is previous[paths[1]]


def test_borrow_rechecks_a_proven_omission_after_an_unobserved_membership_edit(vault: Path, monkeypatch) -> None:
    """A changed omitted source must be prepared again while an independent released row stays reusable."""
    import yaml
    from test_context_catalogue import _built, _page
    from test_governance_egress import _external
    from test_lexstore import _seed_live_scopes
    from test_working_set_withheld_twins import _withhold

    from exomem import context_task
    from exomem.governance import artifact_census

    hidden = _page(vault, "withheld-source", "tags: [private-reference]\n", body="Newly visible specimen.\n")
    visible = _page(vault, "independent", body="Independent public specimen.\n")
    _withhold(vault, hidden, ceiling=0)
    scope = vault / "Knowledge Base/_Governance/scopes/patterns.yaml"
    definition = yaml.safe_load(scope.read_text())
    definition.pop("paths")
    definition["tags"] = ["private-reference"]
    scope.write_text(yaml.safe_dump(definition))
    _seed_live_scopes(vault)
    _built(vault).close()
    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)
    artifact_census.rebuild_published(vault, eager=True)
    store = lexstore.get_store(vault)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        with store.released_task_corpus({visible, hidden}, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == 1
        build = next(iter(store._released_corpus_builds.values()))
        assert build.result is not None and build.omissions[hidden].proof is not None
        previous = build.omissions[hidden]
        independent = build.rows[visible]
    path = vault / hidden
    path.write_text(path.read_text().replace("tags: [private-reference]", "tags: []"))
    prepared = []
    project = context_task.project_source
    def projected(root, path, *args, **kwargs):
        prepared.append(path)
        return project(root, path, *args, **kwargs)
    monkeypatch.setattr(context_task, "project_source", projected)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        fresh = {}
        for relative in (visible, hidden):
            source = project(vault, relative, caller, hydrate=False)
            if source is not None:
                fresh[relative] = bm25.tokenize(json.dumps(source.public, sort_keys=True, ensure_ascii=False))
        with store.released_task_corpus({visible, hidden}, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0] == bm25.CorpusStatistics.of(fresh)
            assert result[2] == frozenset(fresh)
        assert prepared == [hidden]
        current = build.rows.get(hidden) or build.omissions[hidden]
        assert current.proof is not None and current.proof is not previous.proof
        assert build.rows[visible] is independent


@pytest.mark.usefixtures("model_free")
@pytest.mark.parametrize("failure", [None, "after_eviction", "after_insertion"])
def test_same_slot_postings_changes_refresh_the_retained_graph(tmp_path: Path, monkeypatch, failure) -> None:
    """Equal slot totals and failures after mutation must not hide changed retained cache memory."""
    from collections import OrderedDict

    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem import freshness

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    (root / paths[0]).write_text((root / paths[0]).read_text() + "\n" + "a" * 2048 + "\n")
    freshness.on_files_changed(root, [root / paths[0]])
    store = lexstore.get_store(root)
    monkeypatch.setattr(bm25, "POSTINGS_SLOTS_MAX", 1)
    armed = False
    class FailedEviction(OrderedDict):
        def popitem(self, *args, **kwargs):
            result = super().popitem(*args, **kwargs)
            if armed:
                raise MemoryError("allocation failed after cache eviction")
            return result
    if failure == "after_eviction":
        monkeypatch.setattr(bm25, "OrderedDict", FailedEviction)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            statistics, postings = result[:2]
            short = next(word for word, count in statistics.document_frequency.items() if count == 1 and len(word) < 10)
            long = next(word for word, count in statistics.document_frequency.items() if count == 1 and len(word) >= 2048)
            postings.counted_from_page_terms([short.encode().decode()], statistics.document_frequency)
        build = next(iter(store._released_corpus_builds.values()))
        old_charge, slots = build.reservation.reserved, postings._slots
        refresh = lexstore._refresh_released_memory
        refreshed = []
        def measured(*args, **kwargs):
            refreshed.append(True)
            return refresh(*args, **kwargs)
        monkeypatch.setattr(lexstore, "_refresh_released_memory", measured)
        keep = bm25.CorpusPostings._keep
        if failure == "after_insertion":
            def inserted(self, *args, **kwargs):
                keep(self, *args, **kwargs)
                raise MemoryError("request ended after cache insertion")
            monkeypatch.setattr(bm25.CorpusPostings, "_keep", inserted)
        armed = True
        with pytest.raises(MemoryError) if failure else nullcontext():
            with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
                assert result[1] is postings
                postings.counted_from_page_terms([long.encode().decode()], statistics.document_frequency)
        assert refreshed
        assert postings._slots == slots
        assert build.reservation.reserved != old_charge
        assert build.retained_bytes >= _independent_bytes([build]) + build.accounting_bytes
    assert runtime_resources._preparation_bytes == 0


@pytest.mark.usefixtures("private_instances")
def test_phase_close_detects_a_canonical_registry_binding_change(vault: Path, request) -> None:
    """An assigned instance must not close under the storage binding that preceded a rearm."""
    from exomem import state_migration
    from exomem.governance import source_release

    with principal.library_scope():
        config, _authenticate = request.getfixturevalue("configured_boundary")
        facts = source_release._ValidationFacts()
        before = facts.registry_tuple(vault, "relations", None, memory=lambda extra=0: None)
        document = json.loads(config.read_text())
        document["vocabulary"]["public"]["namespace"] = "Knowledge Base/_Schema/public-next"
        config.write_text(json.dumps(document))
        authority = state_migration.assert_offline_migration_authority(source="disposable registry binding race")
        state_migration.arm_connector_boundary_offline(vault, authority=authority)
        assert not facts.registries_current(vault, memory=lambda extra=0: None)
        assert facts.registries["relations", None] is before
        facts.clear()


@pytest.mark.usefixtures("model_free")
def test_final_registry_close_withdraws_an_overlay_edit_without_a_basis_change(tmp_path: Path, monkeypatch) -> None:
    """An overlay edited after its observed proof must prevent publication until dependent rows reproject."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem import relation_registry
    from exomem.governance import egress, source_release
    from exomem.vocabulary import registry

    root = _build_dense_vault(tmp_path, 3)
    overlay = relation_registry.SPEC.overlay(root)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nrelations: {}\n")
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
        build = next(iter(store._released_corpus_builds.values()))
        before = build.rows[paths[0]]
        basis = egress.current_source_release_basis(root)
        close = source_release._ValidationFacts.registries_current
        closes = []
        def edited(self, root, **kwargs):
            closes.append(self)
            if len(closes) == 1:
                overlay.write_text(overlay.read_text() + "\n# A later overlay revision.\n")
                registry.invalidate(root)
                assert egress.current_source_release_basis(root) == basis
            return close(self, root, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(source_release._ValidationFacts, "registries_current", edited)
            with pytest.raises(RuntimeError, match="changed during preparation"), store.released_task_corpus(
                    paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
                pytest.fail("a changed registry phase must not publish")
        assert build.result is None and build.rows[paths[0]] is before
        assert all(not phase.registries and not phase.directories for phase in closes)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)
        assert build.rows[paths[0]].proof.registries != before.proof.registries


@pytest.mark.parametrize("phase", ["names", "folded"])
def test_phase_directory_allocation_refusal_keeps_only_admitted_facts(tmp_path: Path, phase) -> None:
    """Refusing name retention or lazy folding must leave no partially published table."""
    from exomem.governance import source_release

    (tmp_path / "Visible").touch()
    facts = source_release._ValidationFacts()
    if phase == "folded":
        assert facts.match(tmp_path, ".", "Visible", memory=lambda extra=0: None) == "Visible"
    before = facts.retained_bytes
    def refuse(extra=0):
        if extra > facts.retained_bytes + 256:
            raise runtime_resources.PreparationBudgetExceeded("directory facts do not fit")
    with pytest.raises(runtime_resources.PreparationBudgetExceeded):
        facts.match(tmp_path, ".", "VISIBLE", memory=refuse)
    assert facts.retained_bytes == before
    assert not facts.directories if phase == "names" else facts.directories["."][1] is None
    facts.clear()


@pytest.mark.usefixtures("model_free")
@pytest.mark.parametrize("failure", [TimeoutError, runtime_resources.PreparationBudgetExceeded,
                                      runtime_resources.PreparationCapacityBusy, MemoryError, OSError])
def test_validation_failures_clear_phase_facts_without_canonical_rebuild(tmp_path: Path, monkeypatch, failure) -> None:
    """Cancellation and admission failure must propagate instead of becoming an invalid-proof cache miss."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem import context_task, vault
    from exomem.governance import source_release

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
        phases = []
        initialize = source_release._ValidationFacts.__init__
        def initialized(self):
            initialize(self)
            phases.append(self)
        def refused(*args, **kwargs):
            raise failure("proof validation ended")
        def rebuilt(*args, **kwargs):
            pytest.fail("a preparation failure must not reproject a source")
        monkeypatch.setattr(source_release._ValidationFacts, "__init__", initialized)
        if failure is TimeoutError:
            monkeypatch.setattr(vault.PathGuard, "recheck", refused)
        else:
            import inspect

            read = source_release._current_registry_tuple
            def current(*args, **kwargs):
                if inspect.currentframe().f_back.f_code.co_name == "registries_current":
                    return refused()
                return read(*args, **kwargs)
            monkeypatch.setattr(source_release, "_current_registry_tuple", current)
        monkeypatch.setattr(context_task, "project_source", rebuilt)
        expected = RuntimeError if failure is OSError else failure
        with pytest.raises(expected), store.released_task_corpus(
                paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pytest.fail("failed validation must not publish")
    assert phases and all(not phase.directories and not phase.registries for phase in phases)
    assert runtime_resources._preparation_bytes == 0


@pytest.mark.usefixtures("model_free")
def test_final_phase_cannot_publish_after_observing_a_new_source_generation(tmp_path: Path, monkeypatch) -> None:
    """A source edit after its final proof check must invalidate the phase before any ranking publishes."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem import freshness
    from exomem.governance import egress

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
        validate = egress.validate_source_release_proofs
        phases = []
        checked = 0
        changed = False
        def validated(root, basis, proofs, **kwargs):
            nonlocal changed, checked
            result = validate(root, basis, proofs, **kwargs)
            phase = kwargs.get("directory_facts")
            if phase is not None and all(phase is not current for current in phases):
                phases.append(phase)
            if kwargs.get("check_paths", True):
                checked += 1
            if checked == len(paths) and not changed:
                changed = True
                path = root / paths[0]
                path.write_text(path.read_text() + "\nA later source generation.\n")
                freshness.on_files_changed(root, [path])
            return result
        monkeypatch.setattr(egress, "validate_source_release_proofs", validated)
        with pytest.raises(RuntimeError, match="changed during preparation"), store.released_task_corpus(
                paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pytest.fail("a superseded phase must not publish")
        build = next(iter(store._released_corpus_builds.values()))
        assert changed and build.result is None and all(not phase.directories for phase in phases)


@pytest.mark.usefixtures("model_free")
def test_proof_memory_callback_keeps_the_whole_returned_source_live(tmp_path: Path, monkeypatch) -> None:
    """Later proof workspace must add to the live source instead of replacing its floor with capture-only data."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    from exomem import context_task
    from exomem.governance import source_release

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    project, scope, validate = context_task.project_source, source_release.source_release_proof, source_release.validate_source_release_proofs
    observed = {}
    measure = lexstore._released_size
    validating = False
    walks = 0
    checkpoints = []
    def measured(*args, **kwargs):
        nonlocal walks
        if validating:
            walks += 1
        return measure(*args, **kwargs)
    def projected(*args, **kwargs):
        result = project(*args, **kwargs)
        observed["source"] = result
        return result
    @contextmanager
    def captured(*args, **kwargs):
        with scope(*args, **kwargs) as capture:
            observed["capture"] = capture
            yield capture
    def validated(root, basis, proofs, **kwargs):
        nonlocal validating
        if kwargs.get("memory") is not None:
            kwargs["memory"](extra=20000)
            build = next(iter(lexstore.get_store(root)._released_corpus_builds.values()))
            forest = set()
            _independent_bytes([build.basis, build.candidates, *build.rows.keys(), *build.rows.values()], forest)
            expected = _independent_bytes([observed["source"], observed["capture"], observed["capture"].memo, *proofs], forest)
            assert kwargs["preparation_owner"].floor_bytes >= expected
        before = walks
        validating = True
        try:
            return validate(root, basis, proofs, **kwargs)
        finally:
            validating = False
            checkpoints.append(walks - before)
    monkeypatch.setattr(lexstore, "_released_size", measured)
    monkeypatch.setattr(context_task, "project_source", projected)
    monkeypatch.setattr(source_release, "source_release_proof", captured)
    monkeypatch.setattr(source_release, "validate_source_release_proofs", validated)
    # The public re-export is the same owner API used by the row preparer.
    from exomem.governance import egress
    monkeypatch.setattr(egress, "source_release_proof", captured)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with lexstore.get_store(root).released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
    assert checkpoints and all(count == 2 for count in checkpoints)


@pytest.mark.parametrize("proofs", [(), (object(),)])
def test_empty_or_invalid_proof_validation_still_admits_its_result(tmp_path: Path, proofs) -> None:
    """Coalescing supported-proof work must preserve admission when no supported proof is present."""
    from exomem.governance import source_release

    def refused(extra=0):
        raise runtime_resources.PreparationBudgetExceeded("validation result does not fit")
    with pytest.raises(runtime_resources.PreparationBudgetExceeded):
        source_release.validate_source_release_proofs(tmp_path, None, proofs, memory=refused)


def test_legacy_directory_growth_reserves_the_sum_before_retaining_every_name(vault: Path, monkeypatch) -> None:
    """Several directory snapshots must refuse during growth instead of after all names enter the memo."""
    import os

    from test_context_catalogue import _page
    from test_governance_egress import _external
    from test_working_set_withheld_twins import _withhold

    from exomem.governance import egress, source_release

    hidden = _page(vault, "hidden", body="Private source.\n")
    _withhold(vault, hidden, ceiling=0)
    directories = []
    for number in range(3):
        directory = vault / "Knowledge Base/Notes" / f"long-names-{number}"
        directory.mkdir(parents=True, exist_ok=True)
        for entry in range(180):
            (directory / ("x" * 180 + f"-{entry:03d}.txt")).touch()
        directories.append(directory)
    with principal.request_scope(_external()):
        basis = egress.current_source_release_basis(vault)
        with source_release.source_release_proof(vault, basis) as capture:
            egress.filter_withheld_entries(vault, [
                {"path": directory.relative_to(vault).as_posix() + "/absent.md"}
                for directory in directories
            ])
        proof = capture.finish()
    assert proof is not None and all(
        any(relative == directory.relative_to(vault).as_posix() for relative, _name, _resolved in proof.directories)
        for directory in directories
    )
    capture.memo.clear()
    memo = {}
    seen_names = 0
    scan = os.scandir
    @contextmanager
    def scanned(path):
        nonlocal seen_names
        with scan(path) as entries:
            def counted():
                nonlocal seen_names
                for entry in entries:
                    if Path(path) in directories:
                        seen_names += 1
                    yield entry
            yield counted()
    monkeypatch.setattr(os, "scandir", scanned)
    with runtime_resources.parser_preparation(0, bulk=True, limit_bytes=192 * 1024) as owner:
        def memory(extra=0):
            live = _independent_bytes([proof, memo])
            owner.reserve(live + extra + 65536)
            owner.floor_bytes = live
        try:
            with pytest.raises(runtime_resources.PreparationBudgetExceeded):
                source_release.validate_source_release_proofs(
                    vault, basis, (proof,), memo=memo, preparation_owner=owner, memory=memory,
                )
            assert seen_names < 180 * len(directories)
            assert any(("directory", directory.relative_to(vault).as_posix()) in memo for directory in directories)
        finally:
            memo.clear()


@pytest.mark.usefixtures("model_free")
def test_cancelled_row_measurement_discards_partial_identities_before_resume(tmp_path: Path, monkeypatch) -> None:
    """A cancelled identity insertion must not suppress objects when their addresses are later reused."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    root = _build_dense_vault(tmp_path, 8)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    register, add = lexstore._ReleasedEpoch.register, lexstore._CompactIdentities.add
    inserting = False
    added = 0
    failed = []
    def registered(self, value, *args, **kwargs):
        nonlocal inserting
        inserting = isinstance(value, lexstore.ReleasedSourceTerms)
        try:
            return register(self, value, *args, **kwargs)
        finally:
            inserting = False
            value = None
    def interrupted(self, value, **kwargs):
        nonlocal added
        fresh = add(self, value, **kwargs)
        if inserting and fresh:
            added += 1
            if added == 5:
                failed.append(self)
                raise TimeoutError("request ended during row accounting")
        return fresh
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with monkeypatch.context() as patch:
            patch.setattr(lexstore._ReleasedEpoch, "register", registered)
            patch.setattr(lexstore._CompactIdentities, "add", interrupted)
            with pytest.raises(TimeoutError), store.released_task_corpus(
                    paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
                pass
        assert failed and failed[0].count == 0
        assert runtime_resources._preparation_bytes == 0
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)
    build = next(iter(store._released_corpus_builds.values()))
    identities = set()
    actual = _independent_bytes([build.basis, build.candidates, *build.rows.keys(), *build.rows.values()], identities)
    assert build.payload_bytes == actual
    assert build.epoch.identities.count == len(identities)


@pytest.mark.usefixtures("model_free")
def test_idle_custody_facts_are_admitted_and_withdraw_on_pressure(tmp_path: Path, monkeypatch) -> None:
    """A receipt queue must stay charged, and admission failure must withdraw the view without eviction recursion."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    root = _build_dense_vault(tmp_path, 3)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pass
    build = next(iter(store._released_corpus_builds.values()))
    path = paths[0].encode().decode()
    before = build.reservation.reserved
    lexstore._released_custody_apply(root, (path,), (), {path: "f" * 64})
    assert path in build.pending and not build.invalidated
    assert build.reservation.reserved == before + sys.getsizeof(path)
    assert lexstore._released_custody_verify(root, (path,), {path: "f" * 64}) == ()
    monkeypatch.setattr(build.reservation, "limit", build.reservation.reserved)
    lexstore._released_custody_apply(root, (paths[1],), (), {paths[1]: "e" * 64})
    assert build.invalidated and build.pending == {path}
    assert build.reservation.reserved == build.retained_bytes
    assert lexstore._released_custody_verify(root, (paths[1],), {paths[1]: "e" * 64}) == ()


@pytest.mark.usefixtures("model_free")
def test_custody_does_not_modify_an_active_accounting_epoch(tmp_path: Path) -> None:
    """An active lease can only be invalidated; its immutable roots and reservation stay owned."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    root = _build_dense_vault(tmp_path, 2)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            build = next(iter(store._released_corpus_builds.values()))
            epoch, row, charge = build.epoch, build.rows[paths[0]], build.reservation.reserved
            lexstore._released_custody_apply(root, (paths[0],), (), {paths[0]: "e" * 64})
            assert build.invalidated and not build.pending
            assert build.epoch is epoch and build.rows[paths[0]] is row
            assert build.reservation.reserved == charge


@pytest.mark.usefixtures("model_free")
def test_shared_row_accounting_does_not_refuse_the_next_canonical_source(tmp_path: Path, monkeypatch) -> None:
    """Charging common terms repeatedly used to refuse source 83 of a small, ordinary corpus."""
    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    root = _build_dense_vault(tmp_path, 128)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    lexstore.release_released_corpus_memory()
    monkeypatch.setattr(runtime_resources, "_preparation_peak", 0)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with lexstore.get_store(root).released_task_corpus(
                paths, caller, check=lambda: None, max_bytes=1024 * 1024) as result:
            assert result[0].pages == len(paths)
        published = next(iter(lexstore.get_store(root)._released_corpus_builds.values())).result
        with lexstore.get_store(root).released_task_corpus(
                paths, caller, check=lambda: None, max_bytes=1024 * 1024) as result:
            assert result[0] is published[0] and result[1] is published[1]
    peak = runtime_resources.semantic_preparation_status()["peak_reserved_bytes"]
    build = next(iter(lexstore.get_store(root)._released_corpus_builds.values()))
    assert len(build.rows) == len(paths) and build.result is not None
    forest_ids = set()
    forest = _independent_bytes([build.basis, build.candidates, *build.rows.keys(), *build.omissions.keys(),
                                 *build.rows.values(), *build.omissions.values()], forest_ids)
    retained = _independent_bytes([build])
    identities = build.epoch.identities
    metadata = (sys.getsizeof(build.epoch) + sys.getsizeof(build.epoch.payload_bytes)
                + sys.getsizeof(identities) + sys.getsizeof(identities.leaves)
                + sys.getsizeof(identities.maxima) + sys.getsizeof(identities.used)
                + sys.getsizeof(identities.count) + sys.getsizeof(identities.metadata_bytes)
                + sum(sys.getsizeof(leaf) for leaf in identities.leaves))
    assert identities.count == len(forest_ids) and all(value in identities for value in forest_ids)
    assert build.accounting_bytes == metadata
    assert build.payload_bytes == forest
    assert build.retained_bytes >= retained + metadata
    assert build.retained_bytes <= retained + metadata + 1024
    assert peak <= 1024 * 1024
    word = bm25.tokenize("body")[0]
    common = [next(term for term in row.terms.terms if term == word) for row in build.rows.values()]
    assert all(term is common[0] for term in common)
    print("RELEASED_MEMORY " + json.dumps({
        "rows": len(build.rows), "payload_bytes": build.payload_bytes, "identity_metadata_bytes": metadata,
        "mutable_bytes": build.mutable_bytes, "charged_resident_bytes": build.retained_bytes,
        "independent_retained_bytes": retained, "combined_peak_reserved_bytes": peak,
        "workspace_high_water_bound_bytes": build.workspace_peak_bytes,
    }, sort_keys=True))
