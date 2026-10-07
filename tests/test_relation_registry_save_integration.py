from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import (
    commands,
    graph_sync,
    memory_schema,
    relation_registry,
    vault,
    writer_lease,
)


def _proposal(description: str = "A reviewed relation.") -> dict[str, object]:
    return {
        "schema_version": 1,
        "extensions": {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": description,
                "direction": "directed",
                "aliases": ["applies_to"],
            }
        },
    }


def _delta() -> dict[str, object]:
    return {
        "upsert": {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "A reviewed relation.",
                "direction": "directed",
                "aliases": ["applies_to"],
            }
        }
    }


@pytest.fixture(autouse=True)
def owner_library_scope():
    from exomem.governance.principal import library_scope

    with library_scope():
        yield


def test_save_registry_supplies_only_complete_registry_yaml_to_canonical_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[vault.PlannedWrite]] = []

    def batch(writes, *, vault_root: Path, **_sealed):  # noqa: ANN001, ANN003
        calls.append(list(writes))
        return [write.path for write in calls[-1]]

    monkeypatch.setattr(vault, "batch_atomic_write", batch)

    saved = relation_registry.save_registry(tmp_path, _proposal())

    assert saved["path"] == relation_registry.extension_registry_path(
        tmp_path
    ).relative_to(tmp_path).as_posix()
    assert len(calls) == 1
    # The registry YAML and the snapshot of what it replaced; the batch writer,
    # not the caller, adds the graph epoch.
    write, snapshot = calls[0]
    assert write.path == relation_registry.extension_registry_path(tmp_path)
    assert yaml.safe_load(write.content) == _proposal()
    assert snapshot.create_only is True
    assert snapshot.path.parent.name == "relation-registry"
    assert not any("epoch" in write.path.name for write in calls[0])


@pytest.mark.parametrize(
    "mutate, code",
    [
        (
            lambda candidate: candidate["extensions"]["vault.applies_to"].update(
                description="A changed meaning."
            ),
            "IMMUTABLE_RELATION_MEANING",
        ),
        (
            lambda candidate: candidate["extensions"]["vault.applies_to"].update(
                aliases=[]
            ),
            "IMMUTABLE_RELATION_MEANING",
        ),
        (
            lambda candidate: candidate["extensions"].pop("vault.applies_to"),
            "IMMUTABLE_RELATION_MEANING",
        ),
    ],
)
def test_invalid_full_registry_change_never_reaches_canonical_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    code: str,
) -> None:
    created = relation_registry.save_registry(tmp_path, _proposal())
    candidate = _proposal()
    mutate(candidate)
    calls = 0

    def forbidden_batch(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal calls
        calls += 1
        raise AssertionError("invalid registry reached the canonical batch")

    monkeypatch.setattr(vault, "batch_atomic_write", forbidden_batch)

    with pytest.raises(ValueError, match=code):
        relation_registry.save_registry(
            tmp_path, candidate, expected_hash=created["content_hash"]
        )

    assert calls == 0


def test_stale_delta_merge_converges_without_a_second_registry_write(tmp_path: Path) -> None:
    created = relation_registry.save_registry(tmp_path, _proposal())
    current = relation_registry.load_registry(tmp_path)
    delta = {
        "upsert": {
            "vault.applies_to": {
                "aliases": ["applies_to", "governs_case"]
            }
        }
    }
    merged = relation_registry.merge_extension_delta(
        memory_schema.relation_registry_proposal(current), delta
    )
    first = relation_registry.save_registry(
        tmp_path, merged, expected_hash=created["content_hash"]
    )

    with pytest.raises(ValueError, match="STALE_RELATION_REGISTRY"):
        relation_registry.save_registry(
            tmp_path, merged, expected_hash=created["content_hash"]
        )

    assert relation_registry.load_registry(tmp_path).extension_hash == first["content_hash"]


@pytest.mark.parametrize(
    "join_outcome, expected_graph_sync",
    [(True, "completed"), (False, "pending"), (RuntimeError("join cut"), "failed")],
)
def test_public_delta_save_projects_graph_outcome_and_replays_without_recommit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    join_outcome: object,
    expected_graph_sync: str,
) -> None:
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    command = next(
        item for item in commands.PRODUCT_COMMANDS if item.name == "schema_memory"
    )
    registry_path = relation_registry.extension_registry_path(root)
    original_batch = vault.batch_atomic_write
    registry_batches = 0

    def counted_batch(writes, **kwargs):  # noqa: ANN001
        nonlocal registry_batches
        planned = list(writes)
        if any(write.path == registry_path for write in planned):
            registry_batches += 1
        return original_batch(planned, **kwargs)

    def join(*_args, **_kwargs):  # noqa: ANN002, ANN003
        if isinstance(join_outcome, Exception):
            raise join_outcome
        return join_outcome

    monkeypatch.setattr(vault, "batch_atomic_write", counted_batch)
    monkeypatch.setattr(
        graph_sync,
        "registered_checkpoint",
        lambda candidate, **_kwargs: graph_sync.read_checkpoint(candidate),
    )
    monkeypatch.setattr(graph_sync, "join_registered_if_settled", join)
    arguments = {
        "subject": "relations",
        "operation": "save-relations",
        "proposal": _delta(),
        "expected_hash": relation_registry.load_registry(root).extension_hash,
        "why": "Reviewed durable recurring meaning.",
    }

    first = manager.invoke(
        command,
        (root,),
        arguments,
        idempotency_key=f"relation-save-{expected_graph_sync}",
        read_only=False,
    )
    committed_bytes = registry_path.read_bytes()
    replay = manager.invoke(
        command,
        (root,),
        arguments,
        idempotency_key=f"relation-save-{expected_graph_sync}",
        read_only=False,
    )

    assert first["state"] == "committed"
    assert first["graph_sync"] == expected_graph_sync
    assert replay == first
    assert registry_path.read_bytes() == committed_bytes
    assert registry_batches == 1


