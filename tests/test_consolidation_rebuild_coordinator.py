"""Coordinator integration contract for post-publication derivative rebuilds."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from exomem.governance import consolidation_receipts

_tests_package = ModuleType("tests")
_tests_package.__path__ = [str(Path(__file__).parent)]
sys.modules.setdefault("tests", _tests_package)

from tests.test_consolidation_content_publication import (  # noqa: E402
    RUN_ID,
    _action,
    _setup,
)

VERIFYING_AT = "2026-08-30T12:00:06.000Z"
POST_PUBLICATION_CENSUS = hashlib.sha256(b"post-publication-census").hexdigest()
DRIFTED_CENSUS = hashlib.sha256(b"drifted-census").hexdigest()


class SimulatedRebuildCrash(BaseException):
    pass


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )


def _published_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, object], bytes]:
    from exomem.governance import (
        consolidation_content_publication,
        consolidation_saga,
    )

    after = b"published canonical destination bytes\n"
    actions = (
        _action(
            0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/destination.md",
            before=None,
            after=after,
        ),
    )
    vault, _partition, _loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (after,),
    )
    publication = consolidation_content_publication.publish_stored_content_batches(**arguments)
    assert publication.seal_state.phase == "rebuilding"

    def verify_policy_terminal(**kwargs):
        assert kwargs["vault_root"] == vault
        assert kwargs["vault_binding_digest"] == arguments["vault_binding_digest"]
        assert kwargs["allowed_seal_phases"] == frozenset({"rebuilding", "verifying"})
        return consolidation_saga._policy_terminal(  # noqa: SLF001
            kwargs["terminal"],
            expected_policy_fingerprint=kwargs["expected_policy_fingerprint"],
        )

    monkeypatch.setattr(
        consolidation_saga,
        "_verify_policy_terminal_receipt",
        verify_policy_terminal,
    )
    coordinator_arguments = {
        key: arguments[key]
        for key in (
            "vault_root",
            "admission",
            "policy_terminal",
            "vault_binding_digest",
            "run_id",
            "operation_id",
            "journal_digest",
            "request_digest",
            "plan_digest",
        )
    }
    coordinator_arguments["verifying_at"] = VERIFYING_AT
    return vault, coordinator_arguments, after


def _terminal(component: str, context):
    from exomem.governance import consolidation_rebuild

    return consolidation_rebuild.DerivativeRebuildTerminal(
        schema=consolidation_rebuild.DERIVATIVE_REBUILD_TERMINAL_SCHEMA,
        component=component,
        canonical_census_digest=context.canonical_census_digest,
        artifact_fingerprint=hashlib.sha256(f"rebuilt:{component}".encode()).hexdigest(),
    )


def _rebuild_records(vault: Path) -> list[dict[str, object]]:
    return [
        record
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
        if record.get("event_type") == "consolidation"
        and isinstance(record.get("consolidation_event"), dict)
        and record["consolidation_event"].get("kind") == "rebuild-kind"
    ]


def test_rebuild_derives_a_post_publication_census_and_chains_all_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_rebuild,
        consolidation_rebuild_coordinator,
        consolidation_rebuild_journal,
    )

    vault, arguments, after = _published_run(tmp_path, monkeypatch)
    census_roots: list[Path] = []
    calls: list[tuple[str, Path, str]] = []

    def snapshot(root: Path) -> str:
        census_roots.append(root)
        assert (root / "Knowledge Base/Notes/destination.md").read_bytes() == after
        return POST_PUBLICATION_CENSUS

    def rebuild(component: str, context):
        calls.append((component, context.vault_root, context.canonical_census_digest))
        return _terminal(component, context)

    monkeypatch.setattr(consolidation_rebuild_coordinator, "_snapshot_census", snapshot)
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: rebuild,
    )

    result = consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)

    assert result.canonical_census_digest == POST_PUBLICATION_CENSUS
    assert result.completed_components == consolidation_rebuild.DERIVATIVE_COMPONENTS
    assert [component for component, _root, _digest in calls] == list(
        consolidation_rebuild.DERIVATIVE_COMPONENTS
    )
    assert all(root == vault and digest == POST_PUBLICATION_CENSUS for _name, root, digest in calls)
    assert census_roots and all(root == vault for root in census_roots)
    assert result.seal_state.phase == "verifying"
    assert all(entry.status == "final" for entry in result.rebuild_journal.components)

    stored = consolidation_rebuild_journal.ConsolidationRebuildJournalStore(
        vault,
        run_id=RUN_ID,
    ).load()
    assert stored == result.rebuild_journal
    assert (
        tuple(entry.component for entry in stored.components)
        == consolidation_rebuild.DERIVATIVE_COMPONENTS
    )

    records = _rebuild_records(vault)
    intents = [
        consolidation_receipts.validate_nested(record["consolidation_event"], outer_phase="intent")
        for record in records
        if record["phase"] == "intent"
    ]
    terminals = [record for record in records if record["phase"] == "committed"]
    assert [intent["rebuild_ordinal"] for intent in intents] == list(range(8))
    assert [intent["effect_ordinal"] for intent in intents] == list(range(18, 26))
    content_terminals = [
        record
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
        if record.get("event_type") == "consolidation"
        and isinstance(record.get("consolidation_event"), dict)
        and record["consolidation_event"].get("kind") == "content-batch"
        and record["phase"] == "committed"
    ]
    assert intents[0]["semantic_parent_event_id"] == content_terminals[-1]["event_id"]
    assert len(terminals) == 8
    assert all(
        later["semantic_parent_event_id"] == previous["event_id"]
        for previous, later in zip(terminals[:-1], intents[1:], strict=True)
    )


def test_retry_after_all_rebuild_terminals_only_advances_the_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_rebuild_coordinator

    vault, arguments, _after = _published_run(tmp_path, monkeypatch)
    rebuilds: list[str] = []

    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_snapshot_census",
        lambda _root: POST_PUBLICATION_CENSUS,
    )
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: (
            lambda component, context: rebuilds.append(component) or _terminal(component, context)
        ),
    )

    def crash_before_verifying(point: str) -> None:
        if point == "before-verifying":
            raise SimulatedRebuildCrash

    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_crash_point",
        crash_before_verifying,
    )

    with pytest.raises(SimulatedRebuildCrash):
        consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)

    assert len(rebuilds) == 8
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: lambda *_args: pytest.fail("final rebuilds must not run on retry"),
    )

    retried = consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)

    assert retried.seal_state.phase == "verifying"
    assert len(rebuilds) == 8


def test_retry_after_component_result_finishes_receipt_without_rerunning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_effect_coordinator,
        consolidation_rebuild,
        consolidation_rebuild_coordinator,
        consolidation_rebuild_journal,
    )

    vault, arguments, _after = _published_run(tmp_path, monkeypatch)
    rebuilds: list[str] = []

    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_snapshot_census",
        lambda _root: POST_PUBLICATION_CENSUS,
    )
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: (
            lambda component, context: rebuilds.append(component)
            or _terminal(component, context)
        ),
    )
    crashed = False

    def crash_after_component_result(point: str) -> None:
        nonlocal crashed
        if point == "after-effect" and not crashed:
            crashed = True
            raise SimulatedRebuildCrash

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        crash_after_component_result,
    )
    with pytest.raises(SimulatedRebuildCrash):
        consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)

    assert rebuilds == ["media"]
    rebuild_state = consolidation_rebuild_journal.ConsolidationRebuildJournalStore(
        vault,
        run_id=RUN_ID,
    ).load()
    assert rebuild_state.components[0].status == "prepared"
    assert (
        consolidation_effect_coordinator.ConsolidationEffectJournalStore(
            vault,
            run_id=RUN_ID,
            effect_ordinal=18,
        )
        .load()
        .status
        == "prepared"
    )

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    retried = consolidation_rebuild_coordinator.rebuild_published_destination(
        **arguments
    )

    assert rebuilds == list(consolidation_rebuild.DERIVATIVE_COMPONENTS)
    assert rebuilds.count("media") == 1
    assert retried.seal_state.phase == "verifying"


def test_canonical_census_drift_refuses_before_the_next_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_rebuild_coordinator

    _vault, arguments, _after = _published_run(tmp_path, monkeypatch)
    calls: list[str] = []

    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_snapshot_census",
        lambda _root: POST_PUBLICATION_CENSUS if not calls else DRIFTED_CENSUS,
    )
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: lambda component, context: calls.append(component) or _terminal(component, context),
    )

    with pytest.raises(
        consolidation_rebuild_coordinator.ConsolidationRebuildCoordinatorUnavailable
    ):
        consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)

    assert calls == ["media"]


def test_verifying_replay_does_not_rebuild_final_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_rebuild_coordinator

    _vault, arguments, _after = _published_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_snapshot_census",
        lambda _root: POST_PUBLICATION_CENSUS,
    )
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: _terminal,
    )

    first = consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)
    assert first.seal_state.phase == "verifying"
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: lambda *_args: pytest.fail("verifying replay must not rebuild"),
    )

    replayed = consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)

    assert replayed == first
