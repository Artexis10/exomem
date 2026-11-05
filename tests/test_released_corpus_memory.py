"""Released corpus accounting must leave room for canonical preparation that actually fits."""

import gc
import json
import random
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import MappingProxyType

import pytest
from test_latency_gate import model_free  # noqa: F401 - reuse the maintained model-free fixture

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
    """Sibling sources must share directory facts while final validation reads a fresh phase."""
    import inspect
    import os
    from collections import Counter

    from test_governance_egress import _external
    from test_latency_gate import _build_dense_vault

    root = _build_dense_vault(tmp_path, 4)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")))
    store = lexstore.get_store(root)
    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(root)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)
        reads = []
        scandir = os.scandir
        def observed(path):
            if inspect.currentframe().f_back.f_code.co_filename.endswith("/governance/source_release.py"):
                reads.append(Path(path).relative_to(root).as_posix())
            return scandir(path)
        monkeypatch.setattr(os, "scandir", observed)
        with store.released_task_corpus(paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024) as result:
            assert result[0].pages == len(paths)
    assert reads and set(Counter(reads).values()) == {2}


def test_phase_directory_facts_preserve_canonical_matching_and_charge_shared_names(tmp_path: Path) -> None:
    """Exact names win; folded collisions retain the resolver's first match without charging values twice."""
    from exomem.governance import egress, source_release

    for name in ("Case", "case", "Straße", "A", "a"):
        (tmp_path / name).touch()
    facts = source_release._DirectoryFacts()
    admitted = []
    def reserve(extra=0):
        admitted.append(facts.retained_bytes + extra)
    for name in ("Case", "case", "CASE", "STRASSE", "absent"):
        names = dict.fromkeys(path.name for path in tmp_path.iterdir())
        assert facts.match(tmp_path, ".", name, memory=reserve) == egress._physical_directory_match(names, name)
    actual = _independent_bytes([facts.directories]) + sys.getsizeof(facts) + sys.getsizeof(facts.retained_bytes)
    assert actual <= facts.retained_bytes <= actual + 64
    assert max(admitted) >= actual
    facts.clear()
    assert not facts.directories


@pytest.mark.parametrize("phase", ["names", "folded"])
def test_phase_directory_allocation_refusal_keeps_only_admitted_facts(tmp_path: Path, phase) -> None:
    """Refusing name retention or lazy folding must leave no partially published table."""
    from exomem.governance import source_release

    (tmp_path / "Visible").touch()
    facts = source_release._DirectoryFacts()
    if phase == "folded":
        assert facts.match(tmp_path, ".", "Visible", memory=lambda extra=0: None) == "Visible"
    before = facts.retained_bytes
    def refuse(extra=0):
        if extra > 256:
            raise runtime_resources.PreparationBudgetExceeded("directory facts do not fit")
    with pytest.raises(runtime_resources.PreparationBudgetExceeded):
        facts.match(tmp_path, ".", "VISIBLE", memory=refuse)
    assert facts.retained_bytes == before
    assert not facts.directories if phase == "names" else facts.directories["."][1] is None
    facts.clear()


@pytest.mark.usefixtures("model_free")
@pytest.mark.parametrize("failure", [TimeoutError, runtime_resources.PreparationBudgetExceeded,
                                      runtime_resources.PreparationCapacityBusy])
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
        initialize = source_release._DirectoryFacts.__init__
        def initialized(self):
            initialize(self)
            phases.append(self)
        def refused(*args, **kwargs):
            raise failure("proof validation ended")
        def rebuilt(*args, **kwargs):
            pytest.fail("a preparation failure must not reproject a source")
        monkeypatch.setattr(source_release._DirectoryFacts, "__init__", initialized)
        monkeypatch.setattr(vault.PathGuard, "recheck", refused)
        monkeypatch.setattr(context_task, "project_source", rebuilt)
        with pytest.raises(failure), store.released_task_corpus(
                paths, caller, check=lambda: None, max_bytes=2 * 1024 * 1024):
            pytest.fail("failed validation must not publish")
    assert phases and all(not phase.directories for phase in phases)
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
        changed = False
        def validated(root, basis, proofs, **kwargs):
            nonlocal changed
            result = validate(root, basis, proofs, **kwargs)
            phase = kwargs.get("directory_facts")
            if phase is not None and all(phase is not current for current in phases):
                phases.append(phase)
            if len(phases) == 2 and not changed:
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
        if kwargs.get("memory") is not None:
            kwargs["memory"](extra=20000)
            build = next(iter(lexstore.get_store(root)._released_corpus_builds.values()))
            forest = set()
            _independent_bytes([build.basis, build.candidates, *build.rows.keys(), *build.rows.values()], forest)
            expected = _independent_bytes([observed["source"], observed["capture"], observed["capture"].memo, *proofs], forest)
            assert kwargs["preparation_owner"].floor_bytes >= expected
        return validate(root, basis, proofs, **kwargs)
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