def _vault_with_a_mismatched_extension(vault_root: Path) -> str:
    """A registry saved before the family rule: ownership declared as `association`."""
    path = relation_registry.extension_registry_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "extensions": {
                    "vault.holds_site": {
                        "parent": "owns",
                        "family": "association",
                        "description": "Holds a site.",
                        "direction": "directed",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return relation_registry.load_registry(vault_root).extension_hash


def _save(vault_root: Path, delta: dict[str, object], expected_hash: str) -> None:
    commands.op_schema_memory(
        vault_root,
        subject="relations",
        operation="save-relations",
        proposal=delta,
        expected_hash=expected_hash,
        why="repair a registry saved before the family rule",
    )


def test_a_saved_family_mismatch_is_an_advisory_that_blocks_no_save(vault: Path) -> None:
    current = _vault_with_a_mismatched_extension(vault)
    loaded = relation_registry.load_registry(vault)
    assert [(item["code"], item["severity"]) for item in loaded.findings] == [
        ("family_mismatch", "warning")
    ]

    _save(vault, _delta(), current)
    after = relation_registry.load_registry(vault)
    assert {"vault.applies_to", "vault.holds_site"} <= set(after.extensions)

    _save(vault, {"deprecate": {"vault.holds_site": "owns"}}, after.extension_hash)
    assert relation_registry.load_registry(vault).extensions["vault.holds_site"].status == "deprecated"


def test_a_new_mismatched_extension_is_still_refused_beside_a_grandfathered_one(
    vault: Path,
) -> None:
    current = _vault_with_a_mismatched_extension(vault)
    delta = {
        "upsert": {
            "vault.runs_site": {
                "parent": "owns",
                "family": "association",
                "description": "Runs a site.",
                "direction": "directed",
            }
        }
    }
    with pytest.raises(ValueError, match="family_mismatch"):
        _save(vault, delta, current)
    assert "vault.runs_site" not in relation_registry.load_registry(vault).extensions
