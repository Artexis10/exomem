"""Exact-state persistence for the consolidation derivative rebuild."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from exomem import writer_lease

RUN_ID = "00000000-0000-4000-8000-000000000091"
OPERATION_ID = "00000000-0000-4000-8000-000000000092"


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _store(vault: Path):
    from exomem.governance import consolidation_rebuild_journal

    return consolidation_rebuild_journal.ConsolidationRebuildJournalStore(
        vault,
        run_id=RUN_ID,
    )


def _create(store):
    return store.create(
        operation_id=OPERATION_ID,
        request_digest=_digest("request"),
        plan_digest=_digest("plan"),
        partition_digest=_digest("partition"),
        content_batch_journal_digest=_digest("batch-journal"),
        content_effects_digest=_digest("content-effects"),
        last_content_terminal_event_id=_digest("last-terminal") + ":committed",
        last_content_terminal_payload_digest=_digest("last-payload"),
        canonical_census_digest=_digest("post-publication-census"),
    )


def _path(vault: Path) -> Path:
    return vault / "Knowledge Base" / "_Consolidation" / "runs" / RUN_ID / "rebuild.json"


def test_create_persists_exact_prior_state_and_restarts(tmp_path: Path) -> None:
    from exomem.governance import consolidation_rebuild

    vault = tmp_path / "vault"
    created = _create(_store(vault))

    assert created.schema == "exomem.consolidation-rebuild-journal/v1"
    assert created.revision == 1
    assert tuple(entry.component for entry in created.components) == (
        consolidation_rebuild.DERIVATIVE_COMPONENTS
    )
    assert tuple(entry.status for entry in created.components) == ("prior",) * len(
        consolidation_rebuild.DERIVATIVE_COMPONENTS
    )
    assert _store(vault).load() == created


def test_ordered_result_and_terminal_transitions_finish_all_components(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_rebuild

    store = _store(tmp_path / "vault")
    initial = _create(store)

    for ordinal, component in enumerate(consolidation_rebuild.DERIVATIVE_COMPONENTS):
        fingerprint = _digest(f"artifact:{component}")
        prepared = store.record_component_result(component, fingerprint)
        assert prepared.revision == initial.revision + 1
        assert prepared.components[ordinal].status == "prepared"
        assert prepared.components[ordinal].artifact_fingerprint == fingerprint
        assert store.record_component_result(component, fingerprint) == prepared

        final = store.finalize_component(
            component,
            fingerprint,
            terminal_event_id=_digest(f"terminal:{component}") + ":committed",
            terminal_payload_digest=_digest(f"payload:{component}"),
            effect_journal_digest=_digest(f"effect:{component}"),
        )
        assert final.revision == prepared.revision + 1
        assert final.components[ordinal].status == "final"
        assert final.components[ordinal].terminal_event_id == (
            _digest(f"terminal:{component}") + ":committed"
        )
        assert (
            store.finalize_component(
                component,
                fingerprint,
                terminal_event_id=_digest(f"terminal:{component}") + ":committed",
                terminal_payload_digest=_digest(f"payload:{component}"),
                effect_journal_digest=_digest(f"effect:{component}"),
            )
            == final
        )
        initial = final

    assert all(entry.status == "final" for entry in store.load().components)


def test_create_adopts_progressed_same_basis_but_refuses_changed_basis(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_rebuild_journal

    vault = tmp_path / "vault"
    store = _store(vault)
    _create(store)
    progressed = store.record_component_result("media", _digest("artifact:media"))
    assert _create(_store(vault)) == progressed

    with pytest.raises(consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable):
        store.create(
            operation_id=OPERATION_ID,
            request_digest=_digest("changed-request"),
            plan_digest=_digest("plan"),
            partition_digest=_digest("partition"),
            content_batch_journal_digest=_digest("batch-journal"),
            content_effects_digest=_digest("content-effects"),
            last_content_terminal_event_id=_digest("last-terminal") + ":committed",
            last_content_terminal_payload_digest=_digest("last-payload"),
            canonical_census_digest=_digest("post-publication-census"),
        )
    assert store.load() == progressed


def test_changed_fingerprint_or_out_of_order_component_refuses(tmp_path: Path) -> None:
    from exomem.governance import consolidation_rebuild_journal

    store = _store(tmp_path / "vault")
    initial = _create(store)
    with pytest.raises(consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable):
        store.record_component_result("lexical", _digest("artifact:lexical"))
    with pytest.raises(consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable):
        store.finalize_component(
            "media",
            _digest("artifact:media"),
            terminal_event_id=_digest("terminal:media") + ":committed",
            terminal_payload_digest=_digest("payload:media"),
            effect_journal_digest=_digest("effect:media"),
        )
    prepared = store.record_component_result("media", _digest("artifact:media"))
    with pytest.raises(consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable):
        store.record_component_result("media", _digest("different-artifact:media"))
    with pytest.raises(consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable):
        store.finalize_component(
            "media",
            _digest("different-artifact:media"),
            terminal_event_id=_digest("terminal:media") + ":committed",
            terminal_payload_digest=_digest("payload:media"),
            effect_journal_digest=_digest("effect:media"),
        )
    assert store.load() == prepared
    assert initial.components[0].status == "prior"


@pytest.mark.parametrize("mutation", ("missing", "tamper"))
def test_missing_or_tampered_journal_refuses(tmp_path: Path, mutation: str) -> None:
    from exomem.governance import consolidation_rebuild_journal

    vault = tmp_path / "vault"
    _create(_store(vault))
    path = _path(vault)
    if mutation == "missing":
        path.unlink()
    else:
        value = json.loads(path.read_bytes())
        value["revision"] = 2
        path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    with pytest.raises(consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable):
        _store(vault).load()
