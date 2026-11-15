"""Overlapping producer recovery cannot invent Source independence."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import json

import pytest
from test_capture_attempts import attempts  # noqa: F401

from exomem import memory_refs, source_custody
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope


def test_complete_recovery_fences_both_overlapping_publishers(vault, attempts):
    """A census begun before the second admission cannot clear either publisher."""
    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    producer = source_custody.OwnerLocator(owner="collection_imports", locator="store-a", version="2")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        source_custody.publish(vault, source_custody.prepare_enrollment(
            vault, reference=reference, source_hash="1" * 64))
        first, = source_custody.reserve_dependencies(vault, (reference, reference), producer=producer,
                                                    operation_id="import-a", checkpoint="instance-a:0")
        manifest, _ = source_custody.read_manifest(vault, reference)
        earlier = manifest.dependency_epochs[producer.owner]
        second, = source_custody.reserve_dependencies(vault, (reference,), producer=producer,
                                                     operation_id="import-b", checkpoint="instance-a:0")
        assert first.epoch_id == second.epoch_id
        assert not source_custody.reconcile_dependencies(vault, reference, producer=producer,
                                                        expected=earlier, checkpoint="instance-a:1", complete=True)
        source_custody.require_dependencies(vault, (first, second))
        manifest, _ = source_custody.read_manifest(vault, reference)
        current = manifest.dependency_epochs[producer.owner]
        # A later effect attempt invalidates a census even if positive settlement is lost.
        source_custody.begin_dependency_effect(vault, (first, second))
        assert not source_custody.reconcile_dependencies(vault, reference, producer=producer,
                                                        expected=current, checkpoint="instance-a:2", complete=True)
        manifest, _ = source_custody.read_manifest(vault, reference)
        current = manifest.dependency_epochs[producer.owner]
        # The producer has now proved both jobs reverted; closing revokes both old publication rights.
        assert source_custody.reconcile_dependencies(vault, reference, producer=producer,
                                                     expected=current, checkpoint="instance-a:2", complete=True)
        assert source_custody.reconcile_dependencies(vault, reference, producer=producer,
                                                     expected=current, checkpoint="instance-a:2", complete=True)
        with pytest.raises(OpError, match="SOURCE_DEPENDENCY_STALE"):
            source_custody.require_dependencies(vault, (first, second))
        manifest, _ = source_custody.read_manifest(vault, reference)
        assert manifest.dependency.state == "clear"
        later, = source_custody.reserve_dependencies(vault, (reference,), producer=producer,
                                                    operation_id="import-c", checkpoint="instance-a:2")
        assert later.epoch_id != first.epoch_id
        source_custody.require_dependencies(vault, (later,))


def test_recovery_never_clears_legacy_uncertainty_or_positive_dependence(vault, attempts):
    """A clean new import cannot erase pre-upgrade uncertainty or another family's retained version."""
    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    producer = source_custody.OwnerLocator(owner="collection_imports", locator="store-a", version="2")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        plan = source_custody.prepare_enrollment(vault, reference=reference, source_hash="2" * 64)
        legacy = json.loads(plan.writes[0].content)
        legacy["version"] = 1
        legacy["dependency"]["uncertain"] = True
        legacy.pop("dependency_epochs", None)
        legacy.pop("legacy_dependency_uncertain", None)
        plan.writes[0].content = json.dumps(legacy)
        source_custody.publish(vault, plan)
        admission, = source_custody.reserve_dependencies(vault, (reference,), producer=producer,
                                                        operation_id="import-a", checkpoint="instance-a:0")
        manifest, _ = source_custody.read_manifest(vault, reference)
        epoch = manifest.dependency_epochs[producer.owner]
        assert not source_custody.reconcile_dependencies(vault, reference, producer=producer,
                                                        expected=epoch, checkpoint="instance-a:1", complete=False)
        assert source_custody.reconcile_dependencies(vault, reference, producer=producer,
                                                     expected=epoch, checkpoint="instance-a:2", complete=True)
        manifest, _ = source_custody.read_manifest(vault, reference)
        assert manifest.dependency.state == "uncertain"
        witness = source_custody.OwnerLocator(owner="collection_imports", locator="store-a:transition-a:row-1:version-1",
                                             version="instance-a:head-a")
        # A delayed exact positive remains evidence, even after its epoch closed.
        source_custody.observe_dependency(vault, admission, witness=witness)
        manifest, _ = source_custody.read_manifest(vault, reference)
        assert manifest.dependency.state == "present"
        assert manifest.dependency.first_publication == witness


def test_issued_dependency_cannot_fall_back_to_unenrolled_after_custody_loss(vault, attempts):
    """A missing manifest after reservation is lost evidence, not legacy admission."""
    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    producer = source_custody.OwnerLocator(owner="file_records", locator="record-history", version="2")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        assert source_custody.reserve_dependencies(vault, (reference,), producer=producer,
                                                   operation_id="record-a", checkpoint="history:0") == ()
        source_custody.publish(vault, source_custody.prepare_enrollment(
            vault, reference=reference, source_hash="3" * 64))
        admission, = source_custody.reserve_dependencies(vault, (reference,), producer=producer,
                                                        operation_id="record-a", checkpoint="history:0")
        # Simulate loss of canonical evidence, as distinct from a never-enrolled Source.
        source_custody.manifest_path(vault, reference).unlink()
        with pytest.raises(OpError, match="SOURCE_CUSTODY_UNAVAILABLE"):
            source_custody.require_dependencies(vault, (admission,))
